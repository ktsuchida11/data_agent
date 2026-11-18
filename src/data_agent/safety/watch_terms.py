"""見張る言葉（顧客の名前など）の一覧を作る（ADR-0022、ADR-0023）。

Web 検索の前の確認（search_screen.py）が、質問文にこの言葉があるかを調べる。ここでは一覧を作るだけで、判定はしない。
一覧は、次の 2 つから作って合わせる（CombinedTerms）。
- ファイル（config/security.yaml の web_search.watch_terms_file）。変わったら読み直す（WatchTermsFile）。
- データベースの決めた列（顧客の名前の列など）。その列だけを読める専用のロール（DATA_DB_SCREENER_USER）でつなぎ、
  間隔ごとに裏で読み直す（RefreshingTerms、load_watch_values）。エージェントのツールは、このロールを使わない。
読めなかったとき（読み終える前も）は、error 付きの一覧を返す。そのとき search_screen.py は、すべての検索を確かめる。
"""

from __future__ import annotations

import os
import re
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import cached_property, lru_cache
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from data_agent.safety.audit import audit
from data_agent.safety.search_screen import normalize
from data_agent.settings import PROJECT_ROOT, DatabaseLimits, Settings, get_security, get_settings

_MIN_WATCH_WIDE = 2  # 見張る言葉の長さの下限（日本語を含むもの）
_MIN_WATCH_ASCII = 3  # 英数字だけのもの
_MIN_WATCH_SHORT = 3  # 会社の種類（株式会社など）を除いた形の長さの下限（「株式会社日本」の「日本」を見張らない）
_MAX_WATCH_CHARS = 200  # これより長い値は、名前ではないとみなして見張らない
_CONNECT_TIMEOUT_SEC = 10  # tools/database.py と同じ
_WIDE = re.compile(r"[^\x00-\x7f]")  # 日本語などを含む（search_screen.py と同じ）
# 会社の種類。顧客の一覧には「株式会社はまなす商事」とあっても、質問では「はまなす商事」と書くことが多い
_COMPANY_KINDS = (
    "株式会社|有限会社|合同会社|合資会社|合名会社|一般社団法人|公益社団法人|一般財団法人|公益財団法人|"
    "社会福祉法人|医療法人|学校法人|特定非営利活動法人|npo法人|\\(株\\)|\\(有\\)|\\(同\\)|\\(合\\)|\\(社\\)|\\(財\\)"
)
_COMPANY_WIDE = re.compile(rf"^(?:{_COMPANY_KINDS})\s*|\s*(?:{_COMPANY_KINDS})$")
_COMPANY_ASCII = re.compile(
    r"[\s,]*(?<![0-9a-z])(?:co\.?,?\s*ltd\.?|inc\.?|corp\.?|corporation|ltd\.?|limited|llc|k\.k\.|gmbh|plc)$"
)
_ASCII_WORD = re.compile(r"[0-9a-z]+")
_ALNUM = frozenset("0123456789abcdefghijklmnopqrstuvwxyz")


def _compact(text: str) -> str:
    return "".join(text.split())


# ---------- 一覧 ----------
def watch_variants(term: str) -> list[str]:
    """見張る言葉（normalize したもの）と、会社の種類（株式会社など）を除いた形。"""
    found = [term]
    if _WIDE.search(term):
        short = _COMPANY_WIDE.sub("", term).strip()
        if short != term and len(_compact(short)) >= _MIN_WATCH_SHORT:
            found.append(short)
    else:
        short = _COMPANY_ASCII.sub("", term).strip(" ,")
        if short != term and len(short) >= _MIN_WATCH_SHORT:
            found.append(short)
    return found


