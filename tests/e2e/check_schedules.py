"""定時実行を、画面と同じ通信（ログイン・WebSocket・API）で確かめる。

tests/e2e/fake_app.py で起動したアプリのコンテナの中で実行する。チャットも予定も1件もない状態から始めること。
アプリは SCHEDULER_POLL_SECONDS=2 で起動しておくこと（予定の時刻が来たかを、2秒ごとに見る）。

    python tests/e2e/check_schedules.py

環境変数 E2E_PASSWORD にログインのパスワードを渡す。
実行の時刻は、データベースの「次の実行時刻」を過去に書き換えて、すぐに来たことにする。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys

from check_chat_history import Chat, api, check, list_threads, login
from fake_app import FABRICATED_REPLY

from data_agent.features.schedules import ScheduleStore
from data_agent.safety.grounding import NOTE_TITLE
from data_agent.settings import get_settings
from data_agent.storage.chat_history import build_data_layer

NOTE = "/app/workspace/note.txt"
RUN_PREFIX = "[定時実行]"


def run_threads(cookie: str) -> list[dict]:
    return [t for t in list_threads(cookie) if t["name"].startswith(RUN_PREFIX)]


async def wait_for_runs(cookie: str, count: int, seconds: int = 30) -> list[dict]:
    for _ in range(seconds):
        threads = run_threads(cookie)
        if len(threads) >= count:
            await asyncio.sleep(1)  # 返事の保存が終わるのを待つ
            return run_threads(cookie)
        await asyncio.sleep(1)
    return run_threads(cookie)


def last_reply(cookie: str, thread_id: str) -> str:
    steps = api(cookie, "GET", f"/project/thread/{thread_id}")["steps"]
    return next((s["output"] for s in reversed(steps) if s["type"] == "assistant_message"), "")


async def press(cookie: str, chat: Chat, name: str) -> None:
    """一覧に付いてきたボタンのうち、名前が合う最初の1つを押す。"""
    button = next(a for a in chat.actions if a["name"] == name)
    chat.actions.remove(button)
    api(cookie, "POST", "/project/action", {"sessionId": chat.session_id, "action": button})
    await asyncio.sleep(1)


async def main() -> None:
    settings = get_settings()
    db = build_data_layer(settings)
    store = ScheduleStore(db)

    async def make_due(prompt: str) -> None:
        await db.execute_query(
            """UPDATE "Schedule" SET "nextRunAt" = (now() AT TIME ZONE 'utc') - interval '1 minute' WHERE prompt = $1""",
            {"prompt": prompt},
        )

    async def schedule(prompt: str) -> dict:
        return (await db.execute_query('SELECT * FROM "Schedule" WHERE prompt = $1', {"prompt": prompt}))[0]

    cookie = login()
    check("最初は一覧が空", list_threads(cookie) == [])
    chat = Chat()
    await chat.open(cookie)

    # 受け付けられない入力
    reply = await chat.say("こんにちは", command="定時実行")
    check("cron 式がないと断られる", "5項目" in reply, reply)
    reply = await chat.say("* * * * * 毎分やって", command="定時実行")
    check("間隔が短すぎると断られる", "間隔が短すぎます" in reply, reply)
    reply = await chat.say("99 9 * * * だめな時刻", command="定時実行")
    check("読み取れない時刻は断られる", "読み取れませんでした" in reply, reply)

    # 登録は承認制
    chat.ask_decision = "cancel"
    reply = await chat.say("0 9 * * 1-5 平日の朝の作業", command="定時実行")
    check("確認で「やめる」を選ぶと登録されない", "登録しませんでした" in reply, reply)
    check(
        "やめた予定は保存されていない",
        await store.count_for_user((await db.execute_query('SELECT id FROM "User"'))[0]["id"]) == 0,
    )

    chat.ask_decision = "confirm"
    reply = await chat.say("*/5 * * * * 定時のテストです", command="定時実行")
    check("確認で「登録する」を選ぶと登録される", "登録しました" in reply, reply)

    reply = await chat.say("見せて", command="定時実行一覧")
    check("一覧に、時刻と依頼文が出る", "*/5 * * * *" in reply and "定時のテストです" in reply, reply)
    check(
        "一覧に、止めるボタンと削除するボタンが付く", {"schedule_toggle", "schedule_delete"} <= {a["name"] for a in chat.actions}
    )

    # 時刻が来たら、無人で実行される
    await make_due("定時のテストです")
    runs = await wait_for_runs(cookie, 1)
    check("時刻が来ると実行され、結果がチャットとして一覧に出る", len(runs) == 1, [t["name"] for t in list_threads(cookie)])
    run_id = runs[0]["id"]
    check("実行の結果に、モデルの返事が残る", "1 通目" in last_reply(cookie, run_id), last_reply(cookie, run_id))
    row = await schedule("定時のテストです")
    check("予定に、前回の結果が記録される", row["lastStatus"] == "ok" and str(row["lastThreadId"]) == run_id, row["lastStatus"])
    check(
        "次の実行時刻が、先の時刻に進む",
        (
            await db.execute_query(
                'SELECT "nextRunAt" > (now() AT TIME ZONE \'utc\') AS ok FROM "Schedule" WHERE id = $1', {"id": row["id"]}
            )
        )[0]["ok"],
    )

    # 実行の結果のチャットを開いて、続きを話せる
    again = Chat()
    await again.open(cookie, thread_id=run_id)
    reply = await again.say("続きの質問です")
    check("実行の結果のチャットで、続きを話せる", "2 通目" in reply, reply)
    await again.close()

    # 同じ回を二重に実行しない（アプリの見張りと、ここからの確保を同時に走らせる）
    before = len(run_threads(cookie))
    await make_due("定時のテストです")
    claims = await asyncio.gather(*(store.claim_due(settings.schedule_timezone) for _ in range(5)))
    mine = sum(len(c) for c in claims)
    await asyncio.sleep(8)
    by_app = len(run_threads(cookie)) - before
    check("同じ回を確保できるのは1か所だけ", mine + by_app == 1, {"ここで確保": mine, "アプリが実行": by_app})

    # 承認が必要な操作は、自動で拒否される
    with open(NOTE, "w") as f:
        f.write("消えてはいけないメモ\n")
    reply = await chat.say("*/5 * * * * 消して /note.txt", command="定時実行")
    check("削除を頼む予定も登録はできる", "登録しました" in reply, reply)
    before = len(run_threads(cookie))
    await make_due("消して /note.txt")
    runs = await wait_for_runs(cookie, before + 1)
    newest = last_reply(cookie, runs[0]["id"])
    check("承認が必要な操作は自動で拒否される", "自動で拒否しました" in newest, newest)
    check("拒否されたので、ファイルは消えていない", os.path.exists(NOTE))

    # Web 検索は、質問文を調べて、怪しいときだけ確かめる（require_approval: auto。ADR-0022）。
    # 無人では確かめる人がいないので、怪しい検索だけを自動で拒否し、ふつうの検索はそのまま行う
    for search, allowed in (("検索して 今日の天気", True), ("検索して はまなす商事 評判", False)):
        reply = await chat.say(f"*/5 * * * * {search}", command="定時実行")
        check(
            "登録のときに、怪しい Web 検索は自動で拒否されることが示される",
            "登録しました" in reply and "Web 検索（外に出したくない情報らしいものを含むとき）" in chat.asks[-1]["content"],
            chat.asks[-1],
        )
        before = len(run_threads(cookie))
        await make_due(search)
        runs = await wait_for_runs(cookie, before + 1)
        newest = last_reply(cookie, runs[0]["id"])
        if allowed:
            check("無人の実行でも、ふつうの Web 検索は行う", "Web 検索の結果" in newest and "自動で拒否" not in newest, newest)
        else:
            check(
                "無人の実行では、見張る言葉を含む Web 検索は自動で拒否される",
                "自動で拒否しました" in newest and "Web 検索の結果" not in newest,
                newest,
            )
        await db.execute_query('DELETE FROM "Schedule" WHERE prompt = $1', {"prompt": search})

    # データを読んだ回の返事は、画面と同じ決まりで根拠と照合し、確かめられないものに注記を付ける（ADR-0026）。
    # 無人の実行では、その場で読む人がいないので、結果のチャットを開いた人が気づけるようにする
    for prompt, noted in (("読んでから作り話して /data/sales.csv", True), ("読んでから合計を言って /data/sales.csv", False)):
        reply = await chat.say(f"*/5 * * * * {prompt}", command="定時実行")
        check("ファイルを読む予定を登録できる", "登録しました" in reply, reply)
        before = len(run_threads(cookie))
        await make_due(prompt)
        runs = await wait_for_runs(cookie, before + 1)
        newest = last_reply(cookie, runs[0]["id"])
        if noted:
            check(
                "無人の実行でも、根拠にない数と URL に注記が付く（返事そのものは、そのまま残る）",
                newest.startswith(FABRICATED_REPLY)
                and NOTE_TITLE in newest
                and "- 数: 777,777" in newest
                and "- 根拠に使ったもの: `/data/sales.csv`" in newest,
                newest,
            )
        else:
            check(
                "無人の実行でも、合っている計算には注記が付かない",
                "合計は 245 です" in newest and NOTE_TITLE not in newest,
                newest,
            )
        await db.execute_query('DELETE FROM "Schedule" WHERE prompt = $1', {"prompt": prompt})

    # 止める、削除する
    chat.actions.clear()
    await chat.say("見せて", command="定時実行一覧")
    await press(cookie, chat, "schedule_toggle")
    first = (await db.execute_query('SELECT * FROM "Schedule" ORDER BY "createdAt"'))[0]
    check("ボタンで予定を止められる", first["enabled"] is False, first["enabled"])
    before = len(run_threads(cookie))
    await make_due(first["prompt"])
    await asyncio.sleep(6)
    check("止めた予定は、時刻が来ても実行されない", len(run_threads(cookie)) == before)
    await press(cookie, chat, "schedule_delete")
    remaining = await db.execute_query('SELECT prompt FROM "Schedule"')
    check("ボタンで予定を削除できる", len(remaining) == 1, [r["prompt"] for r in remaining])
    await chat.close()

    # 外から起動する形（AWS では EventBridge から、この形で呼ぶ）
    done = subprocess.run(
        [sys.executable, "-m", "data_agent.features.schedule_runner"], capture_output=True, text=True, timeout=120
    )
    check("外から起動する形でも動く", done.returncode == 0 and "実行した予定" in done.stdout, (done.stdout + done.stderr)[-300:])
    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
