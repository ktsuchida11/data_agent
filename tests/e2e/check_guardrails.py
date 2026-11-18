"""ガードレールを、画面と同じ通信（ログイン・WebSocket・API）で確かめる。

tests/e2e/fake_app.py で起動したアプリのコンテナの中で実行する。チャットが1件もない状態から始めること。
作業フォルダ（/app/workspace）に、KEY_IN_FILE を書いた config.txt を置いておくこと。

    python tests/e2e/check_guardrails.py

環境変数 E2E_PASSWORD にログインのパスワードを渡す。キーは、どれも形だけを合わせた偽物。
"""

from __future__ import annotations

import asyncio
import json

from check_chat_history import Chat, api, check, list_threads, login

KEY_IN_INPUT = "sk-ant-api03-INPUT000INPUT000INPUT000"
KEY_IN_FILE = "sk-ant-api03-FILE0000FILE0000FILE0000"
SECRET_MASK = "[マスク済み:秘密情報]"


async def main() -> None:
    cookie = login()
    check("最初は一覧が空", list_threads(cookie) == [])
    chat = Chat()
    await chat.open(cookie)

    # 入口: 利用者が貼り付けたキー
    await chat.say(f"このキーを使って {KEY_IN_INPUT} ください")
    check("マスクしたことが利用者に知らされる", any("マスクしました" in m for m in chat.assistant), chat.assistant)
    thread_id = list_threads(cookie)[0]["id"]
    thread = api(cookie, "GET", f"/project/thread/{thread_id}")
    first = next(s for s in thread["steps"] if s["type"] == "user_message")
    check(
        "保存された入力がマスクされている",
        SECRET_MASK in first["output"] and KEY_IN_INPUT not in first["output"],
        first["output"],
    )
    name = list_threads(cookie)[0]["name"]
    check("チャットの名前もマスクされている", SECRET_MASK in name and KEY_IN_INPUT not in name, name)

    # ツールの結果: ファイルの中のキー
    reply = await chat.say("読んで /config.txt")
    check("ファイルを読んだ結果がモデルに届く", "ツールの結果" in reply, reply)
    check("ファイルの中のキーは、モデルに渡る前にマスクされる", SECRET_MASK in reply and KEY_IN_FILE not in reply, reply)

    # サブエージェントが読んだ場合（サブエージェントは親のガードレールを引き継がないので、個別に付けてある）
    for subagent, phrase in (("explorer", "探索役に読ませて"), ("general-purpose", "汎用役に読ませて")):
        reply = await chat.say(f"{phrase} /config.txt")
        check(
            f"サブエージェント（{subagent}）が読んだ結果もマスクされる", SECRET_MASK in reply and KEY_IN_FILE not in reply, reply
        )

    # 出口: ファイルへの書き込み
    reply = await chat.say("鍵を書いて /leak.txt")
    check("キーを含む書き込みは拒否される", "書き込みを拒否しました" in reply and "秘密情報" in reply, reply)
    reply = await chat.say(f"書いて /copy.txt KEY={SECRET_MASK}")
    check("マスク済みの値を含む書き込みは拒否される", "書き込みを拒否しました" in reply and "元の値" in reply, reply)
    reply = await chat.say("書いて /note.txt ふつうのメモ")
    check("ふつうの書き込みは通る", "ツールの結果" in reply and "拒否" not in reply, reply)

    # 個人情報
    await chat.say("カードは 4111 1111 1111 1111 です")
    await chat.say("連絡先は taro@example.com です")
    await chat.say("電話は０３（１２３４）５６７８です")  # 全角で、日本語に続けて書いたもの
    thread = api(cookie, "GET", f"/project/thread/{thread_id}")
    inputs = [s["output"] for s in thread["steps"] if s["type"] == "user_message"]
    check(
        "カード番号はマスクされる",
        any("[マスク済み:カード番号]" in t for t in inputs) and not any("4111" in t for t in inputs),
        inputs[-2:],
    )
    check(
        "メールアドレスはマスクされる",
        any("[マスク済み:メールアドレス]" in t for t in inputs) and not any("taro@example.com" in t for t in inputs),
        inputs[-1:],
    )

    check(
        "全角で書いた電話番号もマスクされる",
        any("電話は[マスク済み:電話番号]です" in t for t in inputs),
        inputs[-1:],
    )

    # 画面の履歴の全体に、キーが残っていないこと
    saved = json.dumps(thread, ensure_ascii=False)
    check("画面の履歴に、入力したキーが残っていない", KEY_IN_INPUT not in saved)
    check("画面の履歴に、ファイルの中のキーが残っていない", KEY_IN_FILE not in saved)

    # ガードレールが働いても、会話は続けられる
    reply = await chat.say("続きの質問です")
    check("そのあとも会話を続けられる", "通目" in reply, reply)
    await chat.close()
    print(f"THREAD_ID={thread_id}")


if __name__ == "__main__":
    asyncio.run(main())