@dataclass(frozen=True)
class WatchTerms:
    """見張る言葉の一覧（顧客の名前など）。error があれば、一覧を読めなかった（すべての検索を確かめる）。"""

    wide: tuple[str, ...] = ()  # 日本語を含むもの（空白を除いて比べる）
    ascii: tuple[str, ...] = ()  # 英数字だけのもの（語の切れ目で比べる）
    error: str | None = None

    @classmethod
    def parse(cls, text: str) -> WatchTerms:
        """一覧のファイルの中身。1行に1つ。# で始まる行は説明。"""
        return cls.of(line for line in text.splitlines() if not normalize(line).startswith("#"))

    @classmethod
    def of(cls, values: Iterable[str]) -> WatchTerms:
        """値の並び（データベースの列の値など）から作る。短すぎるものと、長すぎるものは見張らない。"""
        wide: set[str] = set()
        plain: set[str] = set()
        for value in values:
            if len(value) > _MAX_WATCH_CHARS:
                continue
            for term in watch_variants(normalize(value)):
                if _WIDE.search(term):
                    if len(_compact(term)) >= _MIN_WATCH_WIDE:
                        wide.add(_compact(term))
                elif len(term) >= _MIN_WATCH_ASCII:
                    plain.add(term)
        return cls(tuple(sorted(wide)), tuple(sorted(plain)))

    @classmethod
    def merge(cls, parts: Iterable[WatchTerms]) -> WatchTerms:
        """いくつかの一覧を合わせる。どれかを読めなかったら、合わせたものも読めなかった扱い。"""
        parts = list(parts)
        errors = [part.error for part in parts if part.error]
        return cls(
            tuple(sorted({term for part in parts for term in part.wide})),
            tuple(sorted({term for part in parts for term in part.ascii})),
            "、".join(errors) or None,
        )

    def __len__(self) -> int:
        return len(self.wide) + len(self.ascii)

    def find(self, question: str) -> list[str]:
        """question（normalize したもの）に含まれる言葉。数万件あっても速いように、言葉の頭で引く。"""
        wide_index, ascii_index, ascii_rest = self._index
        compact = _compact(question)
        wide: set[str] = set()
        for i in range(len(compact) - 1):
            for term in wide_index.get(compact[i : i + 2], ()):
                if compact.startswith(term, i):
                    wide.add(term)
        plain: set[str] = set()
        for match in _ASCII_WORD.finditer(question):  # 語の頭（前の文字は英数字ではない）ごとに引く
            start = match.start()
            for term in ascii_index.get(match.group(), ()):
                end = start + len(term)
                if question.startswith(term, start) and (end == len(question) or question[end] not in _ALNUM):
                    plain.add(term)
        for term in ascii_rest:  # 英数字で始まらないもの（わずか）
            if re.search(rf"(?<![0-9a-z]){re.escape(term)}(?![0-9a-z])", question):
                plain.add(term)
        return sorted(wide) + sorted(plain)

    @cached_property
    def _index(self) -> tuple[dict[str, list[str]], dict[str, list[str]], list[str]]:
        wide: dict[str, list[str]] = {}
        for term in self.wide:
            wide.setdefault(term[:2], []).append(term)
        plain: dict[str, list[str]] = {}
        rest: list[str] = []
        for term in self.ascii:
            if match := _ASCII_WORD.match(term):
                plain.setdefault(match.group(), []).append(term)
            else:
                rest.append(term)
        return wide, plain, rest


_NO_TERMS = WatchTerms()  # 空の一覧（いつも同じものを返す。合わせた一覧を、作り直さずに使えるように）


class WatchTermsFile:
    """見張る言葉の一覧を、ファイルから読む。ファイルが変わったら、読み直す。ファイルがなければ、空の一覧。"""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._stamp: tuple[int, int] | None = None
        self._terms = _NO_TERMS

    def __call__(self) -> WatchTerms:
        if self.path is None:
            return _NO_TERMS
        try:
            stat = os.stat(self.path)
        except FileNotFoundError:
            self._stamp, self._terms = None, _NO_TERMS
            return self._terms
        except OSError as exc:
            return WatchTerms(error=f"ファイル: {type(exc).__name__}")
        stamp = (stat.st_mtime_ns, stat.st_size)
        if stamp != self._stamp:
            try:
                self._terms = WatchTerms.parse(self.path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError) as exc:
                return WatchTerms(error=f"ファイル: {type(exc).__name__}")
            self._stamp = stamp
        return self._terms


