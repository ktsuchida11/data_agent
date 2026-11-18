"""画面の入口の守り（ADR-0027）。接続元の検査と、HTTPS でのクッキー。Chainlit のアプリに、ASGI のミドルウェアとして付ける。

1. 接続元（Origin）の検査
   Chainlit の allow_origins（.chainlit/config.toml）は、ふつうの通信（HTTP）の CORS にしか効かず、
   チャットの接続（WebSocket）には効かない（ADR-0009 で確かめた）。守りはクッキーの SameSite=lax だけだった。
   SameSite はポート番号を区別しないので、同じ Mac で動く別のアプリ（http://localhost:3000 など）のページからは、
   ログイン中のクッキー付きで、チャットにつなげてしまう。
   ブラウザは、WebSocket の接続と、ほかの場所（オリジン）からの通信に、必ず Origin を付ける。
   - Origin が付いていて、一覧にないものは、WebSocket も HTTP も断る（"null" も断る）。
   - Origin が付いていないもの（同じ画面からの GET、スクリプト、死活監視）は、通す。
     スクリプトは Origin を自由に書けるので、断っても守りにならない。守る相手は、ブラウザの中の悪いページ。
   - 一覧に "*" があるときは、検査しない（Chainlit の既定。このプロジェクトでは使わない）。
2. HTTPS でのクッキー
   Chainlit は、SameSite=none のときにしか、クッキーに Secure を付けない。そのままでは、HTTPS で公開しても、
   ログインのクッキーが、暗号化されない通信（http://）でも送られる。
   HTTPS で届いた通信（ロードバランサーが付ける X-Forwarded-Proto: https を含む）の応答では、クッキーに Secure を付ける。
   付けるだけなので、X-Forwarded-Proto を偽られても、守りは弱くならない（偽った本人のクッキーが送られなくなるだけ）。
3. 画面に付ける HTTP ヘッダー（SecurityHeaders。ADR-0020）
   外部の画像を読み込ませない、ほかのサイトの枠に埋め込ませない、エージェントが作ったファイルはスクリプトを動かさずに配る。

このファイルは Chainlit を読み込まない（手元の単体テストで試せるように）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from starlette.datastructures import MutableHeaders
from starlette.responses import PlainTextResponse

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

REJECTED_BODY = "接続元が許可されていません（.chainlit/config.toml の allow_origins）".encode()
_POLICY_VIOLATION = 1008  # WebSocket を閉じる理由（決まりに反する接続）


def normalize_origin(origin: str) -> str:
    return origin.strip().rstrip("/").lower()


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers") or []:
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def is_https(scope: Scope) -> bool:
    """HTTPS で届いた通信か。ロードバランサーの X-Forwarded-Proto も見る（Secure を付けるかどうかにだけ使う）。"""
    if scope.get("scheme") == "https":
        return True
    forwarded = _header(scope, b"x-forwarded-proto")
    return bool(forwarded) and forwarded.split(",")[0].strip().lower() == "https"


def secure_cookie(value: bytes) -> bytes:
    """Set-Cookie の値に、Secure がなければ付ける。"""
    attributes = [part.strip().lower() for part in value.split(b";")[1:]]
    return value if b"secure" in attributes else value + b"; Secure"


class ConnectionGuard:
    """接続元の検査と、HTTPS でのクッキーの Secure。"""

    def __init__(
        self,
        app: ASGIApp,
        allowed_origins: Iterable[str],
        on_reject: Callable[[str, str], None] | None = None,
    ) -> None:
        self.app = app
        self.allowed = {normalize_origin(origin) for origin in allowed_origins}
        self.check_origin = "*" not in self.allowed
        self.on_reject = on_reject  # 断ったときに呼ぶ（通信の種類と、接続元）。監査ログに使う

    def allows(self, origin: str | None) -> bool:
        return origin is None or not self.check_origin or normalize_origin(origin) in self.allowed

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope.get("type")
        if kind not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        origin = _header(scope, b"origin")
        if not self.allows(origin):
            if self.on_reject is not None:
                self.on_reject(kind, origin or "")
            if kind == "websocket":
                await receive()  # 接続の申し込み（websocket.connect）
                # 受け入れる前に閉じると、サーバー（uvicorn）は HTTP の 403 を返して、接続を作らない
                await send({"type": "websocket.close", "code": _POLICY_VIOLATION})
            else:
                await PlainTextResponse(REJECTED_BODY, status_code=403)(scope, receive, send)
            return
        if kind == "http" and is_https(scope):

            async def send_secure(message: dict[str, Any]) -> None:
                if message["type"] == "http.response.start":
                    message["headers"] = [
                        (key, secure_cookie(value) if key.lower() == b"set-cookie" else value)
                        for key, value in message.get("headers") or []
                    ]
                await send(message)

            await self.app(scope, receive, send_secure)
            return
        await self.app(scope, receive, send)


# ---------- 画面に付ける HTTP ヘッダー ----------
# img-src: 返事の文章（Markdown）に外部の画像のリンクが入っていても、ブラウザが取りに行かないようにする。
#   取りに行く URL に会話の中身を入れさせると、データを外へ持ち出せてしまう
#   （対策の前は、実際に取りに行くことを 2026-11-14 に確かめた）。画面に出す画像は、このアプリが配る。
# frame-ancestors: ほかのサイトの枠の中に、この画面を埋め込ませない（承認のボタンを、だまして押させるのを防ぐ）
_PAGE_CSP = "img-src 'self' data: blob:; media-src 'self' data: blob:; object-src 'none'; frame-ancestors 'self'; base-uri 'self'"
# エージェントが作ったファイルの配布（/project/file/…）。HTML や SVG を、この画面と同じ場所（オリジン）で開くと、
# 中のスクリプトが、ログイン中の利用者として動いてしまう（同じく、確かめた）。
# スクリプトを動かさない設定を付け、その場に表示する画像のほかは、ダウンロードにする
_FILE_CSP = "sandbox; default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'"
_FILE_PATH = "/project/file/"
_INLINE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")


class SecurityHeaders:
    """すべての応答に、上のヘッダーを付ける。"""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        is_file = _FILE_PATH in scope.get("path", "")

        async def send_with_headers(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Content-Type-Options"] = "nosniff"  # 中身から種類を推測させない
                if is_file:
                    headers["Content-Security-Policy"] = _FILE_CSP
                    if not headers.get("content-type", "").startswith(_INLINE_TYPES):
                        headers["Content-Disposition"] = "attachment"
                else:
                    headers["Content-Security-Policy"] = _PAGE_CSP
            await send(message)

        await self.app(scope, receive, send_with_headers)
