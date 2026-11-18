"""データベース（PostgreSQL）からデータを読むツール。

読み取り専用。守りは重ねてある。
1. 読み取りだけを許したロールでつなぐ（利用者が用意する。scripts/setup_data_db.sh）。これが本当の守り。
   管理者（スーパーユーザー）のロールでは、問い合わせを受け付けない。
2. 読み取り専用のトランザクションの中で実行する。
3. SQL は、データベースの側のカーソル（DECLARE … CURSOR FOR）の中身として渡す。カーソルにできるのは
   SELECT の類だけで、複数の文をまとめて送っても、データベースが受け付けない。
4. 時間と、受け取る行数・大きさに上限がある。

個人情報の列は、データベースの側で、このロールから読めなくしておく（列ごとの権限。scripts/setup_data_db.sh）。
読めない列を使った問い合わせは、どんな書き方でも、データベースが断る。表の一覧では「読めない」と示す。

結果は CSV にして作業フォルダに保存し、モデルには先頭の数行だけを見せる。
全体を使う集計やグラフは、その CSV を run_python に渡して行う。

接続は、アプリの側から行う。コードを実行するコンテナ（sandbox/）からは、データベースに届かない。

Web 検索の前の確認（ADR-0022）のために、顧客の名前などの列を、別のロール（DATA_DB_SCREENER_USER）で読む処理は、
safety/watch_terms.py にある。エージェントのツールは、そのロールを使わない。
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import json
import re
import time
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from deepagents import FilesystemPermission
from deepagents.backends.protocol import BackendProtocol
from deepagents.backends.utils import validate_path
from langchain_core.tools import BaseTool, StructuredTool
from psycopg.conninfo import make_conninfo
from pydantic import BaseModel, Field

from data_agent.safety.audit import audit
from data_agent.safety.guardrails import Guard
from data_agent.settings import DatabaseLimits, Settings, get_guard, get_security, get_settings
from data_agent.tools import outputs

QUERY_TOOL = "query_database"
TABLES_TOOL = "list_database_tables"

_MB = 1024 * 1024
_CONNECT_TIMEOUT_SEC = 10
_PREVIEW_CELL_CHARS = 200  # モデルに見せるときの、1つの値の長さの上限
_TABLES_MAX_CHARS = 12_000  # 表の一覧をモデルに返すときの長さの上限
_TABLES_MAX_COLUMNS = 20_000  # 表の一覧のために読む列の数の上限
_FETCH_ROWS = 1_000  # データベースから一度に受け取る行数
# 読み取りの文の、先頭の言葉。書き込みは、これに加えて、カーソルと読み取り専用のトランザクションでも止まる
_READ_KEYWORDS = {"select", "with", "values", "table"}
_LEADING = re.compile(r"\A(?:\s+|--[^\n]*(?:\n|\Z)|/\*.*?\*/|\()+", re.DOTALL)
_KINDS = {"r": "テーブル", "p": "テーブル", "v": "ビュー", "m": "ビュー", "f": "外部テーブル"}
# 表やビューを読む権限がないときの、データベースのメッセージ（列ごとに読めなくしてあるときも、この形になる）
_DENIED_TABLE = re.compile(r"permission denied for (?:table|view|materialized view|foreign table) ")

_QUERY_DESCRIPTION = """データベース（PostgreSQL）に、読み取りの SQL を1つ実行する。データの取得と集計に使う。

- 実行できるのは、読み取りの文（SELECT。WITH … SELECT、VALUES、TABLE も可）を1つだけ。書き込みや変更はできない。
- テーブルと列の名前は、先に list_database_tables で確かめる。
- 一覧で「読めない」と書かれた列は、個人情報などのために、読み取りを止めてある。SQL に書くと、問い合わせ全体が失敗する（WHERE や JOIN に書いても同じ）。読めない列がある表では、SELECT * も使えない。読める列の名前を書く。
- 結果は CSV にして、作業フォルダの {output_dir}/ の下に保存される。返ってくるのは、行数・列・保存先と、先頭の {preview_rows} 行だけ。
- 全体を使う集計やグラフは、保存された CSV のパスを run_python の input_files に渡して行う。複数の結果を一緒に使うときは、name で別々の名前を付ける。
- 受け取れるのは {max_rows} 行まで。大きな表は、SQL の側で集計（GROUP BY）や条件（WHERE）で絞る。
- 実行できる時間は {timeout} 秒。"""

_TABLES_DESCRIPTION = """データベースにある表（テーブルとビュー）と、その列の名前・型・説明を返す。SQL を書く前に使う。

