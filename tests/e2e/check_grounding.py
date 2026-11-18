"""返事の数と URL の、根拠との照合（ADR-0026）を、画面と同じ通信（ログイン・WebSocket・API）で確かめる。

tests/e2e/fake_app.py で起動したアプリのコンテナの中で実行する。チャットが1件もない状態から始めること。
作業フォルダ（/app/workspace）に、data/sales.csv（地域,売上 / 東京,120 / 大阪,80 / 福岡,45）を置いておくこと。

    python tests/e2e/check_grounding.py

環境変数 E2E_PASSWORD にログインのパスワードを渡す。
"""

from __future__ import annotations

import asyncio

from check_chat_history import Chat, api, check, list_threads, login
from fake_app import FABRICATED_REPLY

from data_agent.safety.grounding import NOTE_TITLE


async def main() -> None:
    cookie = login()
    chat = Chat()
    await chat.open(cookie)

    reply = await chat.say("読んでから合計を言って /data/sales.csv")
    check("合っている計算（表の列の合計）には、注記が出ない", "合計は 245 です" in reply and NOTE_TITLE not in reply, reply)

    reply = await chat.say("読んでから作り話して /data/sales.csv")
    check("根拠にない数に、注記が出る", NOTE_TITLE in reply and "- 数: 777,777" in reply, reply)
    check("根拠にない URL は、押して開かない書式で出る", "- URL: `https://example.com/fake-source`" in reply, reply)
    check("根拠に使ったものが出る", "- 根拠に使ったもの: `/data/sales.csv`" in reply, reply)
    check("返事そのものは、そのまま残る", reply.startswith(FABRICATED_REPLY), reply)

    reply = await chat.say("そのまま言って サーバーのポートは 8443 で、待ち時間は 1500 ミリ秒です。")
    check("データを読んでいない回の返事は、照合しない", "8443" in reply and NOTE_TITLE not in reply, reply)

    reply = await chat.say("実行して print(1234 * 2)")
    check("コードの実行の結果にある数には、注記が出ない", "2468" in reply and NOTE_TITLE not in reply, reply)

    thread_id = list_threads(cookie)[0]["id"]
    thread = api(cookie, "GET", f"/project/thread/{thread_id}")
    saved = [s["output"] for s in thread["steps"] if s["type"] == "assistant_message"]
    check("保存した会話にも、注記が残る", any(NOTE_TITLE in text and "- 数: 777,777" in text for text in saved), saved)
    check("注記は、照合で引っかかった返事にだけ付く", sum(NOTE_TITLE in text for text in saved) == 1, saved)

    # ツールを呼ぶ前に話した文の数も照合する。注記は、その回の最後の返事の下に1つだけ出す
    reply = await chat.say("作り話してから読んで /data/sales.csv")
    check(
        "ツールを呼ぶ前に話した文にある、根拠にない数と URL も、注記に出る",
        reply.startswith("読み終えました。")
        and NOTE_TITLE in reply
        and "- 数: 777,777" in reply
        and "`https://example.com/fake-source`" in reply,
        reply,
    )
    shown = [text for text in chat.assistant if text.startswith(FABRICATED_REPLY)]
    check(
        "ツールを呼ぶ前に話した文は、そのまま残る（注記は付けない）",
        bool(shown) and all(NOTE_TITLE not in text for text in shown),
        shown,
    )
    thread = api(cookie, "GET", f"/project/thread/{thread_id}")
    saved = [s["output"] for s in thread["steps"] if s["type"] == "assistant_message"]
    check(
        "保存した会話でも、注記は最後の返事に付く",
        any(t.startswith("読み終えました。") and NOTE_TITLE in t for t in saved),
        saved[-3:],
    )
    print("DONE")


if __name__ == "__main__":
    asyncio.run(main())
