"""データベースを読むツール（database.py）の、データベースなしで確かめられる部分。

実際の PostgreSQL を使う確認（読み取り専用、複数の文の拒否、時間切れなど）は、tests/e2e/check_database.py で行う。
"""

from __future__ import annotations

import asyncio
import datetime
import decimal
from pathlib import Path

import pytest
from deepagents.backends import FilesystemBackend
from pydantic import SecretStr, ValidationError
from test_approvals import PERMISSIONS as RULES

from data_agent.safety import watch_terms
from data_agent.safety.guardrails import DEFAULT_ACTIONS, Guard
from data_agent.settings import DatabaseLimits, Settings
from data_agent.tools import database
from data_agent.tools.database import Database, DatabaseError, QueryResult

KEY = "sk-ant-api03-TEST0000TEST0000TEST0000"  # 形だけを合わせた偽物


class FakeDatabase(Database):
    """データベースとのやりとりだけを偽物にしたもの。"""

    def __init__(self, workspace: Path, result: QueryResult | DatabaseError, **limits: object) -> None:
        backend = FilesystemBackend(root_dir=workspace, virtual_mode=True)
        super().__init__("host=unused", backend, RULES, DatabaseLimits(**limits), Guard(DEFAULT_ACTIONS, enabled=True))
        self.result = result
        self.sent: list[str] = []

    async def _fetch(self, sql: str, max_rows: int, max_bytes: int) -> QueryResult:
        self.sent.append(sql)
        if isinstance(self.result, DatabaseError):
            raise self.result
        return self.result


def _result(rows: list[list[str]], truncated: str | None = None) -> QueryResult:
    return QueryResult([("region", "text"), ("amount", "int4")], rows, truncated, 210)


def _files(workspace: Path) -> list[Path]:
    return sorted(p for p in workspace.rglob("*") if p.is_file())


@pytest.mark.parametrize(
    ("sql", "keyword"),
    [
        ("SELECT 1", "select"),
        ("  \n-- 説明\n/* 説明 */ (select 1) union (select 2)", "select"),
        ("with t as (select 1) select * from t", "with"),
        ("DELETE FROM orders", "delete"),
        ("-- select\nDROP TABLE orders", "drop"),
        ("/* /* 入れ子 */ */ select 1", ""),  # 入れ子のコメントは読み解かず、受け付けない側に倒す
        ("", ""),
    ],
)
def test_first_keyword(sql: str, keyword: str) -> None:
    assert database.first_keyword(sql) == keyword


def test_cell_text() -> None:
    assert database.cell_text(None) == ""
    assert database.cell_text(True) == "true"
    assert database.cell_text(decimal.Decimal("12.50")) == "12.50"
    assert database.cell_text(datetime.date(2026, 11, 14)) == "2026-11-14"
    assert database.cell_text({"名前": "山田"}) == '{"名前": "山田"}'
    assert database.cell_text(b"\x00\x01") == "（バイナリ 2 バイト）"


def test_safe_name() -> None:
    assert database.safe_name("monthly_sales") == "monthly_sales"
    assert database.safe_name("月ごとの売上.csv") == "月ごとの売上"
    assert database.safe_name("../../etc/passwd") == "etc_passwd"
    assert database.safe_name("///") == "query"


def test_query_saves_csv_and_returns_preview(tmp_path: Path) -> None:
    rows = [[f"地域{n}", str(n * 10)] for n in range(50)]
    db = FakeDatabase(tmp_path, _result(rows), preview_rows=3)
    text = asyncio.run(db.query("SELECT region, amount FROM sales", "sales"))

    saved = _files(tmp_path)
    assert [p.name for p in saved] == ["sales.csv"]
    lines = saved[0].read_text(encoding="utf-8").splitlines()
    assert lines[0] == "region,amount" and len(lines) == 51  # 全部の行が保存される
    assert "50 行、2 列" in text
    assert f"保存先: /outputs/{saved[0].parent.name}/sales.csv" in text
    assert "列: region（text）、amount（int4）" in text
    assert "地域2,20" in text and "地域3,30" not in text  # モデルに見せるのは、先頭の3行だけ


