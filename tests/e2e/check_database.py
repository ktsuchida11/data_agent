"""データベースを読むツールと Web 検索を、本物の PostgreSQL と、画面と同じ通信で確かめる。

tests/e2e/fake_app.py で起動したアプリのコンテナの中で実行する。
- 使い捨ての PostgreSQL に、scripts/setup_data_db.sh --sample で練習用のデータと読み取り専用のロールを作り、
  アプリに DATA_DB_* を渡しておく。顧客の名前を見張る言葉として読むロール（DATA_DB_SCREENER_*）も、
  同じスクリプトが作る（ADR-0022）。
- direct の確認には、管理者のロールでつなぐ確認が1つある。使い捨ての PostgreSQL に管理者のロールを作り、
  E2E_SUPER_USER と E2E_SUPER_PASSWORD で渡す（渡さなければ、その確認は飛ばす）。

    python tests/e2e/check_database.py direct   # ツールの中身を直接呼んで、守りと上限を確かめる
    python tests/e2e/check_database.py chat     # 画面と同じ通信で、ツールとして使えることを確かめる
    python tests/e2e/check_database.py approval # database と web_search の require_approval を true にして起動したとき

chat は、チャットが1件もない状態から始めること。環境変数 E2E_PASSWORD にログインのパスワードを渡す。
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
import time
from pathlib import Path

import psycopg
from deepagents.backends import FilesystemBackend

from data_agent.safety import watch_terms
from data_agent.safety.guardrails import DEFAULT_ACTIONS, Guard
from data_agent.settings import DatabaseLimits, get_settings
from data_agent.tools import database
from data_agent.tools.database import Database

WORKSPACE = Path("/app/workspace")
SECRET_MASK = "[マスク済み:秘密情報]"
ORDERS = 2007  # 練習用のデータの注文の数（669 日 × 3 件）


def check(label: str, ok: bool, detail: object = "") -> None:
    print(f"{'OK  ' if ok else 'FAIL'} {label}" + (f": {detail}" if detail != "" else ""))
    if not ok:
        raise SystemExit(1)


def _database(root: Path, **limits: object) -> Database:
    config = DatabaseLimits(**limits)
    backend = FilesystemBackend(root_dir=root, virtual_mode=True)
    return Database(database.conninfo(get_settings(), config), backend, [], config, Guard(DEFAULT_ACTIONS, enabled=True))


def _saved(root: Path) -> list[Path]:
    return sorted((p for p in root.rglob("*.csv")), key=lambda p: p.stat().st_mtime)


async def _count_orders() -> int:
    """読み取り専用のロールで、注文の数を数える（ツールを通さずに確かめる）。"""
    settings = get_settings()
    async with await psycopg.AsyncConnection.connect(
        host=settings.data_db_host,
        port=settings.data_db_port,
        dbname=settings.data_db_name,
        user=settings.data_db_user,
        password=settings.data_db_password.get_secret_value(),
    ) as conn:
        return (await (await conn.execute("SELECT count(*) FROM orders")).fetchone())[0]


async def direct() -> None:
    root = Path(tempfile.mkdtemp())
    db = _database(root)

    # ---------- 表の一覧 ----------
    text = await db.tables()
    check("表の一覧に、表の説明と行数の見積もりが出る", f"public.orders（テーブル、約 {ORDERS:,} 行、6 列） — 注文" in text, text)
    check("列の型と説明が出る", "  amount integer — 金額（円）。数量 × 単価" in text, text)
    check("3つの表が出る", all(f"public.{name}" in text for name in ("products", "customers", "orders")), text)
    only = await db.tables("products")
    check("表を指定すると、その表だけが出る", "public.products" in only and "public.orders" not in only, only)
    # 練習用のデータでは、顧客の email を、データベースの側で読めなくしてある（config/hidden_columns.coding_agent_sample.txt）。
    # 顧客の名前（name）は読める（ADR-0023。Web 検索に入れようとすると確かめる。ADR-0022）
    check(
        "読めない列がある表は、その数が出る",
        "public.customers（テーブル、約 20 行、5 列。うち 1 列は読めない） — 顧客" in text,
        text,
    )
    check(
        "読めない列は、名前と「読めない」だけが出る",
        "  email — 読めない" in text
        and "  name text" in text
        and "  name — 読めない" not in text
        and "  region text — 地域" in text,
        text,
    )
    check("読めない列がない表には、注意書きが出ない", "読めない" not in only, only)

    # ---------- 問い合わせと保存 ----------
    text = await db.query("SELECT region, count(*) AS customers FROM customers GROUP BY region ORDER BY region", "by_region")
    check("集計の結果が返る", "5 行、2 列" in text and "列: region（text）、customers（int8）" in text, text)
    csv = _saved(root)[-1]
    check("結果が CSV に保存される", csv.name == "by_region.csv" and csv.read_text().splitlines()[0] == "region,customers", csv)
    check("保存先がモデルに伝わる", f"保存先: /outputs/{csv.parent.name}/by_region.csv" in text, text)
    check("日本語の値がそのまま入る", "東京,4" in csv.read_text() and "東京,4" in text, csv.read_text())

    text = await db.query("SELECT * FROM orders ORDER BY order_id")
    lines = _saved(root)[-1].read_text().splitlines()
    check(
        "全部の行が CSV に入り、モデルには先頭だけが渡る",
        len(lines) == ORDERS + 1 and f"{ORDERS} 行、6 列" in text and text.count("\n") < 40,
        text[:200],
    )

    text = await db.query(
        """SELECT now() AS t, 1.50::numeric AS n, '{"名前": "山田"}'::jsonb AS j, ARRAY[1, 2] AS a, NULL AS x, true AS b, 'ab'::bytea AS raw"""
    )
    row = _saved(root)[-1].read_text().splitlines()[1]
    check("いろいろな型の値を CSV にできる", '1.50,"{""名前"": ""山田""}","[1, 2]",,true,（バイナリ 2 バイト）' in row, row)
    check("型の名前が伝わる", "t（timestamptz）、n（numeric）、j（jsonb）、a（int4[]）" in text, text)

    text = await db.query("SELECT * FROM orders WHERE false")
    check(
        "0 行でも、列の名前が分かる",
        "0 行、6 列" in text and _saved(root)[-1].read_text() == "order_id,order_date,customer_id,product_id,quantity,amount\n",
        text,
    )
    text = await db.query("WITH big AS (SELECT * FROM orders WHERE amount >= 5000) SELECT count(*) AS n FROM big")
    check("WITH … SELECT と TABLE も使える", "1 行、1 列" in text and "8 行、4 列" in await db.query("TABLE products"), text)
    text = await db.query("SELECT current_setting('statement_timeout') AS t, current_setting('transaction_read_only') AS ro")
    check("時間の上限と読み取り専用が、接続に効いている", "\n30s,on" in text, text)

    # ---------- 上限 ----------
    small = _database(root, max_rows=100)
    text = await small.query("SELECT * FROM orders")
    check("行数の上限で打ち切る", "100 行、6 列" in text and "上限の 100 行で打ち切りました" in text, text[:200])
    check("打ち切ったあとも、次の問い合わせができる", "1 行、1 列" in await small.query("SELECT 1 AS one"))
    text = await _database(root, max_csv_mb=1).query("SELECT repeat('x', 1000) AS filler FROM generate_series(1, 5000)")
    check("大きさの上限で打ち切る", "1 MB を超えたところで打ち切りました" in text, text[:200])
    started = time.monotonic()
    text = await _database(root, statement_timeout_seconds=2).query("SELECT pg_sleep(20)")
    check("時間の上限で止まる", "時間切れで止めました（2 秒）" in text and time.monotonic() - started < 8, text)

    # ---------- 守り ----------
    before = len(_saved(root))
    for sql in (
        "DELETE FROM orders",
        "INSERT INTO products (name, category, unit_price) VALUES ('x', 'y', 1)",
        "DROP TABLE orders",
        "TRUNCATE orders",
    ):
        check(f"書き込みの文は送らない（{sql.split()[0]}）", "読み取りの文" in await db.query(sql))
    text = await db.query("SELECT 1; DELETE FROM orders")
    check("複数の文は、データベースが受け付けない", "multiple commands" in text, text)
    for label, sql in (
        ("WITH の中の DELETE", "WITH gone AS (DELETE FROM orders RETURNING 1) SELECT count(*) FROM gone"),
        ("SELECT INTO", "SELECT * INTO orders_copy FROM orders"),
        ("番号を進める関数", "SELECT nextval('orders_order_id_seq')"),
        ("行のロック", "SELECT * FROM orders FOR UPDATE"),
    ):
        text = await db.query(sql)
        check(f"読み取りの形をした書き込みは、データベースが断る（{label}）", "問い合わせに失敗しました" in text, text)
        print("      →", text)
    await db.query("SELECT set_config('default_transaction_read_only', 'off', false)")
    text = await db.query("SELECT current_setting('transaction_read_only') AS ro")
    check("読み取り専用の設定を外そうとしても、次の問い合わせには効かない", "\non" in text, text)
    check("守りに引っかかった問い合わせでは、ファイルが増えていない", len(_saved(root)) == before + 2, len(_saved(root)) - before)
    check("注文は1件も減っていない", await _count_orders() == ORDERS)

    # ツールの守りを通さなくても、ロールそのものが書き込めない（これが本当の守り）
    settings = get_settings()
    conn = await psycopg.AsyncConnection.connect(
        host=settings.data_db_host,
        port=settings.data_db_port,
        dbname=settings.data_db_name,
        user=settings.data_db_user,
        password=settings.data_db_password.get_secret_value(),
        autocommit=True,
    )
    try:
        await conn.execute("SET default_transaction_read_only = off")
        await conn.execute("DELETE FROM orders")
        denied = ""
    except psycopg.Error as exc:
        denied = str(exc)
    finally:
        await conn.close()
    check("ロールに書き込みの権限がない", "permission denied for table orders" in denied, denied)

    text = await db.query("SELECT 'sk-ant-api03-' || repeat('DB000000', 3) AS key, 'ok' AS note")
    saved = _saved(root)[-1].read_text()
    check(
        "秘密情報らしい値は、マスクしてから保存する",
        SECRET_MASK in saved and "DB000000DB000000" not in saved and "ok" in saved,
        saved,
    )

    text = await db.query("SELECT * FROM nothing")
    check("SQL の誤りは、データベースのメッセージで伝わる", 'relation "nothing" does not exist' in text, text)

    # ---------- 読めなくしてある列（個人情報） ----------
    count = len(_saved(root))
    for label, sql in (
        ("列を書く", "SELECT email FROM customers"),
        ("SELECT *", "SELECT * FROM customers"),
        ("式の中", "SELECT upper(email) FROM customers"),
        ("条件", "SELECT customer_id FROM customers WHERE email LIKE 'customer1@%'"),
        ("行の全体", "SELECT row_to_json(c) FROM customers c"),
        ("副問い合わせ", "SELECT (SELECT c.email FROM customers c WHERE c.customer_id = o.customer_id) FROM orders o LIMIT 1"),
    ):
        text = await db.query(sql)
        check(
            f"読めない列を使う問い合わせは、断られる（{label}）",
            "読み取りを止めてある表か列" in text
            and "permission denied for table customers" in text
            and "@example.com" not in text,
            text,
        )
    check("断られた問い合わせでは、ファイルが作られない", len(_saved(root)) == count, len(_saved(root)) - count)
    text = await db.query("SELECT customer_id, name, region FROM customers ORDER BY customer_id LIMIT 3", "names")
    check("顧客の名前は読める（ADR-0023）", "3 行、3 列" in text and "山田 太郎" in text, text)
    text = await db.query(
        "SELECT c.region, sum(o.amount) AS amount FROM orders o JOIN customers c USING (customer_id) GROUP BY c.region ORDER BY 1",
        "amount_by_region",
    )
    check("読める列だけなら、同じ表を使って集計できる", "5 行、2 列" in text, text)
    text = await db.query("SELECT 'taro@example.com' AS mail, '03(1234)5678' AS tel, '東京' AS region", "contacts")
    saved = next(p for p in _saved(root) if p.name == "contacts.csv").read_text()
    check(
        "メールアドレスと電話番号は、マスクしてから保存する",
        "[マスク済み:メールアドレス],[マスク済み:電話番号],東京" in saved and "taro@" not in saved,
        saved,
    )

    # ---------- 接続 ----------
    if os.environ.get("E2E_SUPER_USER"):
        admin = get_settings().model_copy(
            update={"data_db_user": os.environ["E2E_SUPER_USER"], "data_db_password": _secret(os.environ["E2E_SUPER_PASSWORD"])}
        )
        config = DatabaseLimits()
        backend = FilesystemBackend(root_dir=root, virtual_mode=True)
        as_admin = Database(database.conninfo(admin, config), backend, [], config, Guard(DEFAULT_ACTIONS, enabled=True))
        text = await as_admin.query("SELECT 1")
        check("管理者のロールでは、問い合わせを受け付けない", "管理者（スーパーユーザー）のロールでは" in text, text)
        # すべての表を読めるロール（pg_read_all_data）に入っているロールも断る（列ごとに読めなくした設定が効かないため）
        text = await _query_as_broad_role(admin, config, backend)
        check(
            "すべての表を読めるロールに入っているロールでは、問い合わせを受け付けない", "すべての表を読めるロール" in text, text
        )
    else:
        print("SKIP 管理者のロールでの確認（E2E_SUPER_USER がない）")
    wrong = get_settings().model_copy(update={"data_db_password": _secret("wrong-password")})
    config = DatabaseLimits()
    bad = Database(
        database.conninfo(wrong, config),
        FilesystemBackend(root_dir=root, virtual_mode=True),
        [],
        config,
        Guard(DEFAULT_ACTIONS, enabled=True),
    )
    text = await bad.query("SELECT 1")
    check(
        "つながらないときは、理由が伝わる（パスワードは出ない）",
        "接続できませんでした" in text and "wrong-password" not in text,
        text,
    )

    # ---------- 見張る言葉を読むロール（ADR-0022） ----------
    screener = watch_terms.screener_conninfo(get_settings(), DatabaseLimits())
    check("見張る言葉を読むロールが、設定されている", screener is not None)
    values = watch_terms.load_watch_values(screener, 100_000)
    check("顧客の名前を、見張る言葉として、見張る用のロールで読める", len(values) == 20 and "山田 太郎" in values, len(values))
    check("読むのは、一覧に書いた列だけ（メールアドレスや地域は読まない）", not any("@" in v or v == "東京" for v in values))
    for sql in ("SELECT email FROM customers", "SELECT amount FROM orders", "DELETE FROM customers"):
        try:
            with psycopg.connect(screener) as conn:
                conn.execute(sql)
            refused = ""
        except psycopg.Error as exc:
            refused = type(exc).__name__
        check(f"見張る言葉を読むロールでは、ほかの列を読めず、書き込めない（{sql.split()[1]}）", bool(refused), refused)
    print("DONE")


async def _query_as_broad_role(admin, config: DatabaseLimits, backend: FilesystemBackend) -> str:
    """pg_read_all_data に入った、使い捨てのロールでつないで問い合わせる。終わったら、ロールを消す。"""
    from psycopg import sql

    name, password = "e2e_broad_reader", f"e2e-{os.urandom(8).hex()}"
    async with await psycopg.AsyncConnection.connect(database.conninfo(admin, config), autocommit=True) as conn:
        await conn.execute("SET default_transaction_read_only = off")  # 接続の設定は、読み取りだけになっている
        await conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name)))
        await conn.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} IN ROLE pg_read_all_data").format(
                sql.Identifier(name), sql.Literal(password)
            )
        )
        await conn.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(admin.data_db_name), sql.Identifier(name))
        )
    try:
        broad = get_settings().model_copy(update={"data_db_user": name, "data_db_password": _secret(password)})
        guard = Guard(DEFAULT_ACTIONS, enabled=True)
        return await Database(database.conninfo(broad, config), backend, [], config, guard).query("SELECT 1")
    finally:
        async with await psycopg.AsyncConnection.connect(database.conninfo(admin, config), autocommit=True) as conn:
            await conn.execute("SET default_transaction_read_only = off")  # 接続の設定は、読み取りだけになっている
            await conn.execute(
                sql.SQL("REVOKE CONNECT ON DATABASE {} FROM {}").format(sql.Identifier(admin.data_db_name), sql.Identifier(name))
            )
            await conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name)))


def _secret(value: str):
    from pydantic import SecretStr

    return SecretStr(value)


def _outputs() -> list[str]:
    root = WORKSPACE / "outputs"
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()) if root.exists() else []


async def chat() -> None:
    from check_chat_history import Chat, list_threads, login

    cookie = login()
    check("最初は一覧が空", list_threads(cookie) == [])
    talk = Chat()
    await talk.open(cookie)

    # ---------- データベース ----------
    reply = await talk.say("表を見せて")
    check("表の一覧が、ツールの結果としてモデルに届く", "public.orders（テーブル" in reply and "public.customers" in reply, reply)
    reply = await talk.say("問い合わせて SELECT region, count(*) AS customers FROM customers GROUP BY region ORDER BY region")
    check("問い合わせの結果がモデルに届く", "5 行、2 列" in reply and "東京,4" in reply, reply)
    match = re.search(r"保存先: (/outputs/\S+?\.csv)", reply)
    check(
        "結果の CSV が、作業フォルダに保存される",
        bool(match) and match.group(1).removeprefix("/outputs/") in _outputs(),
        (reply, _outputs()),
    )
    reply = await talk.say(
        f"入力付きで実行して {match.group(1)} import pandas as pd; print('合計', pd.read_csv('query.csv')['customers'].sum())"
    )
    check("保存された CSV を、コードの実行に渡して集計できる", "合計 20" in reply, reply)

    before = _outputs()
    reply = await talk.say("問い合わせて DELETE FROM orders")
    check("書き込みの文は実行されない", "読み取りの文" in reply, reply)
    reply = await talk.say("問い合わせて WITH gone AS (DELETE FROM orders RETURNING 1) SELECT count(*) FROM gone")
    check("読み取りの形をした書き込みも実行されない", "問い合わせに失敗しました" in reply, reply)
    reply = await talk.say("問い合わせて SELECT count(*) AS n FROM orders")
    check("注文は減っていない", f"\n{ORDERS}" in reply, reply)
    reply = await talk.say("問い合わせて SELECT 'sk-ant-api03-' || repeat('CHAT0000', 3) AS key")
    check("結果に出たキーは、モデルに渡る前にマスクされる", SECRET_MASK in reply and "CHAT0000CHAT0000" not in reply, reply)
    newest = (WORKSPACE / "outputs" / sorted(set(_outputs()) - set(before))[-1]).read_text()
    check("保存された CSV でも、マスクされている", SECRET_MASK in newest and "CHAT0000" not in newest, newest)

    # 個人情報。メールアドレスの列は、データベースの側で読めなくしてある。顧客の名前は読める（ADR-0023）
    reply = await talk.say("表を見せて customers")
    check("読めない列は、表の一覧に「読めない」と出る", "email — 読めない" in reply and "name — 読めない" not in reply, reply)
    before = _outputs()
    reply = await talk.say("問い合わせて SELECT name, email FROM customers")
    check(
        "読めない列（メールアドレス）は、問い合わせても取れない",
        "読み取りを止めてある表か列" in reply and "@example.com" not in reply and _outputs() == before,
        reply,
    )
    reply = await talk.say("問い合わせて SELECT name, region FROM customers ORDER BY customer_id LIMIT 3")
    check("顧客の名前は、問い合わせで取れる", "3 行、2 列" in reply and "山田 太郎" in reply, reply)
    # 読めなくし忘れた列に入っていた場合の、もう一段の守り（依頼文そのものがマスクされないよう、SQL の中で組み立てる）
    reply = await talk.say("問い合わせて SELECT 'taro' || '@' || 'example.com' AS mail, '03(' || '1234)5678' AS tel")
    check(
        "結果に出たメールアドレスと電話番号は、モデルに渡る前にマスクされる",
        "[マスク済み:メールアドレス]" in reply
        and "[マスク済み:電話番号]" in reply
        and "taro@example.com" not in reply
        and "03(1234)5678" not in reply,
        reply,
    )
    newest = (WORKSPACE / "outputs" / sorted(set(_outputs()) - set(before))[-1]).read_text()
    check(
        "保存された CSV でも、マスクされている（メールアドレスと電話番号）",
        "taro@example.com" not in newest and "[マスク済み:電話番号]" in newest,
        newest,
    )

    reply = await talk.say("汎用役に頼んで 問い合わせて SELECT count(*) AS n FROM products")
    check("汎用のサブエージェントも、問い合わせができる", "1 行、1 列" in reply, reply)
    reply = await talk.say("探索役に頼んで 問い合わせて SELECT count(*) AS n FROM products")
    check("調べるだけのサブエージェントは、問い合わせができない", "1 行、1 列" not in reply and "query_database" in reply, reply)

    # ---------- Web 検索（検索する役も、偽のモデル） ----------
    reply = await talk.say("検索して 今日の天気")
    check(
        "検索する役のまとめが、ツールの結果としてモデルに届く",
        "ツールの結果: （Web 検索の結果です。" in reply and "偽モデルの返事 1 通目（claude-sonnet-5-5）" in reply,
        reply,
    )
    check("検索する役の文章は、チャットの返事としては流れない", len(talk.assistant) == 1, talk.assistant)
    # データベースの結果の写しを、モデルが自分から検索に書こうとすると、検索の前に確かめる（ADR-0022）。
    # コードは SQL の中で組み立てる（利用者の発言に、そのままの形で入らないように）
    await talk.say("問い合わせて SELECT 'はまなす物産' AS company, 'QX-' || '4471' AS code")
    talk.ask_decision = "reject"
    reply = await talk.say("前の結果を検索して")
    asked = talk.asks[-1]["content"] if talk.asks else ""
    check(
        "データベースの結果の写しを検索しようとすると、確かめられる",
        "見つかったもの" in asked
        and "qx-4471" in asked.split("見つかったもの:")[-1]
        and "query_database" in asked
        and "許可しませんでした" in reply,
        (asked, reply),
    )
    # 問い合わせで読んだ顧客の名前を、モデルが自分から検索に書こうとすると、確かめる（ADR-0023、ADR-0022）
    await talk.say("問い合わせて SELECT name FROM customers WHERE customer_id IN (2, 3) ORDER BY customer_id")
    asked_before = len(talk.asks)
    reply = await talk.say("前の結果を検索して")
    asks = talk.asks[asked_before:]
    found = (asks[0]["content"] if asks else "").split("見つかったもの:")[-1]
    check(
        "問い合わせで読んだ顧客の名前を、モデルが検索に書こうとすると、確かめられる",
        len(asks) == 1 and "佐藤花子" in found.replace(" ", "") and "許可しませんでした" in reply,
        (found, reply),
    )
    # 顧客の名前は、見張る言葉としても、別のロールで読んである。利用者が自分で書いても、検索の前に確かめる（ADR-0022）
    asked_before = len(talk.asks)
    reply = await talk.say("検索して 山田 太郎 経歴")
    asks = talk.asks[asked_before:]
    asked = asks[0]["content"] if asks else ""
    check(
        "データベースの顧客の名前を検索しようとすると、確かめられる",
        len(asks) == 1 and "`山田太郎`" in asked and "見張る言葉の一覧" in asked and "許可しませんでした" in reply,
        (asked, reply),
    )
    talk.ask_decision = None
    reply = await talk.say("検索して 高橋 経歴")
    check("名字だけなら、確かめずに検索する", len(talk.asks) == asked_before + 1 and "Web 検索の結果" in reply, reply)
    reply = await talk.say("鍵を検索して")
    check("モデルが秘密情報らしい文字列を検索に入れようとしても、検索しない", "検索しませんでした" in reply, reply)
    reply = await talk.say("探索役に頼んで 検索して 今日の天気")  # 調べるだけの役には、検索のツールがない
    check("調べるだけのサブエージェントは、検索できない", "Web 検索の結果" not in reply and "web_search" in reply, reply)

    await talk.close()
    print("DONE")


async def approval() -> None:
    """問い合わせと検索のたびに承認を求める設定（require_approval: true）のとき。"""
    from check_chat_history import Chat, login

    talk = Chat()
    await talk.open(login())
    for label, request, done in (
        ("問い合わせ", "問い合わせて SELECT count(*) AS n FROM products", "1 行、1 列"),
        ("検索", "検索して 今日の天気", "Web 検索の結果"),
    ):
        talk.ask_decision = "reject"
        reply = await talk.say(request)
        check(f"{label}は承認を求められ、拒否すると実行されない", "許可しませんでした" in reply and done not in reply, reply)
        talk.ask_decision = "approve"
        reply = await talk.say(request)
        check(f"{label}は、承認すると実行される", done in reply, reply)
    check("拒否した問い合わせでは、ファイルが作られていない", len(_outputs()) == 1, _outputs())
    await talk.close()
    print("DONE")


if __name__ == "__main__":
    asyncio.run({"direct": direct, "approval": approval}.get((sys.argv[1:] or ["chat"])[0], chat)())
