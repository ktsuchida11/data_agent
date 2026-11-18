"""モデルの切り替えを、画面と同じ通信（ログイン・WebSocket・API）で確かめる。

tests/e2e/fake_app.py で起動したアプリのコンテナの中で実行する。チャットが1件もない状態から始めること。
偽のモデルは、返事の最後に「どのモデルとして呼ばれたか」を付けて返す。

    python tests/e2e/check_model_switch.py

環境変数 E2E_PASSWORD にログインのパスワードを渡す。MAIN_MODEL と MODEL_CHOICES は既定のままにしておくこと。
"""

from __future__ import annotations

import asyncio

from check_chat_history import Chat, check, list_threads, login

DEFAULT = "claude-opus-5-5"
OTHER = "claude-sonnet-5-5"


def model_setting(chat: Chat) -> dict:
    return next((item for item in chat.settings if item.get("id") == "model"), {})


async def change_model(chat: Chat, model: str) -> str:
    """設定欄でモデルを選んだときと同じ通信を送り、返ってきた知らせを返す。"""
    chat.assistant.clear()
    await chat.sio.emit("chat_settings_change", {"model": model})
    await asyncio.sleep(1.5)
    return chat.assistant[-1] if chat.assistant else ""


async def main() -> None:
    cookie = login()
    check("最初は一覧が空", list_threads(cookie) == [])

    chat = Chat()
    await chat.open(cookie)
    setting = model_setting(chat)
    choices = [item["value"] for item in setting.get("items", [])]
    check("設定欄にモデルの選択肢が届く", OTHER in choices and DEFAULT in choices, choices)
    check("最初は既定のモデルが選ばれている", setting.get("initial") == DEFAULT, setting.get("initial"))

    reply = await chat.say("最初の質問です")
    check("既定のモデルで返事が来る", DEFAULT in reply and "1 通目" in reply, reply)

    notice = await change_model(chat, OTHER)
    check("切り替えたことが知らされる", OTHER in notice and "切り替えました" in notice, notice)
    reply = await chat.say("次の質問です")
    check("切り替えたモデルで、同じ会話の続きとして返事が来る", OTHER in reply and "2 通目" in reply, reply)

    notice = await change_model(chat, "no-such-model")
    check("選べないモデルは断られる", "選べません" in notice, notice)
    reply = await chat.say("もう1つ質問です")
    check("断られたあとも、前に選んだモデルのまま", OTHER in reply and "3 通目" in reply, reply)
    await chat.close()

    # 開き直したとき
    thread_id = list_threads(cookie)[0]["id"]
    again = Chat()
    await again.open(cookie, thread_id=thread_id)
    check(
        "開き直すと、設定欄に前に選んだモデルが出る",
        model_setting(again).get("initial") == OTHER,
        model_setting(again).get("initial"),
    )
    reply = await again.say("開き直してからの質問です")
    check("開き直しても、前に選んだモデルで続く", OTHER in reply and "4 通目" in reply, reply)
    await again.close()

    # 新しいチャットは既定のモデルで始まる
    fresh = Chat()
    await fresh.open(cookie)
    check(
        "新しいチャットは既定のモデルで始まる",
        model_setting(fresh).get("initial") == DEFAULT,
        model_setting(fresh).get("initial"),
    )
    reply = await fresh.say("別のチャットの質問です")
    check("新しいチャットの返事は既定のモデル", DEFAULT in reply and "1 通目" in reply, reply)
    await fresh.close()


if __name__ == "__main__":
    asyncio.run(main())