def test_only_read_statements_are_sent(tmp_path: Path) -> None:
    db = FakeDatabase(tmp_path, _result([]))
    for sql in (
        "DELETE FROM orders",
        "UPDATE orders SET amount = 0",
        "DROP TABLE orders",
        "COPY orders TO STDOUT",
        "EXPLAIN ANALYZE DELETE FROM orders",
        "SHOW server_version",
    ):
        assert "読み取りの文" in asyncio.run(db.query(sql))
    assert db.sent == [] and _files(tmp_path) == []


def test_truncation_is_reported(tmp_path: Path) -> None:
    text = asyncio.run(FakeDatabase(tmp_path, _result([["a", "1"]], "rows"), max_rows=1).query("SELECT 1"))
    assert "上限の 1 行で打ち切りました" in text
    text = asyncio.run(FakeDatabase(tmp_path, _result([["a", "1"]], "size")).query("SELECT 1"))
    assert "10 MB を超えたところで打ち切りました" in text


def test_error_message_is_returned(tmp_path: Path) -> None:
    db = FakeDatabase(tmp_path, DatabaseError('問い合わせに失敗しました: relation "x" does not exist'))
    assert 'relation "x" does not exist' in asyncio.run(db.query("SELECT * FROM x"))
    assert _files(tmp_path) == []


def test_email_and_phone_are_masked_in_preview_source_and_saved_csv(tmp_path: Path) -> None:
    # 読めなくし忘れた列に、メールアドレスや電話番号が入っていたときの、もう一段の守り
    db = FakeDatabase(tmp_path, _result([["taro@example.com", "090-1234-5678"], ["大阪", "80"]]))
    asyncio.run(db.query("SELECT region, amount FROM sales"))
    saved = _files(tmp_path)[0].read_text(encoding="utf-8")
    assert "taro@example.com" not in saved and "090-1234-5678" not in saved
    assert "[マスク済み:メールアドレス],[マスク済み:電話番号]" in saved and "大阪,80" in saved


def test_secrets_are_masked_in_saved_csv(tmp_path: Path) -> None:
    db = FakeDatabase(tmp_path, _result([["東京", KEY], ["大阪", "80"]]))
    asyncio.run(db.query("SELECT region, amount FROM sales"))
    saved = _files(tmp_path)[0].read_text(encoding="utf-8")
    assert KEY not in saved and "[マスク済み:秘密情報]" in saved and "大阪,80" in saved


def test_long_values_are_shortened_only_in_preview(tmp_path: Path) -> None:
    long = "あ" * 500
    text = asyncio.run(FakeDatabase(tmp_path, _result([[long, "1"]])).query("SELECT 1"))
    assert long not in text and "あ" * 200 + "…" in text
    assert long in _files(tmp_path)[0].read_text(encoding="utf-8")


def test_tables_lists_columns_and_filters(tmp_path: Path) -> None:
    rows = [
        ["public", "orders", "r", "2007", "注文", "order_id", "integer", "", "true"],
        ["public", "orders", "r", "2007", "注文", "amount", "integer", "金額（円）", "true"],
        ["public", "v_sales", "v", "-1", "", "region", "text", "", "true"],
    ]
    db = FakeDatabase(tmp_path, QueryResult([], rows, None, 5))
    text = asyncio.run(db.tables())
    assert "public.orders（テーブル、約 2,007 行、2 列） — 注文" in text
    assert "  amount integer — 金額（円）" in text
    assert "public.v_sales（ビュー、1 列）" in text
    assert "読めない" not in text

    only = asyncio.run(db.tables("PUBLIC.orders"))
    assert "public.orders" in only and "v_sales" not in only
    assert asyncio.run(db.tables("nothing")) == "表 nothing は見つかりませんでした。"