class WatchTermsError(Exception):
    """見張る言葉を読めなかった。メッセージは、そのまま画面に出す（値は含めない）。"""


class RefreshingTerms:
    """見張る言葉を、ファイルの外（データベース）から読む。間隔ごとに、裏で読み直す（検索を待たせない）。

    読み終える前と、読めなかったときは、error 付きの一覧を返す（すべての検索を確かめる）。
    読み直しに失敗したときは、前に読めた言葉も残して見張る。
    """

    def __init__(
        self,
        load: Callable[[], Iterable[str]],
        *,
        label: str,
        interval: float,
        retry: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._load = load
        self.label = label
        self.interval = interval
        self.retry = retry
        self._clock = clock
        self._terms = WatchTerms(error=f"{label}: まだ読み終えていません")
        self._next = 0.0  # 次に読み直す時刻
        self._lock = threading.Lock()
        self._running = False

    def __call__(self) -> WatchTerms:
        if self._clock() >= self._next:
            self.start()
        return self._terms

    def start(self) -> None:
        """裏で読み直す（読み直している途中なら、何もしない）。"""
        with self._lock:
            if self._running:
                return
            self._running = True
        threading.Thread(target=self._run, name="watch-terms", daemon=True).start()

    def refresh(self) -> WatchTerms:
        """いま読み直す（テストと、起動の確認に使う）。"""
        try:
            terms = WatchTerms.of(self._load())
            wait = self.interval
        except Exception as exc:  # 読めなかった理由が何であれ、すべての検索を確かめる側に倒す
            reason = str(exc) if isinstance(exc, WatchTermsError) else type(exc).__name__
            old = self._terms
            terms = WatchTerms(old.wide, old.ascii, f"{self.label}: {reason}")
            wait = self.retry
        self._terms = terms
        self._next = self._clock() + wait
        return terms

    def _run(self) -> None:
        try:
            self.refresh()
        finally:
            with self._lock:
                self._running = False


class CombinedTerms:
    """いくつかの見張る言葉の一覧（ファイルとデータベース）を、1つにして返す。"""

    def __init__(self, *sources: Callable[[], WatchTerms]) -> None:
        self.sources = sources
        self._last: tuple[tuple[WatchTerms, ...], WatchTerms] | None = None

    def __call__(self) -> WatchTerms:
        parts = tuple(source() for source in self.sources)
        last = self._last
        # どれも変わっていなければ、前に合わせたもの（引くための索引も作ってある）を使う
        if last is None or len(last[0]) != len(parts) or any(a is not b for a, b in zip(last[0], parts, strict=True)):
            last = self._last = (parts, WatchTerms.merge(parts))
        return last[1]


# ---------- データベースから読む ----------
# 見張る言葉を読むロールが、読むことを許された列。ロールに直接許したものだけ（すべてのロールへの許可は除く）
_WATCH_COLUMNS_SQL = """
SELECT table_schema, table_name, column_name
FROM information_schema.column_privileges
WHERE grantee = current_user AND privilege_type = 'SELECT'
  AND table_schema NOT IN ('pg_catalog', 'information_schema')
ORDER BY 1, 2, 3
"""


def screener_conninfo(settings: Settings, limits: DatabaseLimits) -> str | None:
    """見張る言葉（顧客の名前など）を読むロールの接続情報。DATA_DB_SCREENER_USER が未設定なら None。

    つなぐ先は、エージェントが読むデータベースと同じ（DATA_DB_HOST など）。ロールは別にする。
    """
    if not settings.data_db_screener_user:
        return None
    missing = [
        name
        for name, value in (
            ("DATA_DB_HOST", settings.data_db_host),
            ("DATA_DB_NAME", settings.data_db_name),
            ("DATA_DB_SCREENER_PASSWORD", settings.data_db_screener_password),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"DATA_DB_SCREENER_USER を設定した場合は、{'、'.join(missing)} も設定してください。")
    if settings.data_db_screener_user == settings.data_db_user:
        # 同じロールだと、エージェントも顧客の名前を読める（読めなくした列ではない）ことになる
        raise RuntimeError(
            "DATA_DB_SCREENER_USER には、DATA_DB_USER（エージェントのロール）とは別のロールを設定してください"
            "（scripts/setup_data_db.sh が作る coding_agent_screener）。"
        )
    timeout_ms = limits.statement_timeout_seconds * 1000
    return make_conninfo(
        host=settings.data_db_host,
        port=settings.data_db_port,
        dbname=settings.data_db_name,
        user=settings.data_db_screener_user,
        password=settings.data_db_screener_password.get_secret_value(),
        sslmode=settings.data_db_sslmode,
        connect_timeout=_CONNECT_TIMEOUT_SEC,
        application_name="data-agent-screen",
        options=f"-c default_transaction_read_only=on -c statement_timeout={timeout_ms} -c lock_timeout=5000",
    )


def load_watch_values(conninfo: str, max_values: int) -> list[str]:
    """見張る言葉を読むロールが読める列の、値（重なりは除く）。値の数が max_values を超えたら、失敗にする。

    どの列を読むかは、ロールの権限で決まる（scripts/setup_data_db.sh が、config/watch_columns.<名前>.txt の列を許す）。
    記録には、列と値の数だけを残す（値は残さない）。
    """
    started = time.monotonic()
    status = "error"
    try:
        with psycopg.connect(conninfo) as conn:
            conn.read_only = True
            row = conn.execute("SELECT rolsuper FROM pg_catalog.pg_roles WHERE rolname = current_user").fetchone()
            if row is None or row[0]:
                raise WatchTermsError("管理者のロールでは読みません（DATA_DB_SCREENER_USER）")
            columns = conn.execute(_WATCH_COLUMNS_SQL).fetchall()
            values: set[str] = set()
            for schema, table, column in columns:
                query = sql.SQL("SELECT DISTINCT {col}::text FROM {table} WHERE {col} IS NOT NULL LIMIT {limit}").format(
                    col=sql.Identifier(column), table=sql.Identifier(schema, table), limit=sql.Literal(max_values + 1)
                )
                values.update(value for (value,) in conn.execute(query))
                if len(values) > max_values:
                    status = "too_many"
                    raise WatchTermsError(f"値が多すぎます（上限 {max_values} 件。web_search.watch_terms_max）")
    except Exception as exc:
        audit("watch_terms_load", status=status, error=type(exc).__name__)
        raise
    audit(
        "watch_terms_load",
        status="ok",
        columns=len(columns),
        values=len(values),
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    return sorted(values)


# ---------- 設定から作る ----------
@lru_cache
def from_settings() -> Callable[[], WatchTerms]:
    """見張る言葉（顧客の名前など）の一覧。ファイル（変わったら読み直す）と、データベースの列（間隔ごとに読み直す）。

    ファイルは、エージェントから読める場所には置かせない。データベースは、.env に DATA_DB_SCREENER_USER があるときだけ。
    """
    settings, security = get_settings(), get_security()
    name = security.web_search.watch_terms_file
    file = WatchTermsFile(None)
    if name:
        resolved = (Path(name) if Path(name).is_absolute() else PROJECT_ROOT / name).resolve()
        for visible in (settings.workspace_dir, PROJECT_ROOT / "memories", PROJECT_ROOT / "skills"):
            if resolved.is_relative_to(Path(visible).resolve()):
                raise ValueError(
                    f"web_search.watch_terms_file を、エージェントから読める場所（{visible}）に置かないでください: {name}"
                )
        file = WatchTermsFile(resolved)
    if (conninfo := screener_conninfo(settings, security.database)) is None:
        return file
    limit = security.web_search.watch_terms_max
    from_db = RefreshingTerms(
        lambda: load_watch_values(conninfo, limit),
        label="データベース",
        interval=security.web_search.watch_terms_refresh_minutes * 60,
    )
    from_db.start()  # 最初の検索までに読み終えるように、すぐ読み始める
    return CombinedTerms(file, from_db)
