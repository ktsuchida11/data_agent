"""アーカイブとグループ分けを、画面と同じ通信（ログイン・WebSocket・API）で確かめる。

tests/e2e/fake_app.py で起動したアプリのコンテナの中で実行する。チャットが1件もない状態から始めること。

    python tests/e2e/check_archive_group.py

環境変数 E2E_PASSWORD にログインのパスワードを渡す。
"""

from __future__ import annotations

import asyncio
import uuid

from check_chat_history import Chat, api, check, list_threads, login


def names(cookie: str, search: str | None = None) -> list[str]:
    return sorted(t["name"] for t in list_threads(cookie, search))


async def main() -> None:
    cookie = login()
    check("最初は一覧が空", list_threads(cookie) == [])

    # チャットを2つ作る
    chat_a, chat_b = Chat(), Chat()
    await chat_a.open(cookie)
    check("コマンドが画面に知らされる", "set_commands" in chat_a.events)
    await chat_a.say("相談その1")
    await chat_b.open(cookie)
    await chat_b.say("別の話")
    check("2件が一覧に出る", names(cookie) == ["別の話", "相談その1"], names(cookie))

    # グループ
    reply = await chat_a.say("見積", command="グループ")
    check("グループに入れたと返事が来る", "見積" in reply, reply)
    check("一覧で名前の先頭にグループ名が付く", names(cookie) == ["[見積] 相談その1", "別の話"], names(cookie))
    check("グループ名で検索すると絞り込める", names(cookie, "見積") == ["[見積] 相談その1"], names(cookie, "見積"))

    thread_a = next(t["id"] for t in list_threads(cookie) if "相談" in t["name"])
    api(cookie, "PUT", "/project/thread", {"threadId": thread_a, "name": "[見積] 新しい名前"})
    check("名前を変更してもグループ名が二重に付かない", "[見積] 新しい名前" in names(cookie), names(cookie))

    # アーカイブ
    reply = await chat_b.say("お願いします", command="アーカイブ")
    check("アーカイブしたと返事が来る", "アーカイブしました" in reply, reply)
    check("アーカイブしたチャットは一覧から消える", names(cookie) == ["[見積] 新しい名前"], names(cookie))

    reply = await chat_a.say("見せて", command="アーカイブ一覧")
    check("アーカイブ一覧に出る", "別の話" in reply, reply)
    button = next((a for a in chat_a.actions if a["name"] == "unarchive"), None)
    check("一覧に戻すボタンが付く", button is not None, [a["name"] for a in chat_a.actions])

    # ほかのチャットの ID をでっち上げたボタンでは、何も起きない
    forged = dict(button, id=str(uuid.uuid4()), payload={"thread_id": str(uuid.uuid4())})
    api(cookie, "POST", "/project/action", {"sessionId": chat_a.session_id, "action": forged})
    check("存在しないチャットの ID では何も変わらない", names(cookie) == ["[見積] 新しい名前"], names(cookie))

    api(cookie, "POST", "/project/action", {"sessionId": chat_a.session_id, "action": button})
    await asyncio.sleep(1)
    check("ボタンで一覧に戻せる", names(cookie) == ["[見積] 新しい名前", "別の話"], names(cookie))

    # グループ解除
    await chat_a.say("外して", command="グループ解除")
    check("グループから外すと先頭のグループ名が消える", names(cookie) == ["別の話", "新しい名前"], names(cookie))

    # コマンドのメッセージがエージェントに渡っていないこと
    reply = await chat_a.say("相談その2")
    check("コマンドはエージェントに渡らない（次の返事が2通目）", "2 通目" in reply, reply)

    # チャットの最初のメッセージがコマンドでも動くこと
    chat_c = Chat()
    await chat_c.open(cookie)
    reply = await chat_c.say("最初からグループ", command="グループ")
    check("最初のメッセージがコマンドでも動く", "最初からグループ" in reply, reply)

    for chat in (chat_a, chat_b, chat_c):
        await chat.close()


if __name__ == "__main__":
    asyncio.run(main())
