"""画面の入口の守り（connection_guard.py。ADR-0027）。接続元の検査と、HTTPS でのクッキーの Secure。"""

from __future__ import annotations

import asyncio
from typing import Any

from data_agent.ui.connection_guard import REJECTED_BODY, ConnectionGuard, secure_cookie

ALLOWED = ["http://127.0.0.1:8000", "http://localhost:8000/"]  # 末尾の / は、あってもなくてもよい
COOKIES = [(b"set-cookie", b"access_token=abc; HttpOnly; Path=/; SameSite=lax"), (b"set-cookie", b"other=1; Path=/")]


def _run(
    kind: str, origin: str | None = None, *, scheme: str = "http", headers: list | None = None, allowed: list[str] = ALLOWED
) -> tuple[bool, list[dict[str, Any]], list[str]]:
    """守りを通して呼ぶ。（中のアプリまで届いたか、送った応答、断ったときの記録）を返す。"""
    reached: list[bool] = []
    sent: list[dict[str, Any]] = []
    rejected: list[str] = []

    async def inner(scope: dict, receive: Any, send: Any) -> None:
        reached.append(True)
        if scope["type"] == "http":
            await send({"type": "http.response.start", "status": 200, "headers": list(COOKIES)})
            await send({"type": "http.response.body", "body": b"ok"})

    async def receive() -> dict[str, Any]:
        return {"type": "websocket.connect"} if kind == "websocket" else {"type": "http.request", "body": b""}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    raw = [(b"host", b"127.0.0.1:8000"), *(headers or [])]
    if origin is not None:
        raw.append((b"origin", origin.encode()))
    scope = {"type": kind, "scheme": scheme, "path": "/ws/socket.io/", "headers": raw}
    guard = ConnectionGuard(inner, allowed, on_reject=lambda k, o: rejected.append(f"{k} {o}"))
    asyncio.run(guard(scope, receive, send))
    return bool(reached), sent, rejected


def test_websocket_from_other_origins_is_refused() -> None:
    for origin in ["http://localhost:3000", "http://127.0.0.1:8001", "https://evil.example", "null", ""]:
        reached, sent, rejected = _run("websocket", origin)
        assert not reached, origin
        assert sent == [{"type": "websocket.close", "code": 1008}], origin  # 受け入れる前に閉じる（サーバーは 403 を返す）
        assert rejected == [f"websocket {origin}"]


def test_websocket_from_allowed_origins_and_without_origin_is_passed() -> None:
    for origin in ["http://127.0.0.1:8000", "http://localhost:8000", "HTTP://LOCALHOST:8000/", None]:
        reached, sent, rejected = _run("websocket", origin)
        assert reached and not sent and not rejected, origin


def test_http_from_other_origins_is_refused() -> None:
    reached, sent, rejected = _run("http", "http://localhost:3000")
    assert not reached
    assert sent[0]["status"] == 403 and sent[1]["body"] == REJECTED_BODY
    assert rejected == ["http http://localhost:3000"]


def test_http_from_allowed_origin_and_without_origin_is_passed() -> None:
    for origin in ["http://127.0.0.1:8000", None]:
        reached, sent, rejected = _run("http", origin)
        assert reached and sent[0]["status"] == 200 and not rejected, origin


def test_star_turns_the_origin_check_off() -> None:
    reached, _, rejected = _run("websocket", "https://evil.example", allowed=["*"])
    assert reached and not rejected


def test_other_scopes_are_passed() -> None:
    reached, _, _ = _run("lifespan")
    assert reached


def _cookies(sent: list[dict[str, Any]]) -> list[bytes]:
    return [value for key, value in sent[0]["headers"] if key == b"set-cookie"]


def test_cookies_get_secure_over_https() -> None:
    _, sent, _ = _run("http", scheme="https")
    assert all(c.endswith(b"; Secure") for c in _cookies(sent))
    _, sent, _ = _run("http", headers=[(b"x-forwarded-proto", b"https")])  # ロードバランサーの後ろ
    assert all(c.endswith(b"; Secure") for c in _cookies(sent))
    _, sent, _ = _run("http", headers=[(b"x-forwarded-proto", b"https, http")])
    assert all(c.endswith(b"; Secure") for c in _cookies(sent))


def test_cookies_are_unchanged_over_http() -> None:
    _, sent, _ = _run("http")
    assert _cookies(sent) == [value for _, value in COOKIES]  # 手元（http）では付けない。付けると、ログインできなくなる
    _, sent, _ = _run("http", headers=[(b"x-forwarded-proto", b"http")])
    assert _cookies(sent) == [value for _, value in COOKIES]


def test_secure_is_not_added_twice() -> None:
    assert secure_cookie(b"a=1; Secure; Path=/") == b"a=1; Secure; Path=/"
    assert secure_cookie(b"a=1; secure") == b"a=1; secure"
    assert secure_cookie(b"secure=1; Path=/") == b"secure=1; Path=/; Secure"  # 名前が secure のクッキーには付ける