table を指定すると、その表だけを返す（例: sales、public.sales）。表が多いときは、まず指定せずに名前の一覧を見て、次に table を指定して列を見る。

「読めない」と書かれた列は、個人情報などのために、読み取りを止めてある。値を取る方法はないので、利用者には、その列は読めない設定であることを伝える。"""

# 表と列の一覧。1つでも読める列がある表だけを出す。パーティションの子は、親と同じなので出さない。
# 最後の列は、その列を読めるかどうか（列ごとに読めなくしてあることがある）
_TABLES_SQL = """
SELECT n.nspname, c.relname, c.relkind::text, c.reltuples::bigint,
       obj_description(c.oid, 'pg_class'),
       a.attname, format_type(a.atttypid, a.atttypmod), col_description(c.oid, a.attnum),
       has_column_privilege(c.oid, a.attnum, 'SELECT')
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
  AND NOT c.relispartition
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname !~ '^pg_(toast|temp)'
  AND has_any_column_privilege(c.oid, 'SELECT')
ORDER BY n.nspname, c.relname, a.attnum
"""


# つなぐロールが、管理者か。すべての表やサーバーのファイルを読める組み込みのロールに入っているか
_ROLE_CHECK = """
    SELECT r.rolsuper,
           EXISTS (
               SELECT 1 FROM pg_catalog.pg_roles g
               WHERE g.rolname IN ('pg_read_all_data', 'pg_write_all_data', 'pg_read_server_files',
                                   'pg_write_server_files', 'pg_execute_server_program')
                 AND pg_catalog.pg_has_role(current_user, g.oid, 'MEMBER')
           )
    FROM pg_catalog.pg_roles r WHERE r.rolname = current_user
