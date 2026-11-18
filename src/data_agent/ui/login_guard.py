"""ログインの失敗の回数の制限（ADR-0038）。Chainlit のアプリに、ASGI のミドルウェアとして付ける。

コードを実行する環境（sandbox）は、アプリと同じネットワークにいるので、ログインの入口（POST /login）に届く。
そのため、仕込まれたコードが、パスワードを総当たりで試せてしまう（ADR-0034 で見つけた）。

- 接続元（つないできた相手のアドレス）ごとに、続けて試した回数を数える。
  free_attempts 回を使い切ると、lock_seconds のあいだ受け付けない（HTTP 429）。そのあとも試すたびに、
  受け付けない時間を倍にする（上限は max_lock_seconds）。ログインに成功したら、数え直す。
- 試した回数は、答えを待たずに、受け付けた時点で数える。同時にたくさん送られても、free_attempts を超えては通らない。
- X-Forwarded-For は見ない。仕込まれたコードが自由に書けるので、信じると、接続元を偽って制限を逃れられる。
  ロードバランサーの後ろに置くとき（AWS）は、どの接続元を信じるかを決めてから、ここを直す。
- 接続元ごとに数えるので、仕込まれたコードが失敗を重ねても、画面を開いている利用者は締め出されない。
- 数は、アプリのメモリにだけ持つ。アプリを起動し直すと、数え直す。

このファイルは Chainlit を読み込まない（手元の単体テストで試せるように）。
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import Any

from starlette.responses import JSONResponse

from data_agent.ui.connection_guard import ASGIApp, Receive, Scope, Send

MAX_TRACKED = 10_000  # 覚えておく接続元の数の上限（メモリを使いすぎないため。古いものから忘れる）
BLOCKED_DETAIL = "ログインの失敗が続いたため、しばらく受け付けません。時間をおいて、もう一度お試しください。"


class LoginThrottle:
    """接続元ごとの、続けて試した回数と、最後に試した時刻。"""

    def __init__(
        self,
        free_attempts: int = 5,
        lock_seconds: int = 60,
        max_lock_seconds: int = 900,
        forget_after_seconds: int = 3600,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.free_attempts = free_attempts
        self.lock_seconds = lock_seconds
        self.max_lock_seconds = max_lock_seconds
        self.forget_after_seconds = forget_after_seconds
        self._clock = clock
        self._tries: dict[str, tuple[int, float]] = {}  # 接続元 → (続けて試した回数, 最後に試した時刻)

    def wait(self, key: str) -> int:
        """あと何秒、受け付けないか（受け付けるなら 0）。"""
        count, last = self._tries.get(key, (0, 0.0))
        over = count - self.free_attempts
        if over < 0:
            return 0
        lock = min(self.lock_seconds * 2 ** min(over, 32), self.max_lock_seconds)  # 32: 数が大きくなりすぎないように
        return max(0, math.ceil(last + lock - self._clock()))

    def attempt(self, key: str) -> None:
        """1 回試した。しばらく試していなければ、数え直す。"""
        now = self._clock()
        count, last = self._tries.pop(key, (0, now))
        if now - last > self.forget_after_seconds:
            count = 0
        self._tries[key] = (count + 1, now)  # 後ろに置き直す（忘れるときは、前から）
        if len(self._tries) > MAX_TRACKED:
            del self._tries[next(iter(self._tries))]

    def succeeded(self, key: str) -> None:
        """ログインに成功した。数え直す。"""
        self._tries.pop(key, None)


def client_key(scope: Scope) -> str:
    """接続元のアドレス。X-Forwarded-For は見ない（上の説明）。"""
    client = scope.get("client")
    return str(client[0]) if client else "unknown"


def is_login(scope: Scope) -> bool:
    return scope["type"] == "http" and scope["method"] == "POST" and scope["path"].rstrip("/").endswith("/login")


class LoginGuard:
    """ログインの入口で、接続元ごとの回数を数え、使い切ったら 429 を返す。"""

    def __init__(self, app: ASGIApp, throttle: LoginThrottle, on_block: Callable[[str, int], None] | None = None) -> None:
        self.app = app
        self.throttle = throttle
        self.on_block = on_block  # 失敗して受け付けなくなったときに呼ぶ（接続元と、待つ秒数）。監査ログに使う

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not is_login(scope):
            await self.app(scope, receive, send)
            return
        key = client_key(scope)
        if wait := self.throttle.wait(key):
            response = JSONResponse({"detail": BLOCKED_DETAIL}, status_code=429, headers={"Retry-After": str(wait)})
            await response(scope, receive, send)
            return
        self.throttle.attempt(key)  # 答えを待たずに数える

        async def watch(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                if 200 <= message["status"] < 300:
                    self.throttle.succeeded(key)
                elif self.on_block is not None and (lock := self.throttle.wait(key)):
                    self.on_block(key, lock)  # 受け付けない時間 1 回につき 1 回（429 のたびには書かない）
            await send(message)

        await self.app(scope, receive, watch)
