"""ログインの失敗の回数の制限（ui/login_guard.py。ADR-0038）。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from data_agent.settings import get_security
from data_agent.ui import login_guard
from data_agent.ui.login_guard import BLOCKED_DETAIL, LoginGuard, LoginThrottle

SANDBOX = "172.30.0.5"  # コードを実行する環境の接続元（の例）
BROWSER = "172.18.0.1"  # 画面を開いている利用者の接続元（の例）


class Clock:
    now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_five_attempts_are_free_then_the_wait_doubles_up_to_the_limit() -> None:
    clock = Clock()
    throttle = LoginThrottle(clock=clock)
    waits = []
    for _ in range(12):
        throttle.attempt(SANDBOX)
        waits.append(throttle.wait(SANDBOX))
        clock.now += waits[-1]
    assert waits == [0, 0, 0, 0, 60, 120, 240, 480, 900, 900, 900, 900]
    assert throttle.wait(SANDBOX) == 0  # 待ち終われば、また試せる


def test_the_count_is_forgotten_after_an_idle_hour() -> None:
    clock = Clock()
    throttle = LoginThrottle(clock=clock)
    for _ in range(4):
        throttle.attempt(BROWSER)
    clock.now += 3601
    throttle.attempt(BROWSER)  # 数え直したので、1 回目
    assert throttle.wait(BROWSER) == 0


def test_old_clients_are_forgotten_when_too_many_are_tracked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(login_guard, "MAX_TRACKED", 3)
    throttle = LoginThrottle(clock=Clock())
    for client in ["a", "b", "c", "d"]:
        for _ in range(5):
            throttle.attempt(client)
    assert throttle.wait("a") == 0
    assert throttle.wait("d") > 0


def test_the_throttle_is_built_from_the_settings() -> None:
    throttle = LoginThrottle(**get_security().login.model_dump())  # app.py と同じ作り方
    assert (throttle.free_attempts, throttle.lock_seconds, throttle.max_lock_seconds) == (5, 60, 900)


# ---------- ミドルウェア ----------


class FakeApp:
    """ログインの入口の代わり。パスワードが "right" なら 200、ちがえば 401 を返す。"""

    def __init__(self, gate: asyncio.Event | None = None) -> None:
        self.calls = 0
        self.gate = gate

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        status = 200 if scope["password"] == "right" else 401
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b""})


def request(
    client: str, password: str = "wrong", path: str = "/login", method: str = "POST", headers: list | None = None
) -> dict:
    return {
        "type": "http",
        "method": method,
        "path": path,
        "client": (client, 50000),
        "headers": headers or [],
        "password": password,
    }


def new_guard(app: FakeApp | None = None, on_block: Any = None) -> LoginGuard:
    return LoginGuard(app or FakeApp(), LoginThrottle(clock=Clock()), on_block)


async def call(guard: LoginGuard, scope: dict) -> tuple[int, dict[bytes, bytes], bytes]:
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b""}

    async def send(message: dict) -> None:
        sent.append(message)

    await guard(scope, receive, send)
    return sent[0]["status"], dict(sent[0]["headers"]), b"".join(m.get("body", b"") for m in sent[1:])


def send_all(guard: LoginGuard, scopes: list[dict]) -> list[tuple[int, dict[bytes, bytes], bytes]]:
    """順に送って、応答（番号、ヘッダー、本文）を返す。"""

    async def run() -> list:
        return [await call(guard, scope) for scope in scopes]

    return asyncio.run(run())


def statuses(guard: LoginGuard, scopes: list[dict]) -> list[int]:
    return [status for status, _, _ in send_all(guard, scopes)]


def test_the_sixth_attempt_gets_429_without_reaching_the_login() -> None:
    app, locks = FakeApp(), []
    results = send_all(new_guard(app, on_block=lambda client, wait: locks.append((client, wait))), [request(SANDBOX)] * 8)
    assert [status for status, _, _ in results] == [401] * 5 + [429] * 3
    assert app.calls == 5
    _, headers, body = results[5]
    assert headers[b"retry-after"] == b"60"
    assert json.loads(body) == {"detail": BLOCKED_DETAIL}
    assert locks == [(SANDBOX, 60)]  # 監査ログに書くのは、受け付けなくなったときの 1 回だけ


def test_a_blocked_client_cannot_log_in_even_with_the_right_password() -> None:
    assert statuses(new_guard(), [request(BROWSER)] * 5 + [request(BROWSER, "right")]) == [401] * 5 + [429]


def test_the_injected_code_does_not_lock_out_the_person_at_the_screen() -> None:
    assert statuses(new_guard(), [request(SANDBOX)] * 10 + [request(BROWSER, "right")])[-1] == 200


def test_a_success_resets_the_count() -> None:
    typos_then_right = [request(BROWSER)] * 4 + [request(BROWSER, "right")]
    assert statuses(new_guard(), typos_then_right * 3) == ([401] * 4 + [200]) * 3


def test_a_success_on_the_last_free_attempt_is_not_reported() -> None:
    locks: list = []
    send_all(new_guard(on_block=lambda client, wait: locks.append(client)), [request(BROWSER)] * 4 + [request(BROWSER, "right")])
    assert locks == []


def test_attempts_sent_at_the_same_time_are_counted_before_they_finish() -> None:
    gate = asyncio.Event()
    app = FakeApp(gate)
    guard = new_guard(app)

    async def scenario() -> list[int]:
        tasks = [asyncio.create_task(call(guard, request(SANDBOX))) for _ in range(20)]
        await asyncio.sleep(0)
        gate.set()
        return [status for status, _, _ in await asyncio.gather(*tasks)]

    assert sorted(asyncio.run(scenario())) == [401] * 5 + [429] * 15
    assert app.calls == 5


def test_a_forged_forwarded_header_does_not_change_the_client() -> None:
    forged = [request(SANDBOX, headers=[(b"x-forwarded-for", f"10.0.0.{i}".encode())]) for i in range(7)]
    assert statuses(new_guard(), forged) == [401] * 5 + [429] * 2


@pytest.mark.parametrize(
    "scope",
    [request(SANDBOX, method="GET"), request(SANDBOX, path="/project/threads"), request(SANDBOX, path="/logout")],
)
def test_other_requests_are_not_counted(scope: dict) -> None:
    app = FakeApp()
    send_all(new_guard(app), [scope] * 10)
    assert app.calls == 10