def test_tables_marks_columns_that_cannot_be_read(tmp_path: Path) -> None:
    # 個人情報の列は、データベースの側で読めなくしてある。型や説明は出さず、「読めない」とだけ示す
    rows = [
        ["public", "customers", "r", "20", "顧客", "customer_id", "integer", "", "true"],
        ["public", "customers", "r", "20", "顧客", "customer_name", "text", "氏名", "false"],
        ["public", "customers", "r", "20", "顧客", "region", "text", "地域", "true"],
        ["public", "customers", "r", "20", "顧客", "email", "text", "", "false"],
    ]
    text = asyncio.run(FakeDatabase(tmp_path, QueryResult([], rows, None, 5)).tables())
    assert "public.customers（テーブル、約 20 行、4 列。うち 2 列は読めない） — 顧客" in text
    assert "  customer_name — 読めない" in text and "  email — 読めない" in text
    assert "  region text — 地域" in text
    assert "氏名" not in text  # 読めない列の説明は出さない
    assert "SELECT * も使えない" in text


def test_tables_falls_back_to_names_when_too_many(tmp_path: Path) -> None:
    rows = [["public", f"table_{t}", "r", "10", "", f"column_{c}", "text", "", "true"] for t in range(200) for c in range(10)]
    text = asyncio.run(FakeDatabase(tmp_path, QueryResult([], rows, None, 5)).tables())
    assert text.startswith("表が多いので、名前だけを返します。")
    assert "public.table_0（テーブル、約 10 行、10 列）" in text and "column_0" not in text


def test_conninfo_requires_all_items(monkeypatch: pytest.MonkeyPatch) -> None:
    # settings は、読み込まれるときに .env を環境変数に入れる。手元の .env の値が混ざらないようにする
    for name in ("DATA_DB_HOST", "DATA_DB_PORT", "DATA_DB_NAME", "DATA_DB_USER", "DATA_DB_PASSWORD", "DATA_DB_SSLMODE"):
        monkeypatch.delenv(name, raising=False)
    limits = DatabaseLimits()
    assert database.conninfo(Settings(_env_file=None, data_db_host=None), limits) is None
    with pytest.raises(RuntimeError, match="DATA_DB_USER、DATA_DB_PASSWORD"):
        database.conninfo(Settings(_env_file=None, data_db_host="db", data_db_name="sample"), limits)
    info = database.conninfo(
        Settings(
            _env_file=None, data_db_host="db", data_db_name="sample", data_db_user="reader", data_db_password=SecretStr("pw")
        ),
        limits,
    )
    assert "default_transaction_read_only=on" in info and "statement_timeout=30000" in info


def test_screener_conninfo(monkeypatch: pytest.MonkeyPatch) -> None:
    """見張る言葉を読むロール。エージェントのロールとは別にする。"""
    for name in ("DATA_DB_HOST", "DATA_DB_NAME", "DATA_DB_USER", "DATA_DB_SCREENER_USER", "DATA_DB_SCREENER_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    limits = DatabaseLimits()
    base = {"_env_file": None, "data_db_host": "db", "data_db_name": "sample", "data_db_user": "reader"}
    assert watch_terms.screener_conninfo(Settings(**base), limits) is None
    with pytest.raises(RuntimeError, match="DATA_DB_SCREENER_PASSWORD"):
        watch_terms.screener_conninfo(Settings(**base, data_db_screener_user="screener"), limits)
    with pytest.raises(RuntimeError, match="別のロール"):
        watch_terms.screener_conninfo(
            Settings(**base, data_db_screener_user="reader", data_db_screener_password=SecretStr("pw")), limits
        )
    info = watch_terms.screener_conninfo(
        Settings(**base, data_db_screener_user="screener", data_db_screener_password=SecretStr("pw")), limits
    )
    assert "user=screener" in info and "dbname=sample" in info and "default_transaction_read_only=on" in info


def test_output_dir_must_be_inside_workspace() -> None:
    with pytest.raises(ValidationError):
        DatabaseLimits(output_dir="../outside")