"""


class DatabaseError(Exception):
    """問い合わせを実行できなかった。メッセージは、そのままモデルに返す。"""

    def __init__(self, message: str, status: str = "error") -> None:
        super().__init__(message)
        self.status = status  # 監査ログに残す種類


@dataclass
class QueryResult:
    columns: list[tuple[str, str]]  # (名前, 型)
    rows: list[list[str]]  # 値は、CSV に書く文字列にしてある
    truncated: str | None  # 途中で打ち切ったときの理由（"rows" か "size"）
    duration_ms: int


class QueryInput(BaseModel):
    sql: str = Field(description="実行する SQL（読み取りの文を1つ）。")
    name: str = Field(
        default="query",
        description="結果を保存する CSV の名前（拡張子なし。例: monthly_sales）。",
    )


class TablesInput(BaseModel):
    table: str | None = Field(default=None, description="列を見たい表の名前（例: sales、public.sales）。省略すると全部。")


def first_keyword(sql: str) -> str:
    """SQL の先頭の言葉を、小文字で返す（前に付いた空白・コメント・かっこは飛ばす）。"""
    match = re.match(r"[A-Za-z]+", _LEADING.sub("", sql))
    return match.group().lower() if match else ""


def cell_text(value: Any) -> str:
    """データベースの値を、CSV に書く文字列にする。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, bytes | bytearray | memoryview):
        return f"（バイナリ {len(bytes(value))} バイト）"
    if isinstance(value, dict | list):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def to_csv(columns: list[str], rows: list[list[str]]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(columns)
    writer.writerows(rows)
    return buffer.getvalue()


def safe_name(name: str) -> str:
    """CSV の名前に使える文字だけを残す。使える文字がなければ query にする。"""
    cleaned = re.sub(r"[^\w\-]+", "_", name.removesuffix(".csv"), flags=re.UNICODE).strip("_")[:50]
    return cleaned or "query"


def conninfo(settings: Settings, limits: DatabaseLimits) -> str | None:
    """psycopg に渡す接続情報を返す。DATA_DB_HOST が未設定なら None。"""
    if not settings.data_db_host:
        return None
    missing = [
        name
        for name, value in (
            ("DATA_DB_NAME", settings.data_db_name),
            ("DATA_DB_USER", settings.data_db_user),
            ("DATA_DB_PASSWORD", settings.data_db_password),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"DATA_DB_HOST を設定した場合は、{'、'.join(missing)} も設定してください。")
    timeout_ms = limits.statement_timeout_seconds * 1000
    return make_conninfo(
        host=settings.data_db_host,
        port=settings.data_db_port,
        dbname=settings.data_db_name,
        user=settings.data_db_user,
        password=settings.data_db_password.get_secret_value(),
        sslmode=settings.data_db_sslmode,
        connect_timeout=_CONNECT_TIMEOUT_SEC,
        application_name="data-agent-data",
        # 接続そのものを読み取り専用にし、時間の上限も付ける
        options=(
            f"-c default_transaction_read_only=on -c statement_timeout={timeout_ms} "
            f"-c lock_timeout=5000 -c idle_in_transaction_session_timeout={timeout_ms + 30_000}"
        ),
    )


class Database:
    """query_database と list_database_tables の中身。"""

    def __init__(
        self,
        conninfo: str,
        backend: BackendProtocol,
        permissions: list[FilesystemPermission],
        limits: DatabaseLimits,
        guard: Guard,
        timezone: str = "Asia/Tokyo",
    ) -> None:
        self._conninfo = conninfo
        self._backend = backend
        self._permissions = permissions
        self._limits = limits
        self._guard = guard
        self._timezone = ZoneInfo(timezone)

    # ---------- ツール ----------
    async def query(self, sql: str, name: str = "query") -> str:
        """SQL を実行して、結果を CSV に保存する。モデルに返す文章を返す。"""
        limits = self._limits
        try:
            if first_keyword(sql) not in _READ_KEYWORDS:
                raise DatabaseError("実行できるのは、読み取りの文（SELECT、WITH … SELECT、VALUES、TABLE）だけです。", "rejected")
            result = await self._fetch(sql, limits.max_rows, limits.max_csv_mb * _MB)
        except DatabaseError as exc:
            audit("database_query", status=exc.status)
            return str(exc)

        names = [column for column, _ in result.columns]
        saved, note = await self._save(names, result.rows, name)
        audit(
            "database_query",
            status="ok",
            rows=len(result.rows),
            columns=len(names),
            truncated=result.truncated,
            duration_ms=result.duration_ms,
            saved=saved is not None,
        )
        lines = [f"{len(result.rows)} 行、{len(names)} 列（{result.duration_ms / 1000:.2f} 秒）"]
        if result.truncated == "rows":
            lines.append(f"上限の {limits.max_rows} 行で打ち切りました。集計（GROUP BY）や条件（WHERE）で絞ってください。")
        elif result.truncated == "size":
            lines.append(f"結果が大きいので、{limits.max_csv_mb} MB を超えたところで打ち切りました。列や行を絞ってください。")
        lines.append("列: " + "、".join(f"{column}（{kind}）" for column, kind in result.columns))
        lines.append(f"保存先: {saved}" if saved else f"保存しませんでした: {note}")
        if result.rows:
            shown = result.rows[: limits.preview_rows]
            lines.append(f"--- 先頭の {len(shown)} 行 ---")
            lines.append(to_csv(names, [[_shorten(value) for value in row] for row in shown]).rstrip())
        return "\n".join(lines)

    async def tables(self, table: str | None = None) -> str:
        """読める表と、その列を返す。"""
        try:
            result = await self._fetch(_TABLES_SQL, _TABLES_MAX_COLUMNS, 64 * _MB)
        except DatabaseError as exc:
            audit("database_tables", status=exc.status)
            return str(exc)
        found: dict[tuple[str, str], dict[str, Any]] = {}
        for schema, relname, kind, estimate, comment, column, column_type, column_comment, readable in result.rows:
            entry = found.setdefault((schema, relname), {"kind": kind, "rows": estimate, "comment": comment, "columns": []})
            entry["columns"].append((column, column_type, column_comment, readable == "true"))
        if table:
            wanted = table.strip().strip('"').lower()
            found = {key: value for key, value in found.items() if wanted in (key[1].lower(), f"{key[0]}.{key[1]}".lower())}
        hidden = sum(not readable for value in found.values() for *_, readable in value["columns"])
        audit("database_tables", status="ok", tables=len(found), hidden_columns=hidden)
        if not found:
            return f"表 {table} は見つかりませんでした。" if table else "読める表がありません。"
        text = "\n".join(_describe(key, value, with_columns=True) for key, value in found.items())
        if len(text) > _TABLES_MAX_CHARS and not table:
            # 多すぎるときは、名前だけを返す
            text = "\n".join(_describe(key, value, with_columns=False) for key, value in found.items())
            text = "表が多いので、名前だけを返します。列を見るには、table を指定してください。\n" + text
        return text[:_TABLES_MAX_CHARS]

    # ---------- データベースとのやりとり ----------
    async def _fetch(self, sql: str, max_rows: int, max_bytes: int) -> QueryResult:
        seconds = self._limits.statement_timeout_seconds
        try:
            # データベースの側の時間切れが効かなかったときのために、こちらでも時間を計る
            return await asyncio.wait_for(self._fetch_rows(sql, max_rows, max_bytes), seconds + _CONNECT_TIMEOUT_SEC + 5)
        except TimeoutError:
            raise DatabaseError(f"時間切れで止めました（{seconds} 秒）。条件で絞るか、集計してください。", "timeout") from None

    async def _fetch_rows(self, sql: str, max_rows: int, max_bytes: int) -> QueryResult:
        started = time.monotonic()
        conn = await self._connect()
        rows: list[list[str]] = []
        truncated: str | None = None
        size = 0
        try:
            # 名前を付けると、データベースの側のカーソルになる（DECLARE … CURSOR FOR <SQL>）。
            # 文を1つだけ受け付ける送り方（拡張プロトコル）で送られ、行は少しずつ受け取れる。
            # 途中でやめれば、残りは読まれない
            cursor = conn.cursor(name="data_agent_query")
            await cursor.execute(sql)
            columns = [(column.name, _type_name(conn, column.type_code)) for column in cursor.description or []]
            while truncated is None and (chunk := await cursor.fetchmany(_FETCH_ROWS)):
                for row in chunk:
                    if len(rows) >= max_rows:
                        truncated = "rows"
                        break
                    values = [cell_text(value) for value in row]
                    size += sum(len(value.encode("utf-8")) + 1 for value in values)
                    if size > max_bytes:
                        truncated = "size"
                        break
                    rows.append(values)
        except psycopg.errors.QueryCanceled:
            seconds = self._limits.statement_timeout_seconds
            raise DatabaseError(f"時間切れで止めました（{seconds} 秒）。条件で絞るか、集計してください。", "timeout") from None
        except psycopg.errors.InsufficientPrivilege as exc:
            if not _DENIED_TABLE.match(_message(exc)):
                # 表や列ではないもの（番号を進める関数など）を断られた
                raise DatabaseError(f"問い合わせに失敗しました: {_message(exc)}") from None
            # 読めなくしてある表や列（個人情報など）を使った。どの列かは、データベースは教えてくれない
            raise DatabaseError(
                f"読み取りを止めてある表か列を使っているので、実行できませんでした（{_message(exc)}）。"
                "list_database_tables で「読めない」と書かれた列は、SQL のどこにも書けません。SELECT * も使えません。"
                "読める列だけを書いてください。読めない列の値が必要な依頼なら、"
                "その列は読めない設定であることを、利用者に伝えてください。",
                "denied",
            ) from None
        except psycopg.Error as exc:
            raise DatabaseError(f"問い合わせに失敗しました: {_message(exc)}") from None
        finally:
            with contextlib.suppress(Exception):
                await conn.rollback()
            with contextlib.suppress(Exception):
                await conn.close()
        return QueryResult(columns, rows, truncated, int((time.monotonic() - started) * 1000))

    async def _connect(self) -> psycopg.AsyncConnection:
        try:
            conn = await psycopg.AsyncConnection.connect(self._conninfo)
        except psycopg.Error as exc:
            raise DatabaseError(f"データベースに接続できませんでした: {_message(exc)}", "connect_error") from None
        try:
            await conn.set_read_only(True)
            cursor = await conn.execute(_ROLE_CHECK)
            row = await cursor.fetchone()
            await conn.rollback()
        except psycopg.Error as exc:
            await conn.close()
            raise DatabaseError(f"データベースに接続できませんでした: {_message(exc)}", "connect_error") from None
        if row is None or row[0]:
            # 管理者は、読み取り専用のトランザクションの中でも、サーバーのファイルを読むなどの操作ができてしまう
            await conn.close()
            raise DatabaseError(
                "管理者（スーパーユーザー）のロールでは、問い合わせを受け付けません。"
                "読み取りだけを許したロールを作って、.env の DATA_DB_USER に設定してください"
                "（scripts/setup_data_db.sh）。その旨を利用者に伝えてください。",
                "superuser",
            )
        if row[1]:
            # すべての表を読めるロールに入っていると、列ごとに読めなくした設定（ADR-0019）が効かない
            await conn.close()
            raise DatabaseError(
                "すべての表を読めるロール（pg_read_all_data など）に入っているロールでは、問い合わせを受け付けません。"
                "個人情報の列を読めなくした設定が効かないためです。scripts/setup_data_db.sh で作った、読み取りだけのロールを"
                ".env の DATA_DB_USER に設定してください。その旨を利用者に伝えてください。",
                "broad_role",
            )
        return conn

    # ---------- 保存 ----------
    async def _save(self, columns: list[str], rows: list[list[str]], name: str) -> tuple[str | None, str]:
        """結果を CSV にして、作業フォルダに保存する。保存先（保存しなかったときは None）と、その理由を返す。"""
        folder = outputs.new_folder(self._limits.output_dir, self._timezone)
        try:
            path = validate_path(f"{folder}/{safe_name(name)}.csv")
        except ValueError:
            return None, "ファイル名が正しくありません"
        if outputs.denial_reason(self._permissions, self._backend, "write", path):
            return None, "この名前のファイルは保存できません"
        # 秘密情報やカード番号などは、保存する前にマスクする（ファイルツールで読んだときと同じ扱い）
        text, findings = self._guard.scan(to_csv(columns, rows))
        for finding in findings:
            audit("guardrail", place="database", **finding.as_dict())
        data = text.encode("utf-8")
        response = (await self._backend.aupload_files([(path, data)]))[0]
        if response.error:
            return None, f"保存に失敗しました（{response.error}）"
        return f"{response.path}（{outputs.size_text(len(data))}）", ""


def _type_name(conn: psycopg.AsyncConnection, oid: int) -> str:
    """列の型の名前（例: int4、text、int4[]）。知らない型は「不明」。"""
    info = conn.adapters.types.get(oid)
    if info is None:
        return "不明"
    return f"{info.name}[]" if info.array_oid == oid else info.name


def _message(exc: Exception) -> str:
    """データベースのエラーの1行目。モデルが SQL を直す手がかりになる。"""
    lines = str(exc).strip().splitlines()
    return lines[0][:300] if lines else type(exc).__name__


def _shorten(value: str) -> str:
    return value if len(value) <= _PREVIEW_CELL_CHARS else value[:_PREVIEW_CELL_CHARS] + "…"


def _describe(key: tuple[str, str], table: dict[str, Any], *, with_columns: bool) -> str:
    kind = _KINDS.get(table["kind"], "表")
    estimate = table["rows"]
    # 行数は、データベースが持っている見積もり。一度も集計されていない表では分からない（-1 や 0）
    rows = f"、約 {int(estimate):,} 行" if estimate.lstrip("-").isdigit() and int(estimate) > 0 else ""
    comment = f" — {table['comment']}" if table["comment"] else ""
    hidden = sum(not readable for *_, readable in table["columns"])
    hidden_note = f"。うち {hidden} 列は読めない" if hidden else ""
    head = f"{key[0]}.{key[1]}（{kind}{rows}、{len(table['columns'])} 列{hidden_note}）{comment}"
    if not with_columns:
        return head
    columns = [
        (f"  {name} {kind}" + (f" — {note}" if note else "")) if readable else f"  {name} — 読めない"
        for name, kind, note, readable in table["columns"]
    ]
    if hidden:
        columns.append("  （読めない列は SQL に書けない。この表では SELECT * も使えない）")
    return "\n".join([head, *columns])


def build_tools(backend: BackendProtocol, permissions: list[FilesystemPermission]) -> list[BaseTool]:
    """データベースのツールを作る。DATA_DB_HOST が未設定か、設定で止めてあれば、空のリストを返す。"""
    settings = get_settings()
    limits = get_security().database
    info = conninfo(settings, limits) if limits.enabled else None
    if info is None:
        return []
    return as_tools(Database(info, backend, permissions, limits, get_guard(), settings.schedule_timezone), limits)


def as_tools(database: Database, limits: DatabaseLimits) -> list[BaseTool]:
    """データベースを読む役を、エージェントのツールの形にする。点検（redteam）では、決まった行を返す役を渡して使う。"""

    async def query_database(sql: str, name: str = "query") -> str:
        return await database.query(sql, name)

    def query_database_sync(sql: str, name: str = "query") -> str:
        return asyncio.run(database.query(sql, name))

    async def list_database_tables(table: str | None = None) -> str:
        return await database.tables(table)

    def list_database_tables_sync(table: str | None = None) -> str:
        return asyncio.run(database.tables(table))

    return [
        StructuredTool.from_function(
            func=list_database_tables_sync,
            coroutine=list_database_tables,
            name=TABLES_TOOL,
            description=_TABLES_DESCRIPTION,
            args_schema=TablesInput,
        ),
        StructuredTool.from_function(
            func=query_database_sync,
            coroutine=query_database,
            name=QUERY_TOOL,
            description=_QUERY_DESCRIPTION.format(
                output_dir=limits.output_dir.rstrip("/"),
                preview_rows=limits.preview_rows,
                max_rows=limits.max_rows,
                timeout=limits.statement_timeout_seconds,
            ),
            args_schema=QueryInput,
        ),
    ]
