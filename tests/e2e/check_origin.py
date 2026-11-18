"""画面の入口の守り（接続元の検査と、HTTPS でのクッキー。ADR-0027）を、画面と同じ通信で確かめる。

tests/e2e/fake_app.py で起動したアプリのコンテナの中で実行する。許可する接続元は、.chainlit/config.toml の
allow_origins（http://127.0.0.1:8000 と http://localhost:8000）。

    python tests/e2e/check_origin.py

環境変数 E2E_PASSWORD にログインのパスワードを渡す。
"""

from __future__ import annotations

import asyncio
import base64
import http.client
import json
import os
import urllib.error
import urllib.parse
import urllib.request

import socketio
from check_chat_history import BASE, USER, Chat, check, login

OTHER = "http://localhost:3000"  # 同じ Mac で動く、別のアプリ（SameSite では「同じサイト」とみなされる）
SOCKET = "/ws/socket.io/"


def upgrade(cookie: str, origin: str | None) -> int:
    """WebSocket の接続の申し込みを、そのまま送る。返ってきた HTTP の状態を返す（101 なら、接続できた）。"""
    conn = http.client.HTTPConnection("127.0.0.1", 8000, timeout=10)
    headers = {
        "Upgrade": "websocket",
        "Connection": "Upgrade",
        "Sec-WebSocket-Key": base64.b64encode(os.urandom(16)).decode(),
        "Sec-WebSocket-Version": "13",
        "Cookie": cookie,
    }
    if origin is not None:
        headers["Origin"] = origin
    conn.request("GET", f"{SOCKET}?EIO=4&transport=websocket", headers=headers)
    status = conn.getresponse().status
    conn.close()
    return status


def request(method: str, path: str, *, headers: dict[str, str], body: bytes | None = None) -> tuple[int, str, list[str]]:
    """（状態、本文、Set-Cookie の一覧）を返す。"""
    req = urllib.request.Request(f"{BASE}{path}", data=body, method=method, headers=headers)
    try:
        res = urllib.request.urlopen(req, timeout=10)
    except urllib.error.HTTPError as err:
        res = err
    return res.status, res.read().decode("utf-8", "replace"), res.headers.get_all("set-cookie") or []


def attributes(cookie: str) -> str:
    """クッキーの名前と属性だけ（値は、ログインの鍵なので出さない）。"""
    name, _, rest = cookie.partition("=")
    return "; ".join([name, *(part.strip() for part in rest.split(";")[1:])])


def has_secure(cookie: str) -> bool:
    return "secure" in [part.strip().lower() for part in cookie.split(";")[1:]]


async def connect(cookie: str, origin: str) -> bool:
    client = socketio.AsyncClient()
    try:
        await client.connect(
            BASE,
            socketio_path="/ws/socket.io",
            headers={"Cookie": cookie, "Origin": origin},
            auth={"clientType": "webapp", "sessionId": "origin-check", "threadId": "", "userEnv": "{}", "chatProfile": ""},
            transports=["websocket"],
            wait_timeout=5,
        )
    except socketio.exceptions.ConnectionError:
        return False
    finally:
        await client.disconnect()  # つながらなかったときも、通信の後片付けをする
    return True


async def main() -> None:
    cookie = login()  # Origin を付けない通信（スクリプト）は、これまでどおり通る

    chat = Chat()  # 画面と同じ接続元（http://127.0.0.1:8000）
    await chat.open(cookie)
    reply = await chat.say("そのまま言って 接続元の確認")
    check("許可した接続元からは、チャットにつながり、返事が返る", "接続元の確認" in reply, reply)
    await chat.sio.disconnect()
    check("許可したもう1つの接続元（http://localhost:8000）からも、つながる", await connect(cookie, "http://localhost:8000"))

    check(
        "別のアプリ（http://localhost:3000）からは、ログイン中のクッキーがあっても、チャットにつながらない",
        not await connect(cookie, OTHER),
    )
    status = upgrade(cookie, OTHER)
    check("WebSocket の申し込みに、403 が返る（別のアプリ）", status == 403, status)
    check("WebSocket の申し込みに、403 が返る（null）", upgrade(cookie, "null") == 403)
    check("許可した接続元の申し込みには、101 が返る（つながる）", upgrade(cookie, BASE) == 101)

    status, body, _ = request("GET", f"{SOCKET}?EIO=4&transport=polling", headers={"Cookie": cookie, "Origin": OTHER})
    check(
        "WebSocket の代わりの通信（polling）も、別のアプリからは断る",
        status == 403 and "接続元が許可されていません" in body,
        status,
    )
    status, body, _ = request("GET", f"{SOCKET}?EIO=4&transport=polling", headers={"Cookie": cookie, "Origin": BASE})
    check("polling は、許可した接続元からは通る", status == 200 and '"sid"' in body, status)

    threads = json.dumps({"pagination": {"first": 5}, "filter": {}}).encode()
    for origin, expected, label in [(OTHER, 403, "断る"), (BASE, 200, "通る")]:
        status, _, _ = request(
            "POST",
            "/project/threads",
            headers={"Cookie": cookie, "Content-Type": "application/json", "Origin": origin},
            body=threads,
        )
        check(f"ふつうの API（チャットの一覧）も、{origin} からは{label}", status == expected, status)

    form = urllib.parse.urlencode({"username": USER, "password": os.environ["E2E_PASSWORD"]}).encode()
    _, _, cookies = request("POST", "/login", headers={"X-Forwarded-Proto": "https"}, body=form)
    check(
        "HTTPS で届いたログイン（ロードバランサーの後ろ）では、クッキーに Secure が付く",
        bool(cookies) and all(has_secure(c) for c in cookies),
        [attributes(c) for c in cookies],
    )
    _, _, cookies = request("POST", "/login", headers={}, body=form)
    check(
        "手元（http）のログインでは、Secure を付けない（付けると、ログインできなくなる）",
        bool(cookies) and not any(has_secure(c) for c in cookies),
        [attributes(c) for c in cookies],
    )
    print("DONE")


if __name__ == "__main__":
    asyncio.run(main())
