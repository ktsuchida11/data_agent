"""ログインの失敗の回数の制限（ADR-0038）を、画面と同じ通信で確かめる。

tests/e2e/fake_app.py で起動したアプリのコンテナの中で実行する。コードを実行する環境（sandbox）からの総当たりは、
アプリの run_python と同じ部品（SandboxClient）で、コードを実行する環境にコードを送って試す。
アプリは、コードを実行する環境のネットワークに data-agent という名前でつないでおく（regress.sh。compose.yaml と同じ）。

    python tests/e2e/check_login.py

環境変数 E2E_PASSWORD にログインのパスワードを渡す。
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from check_chat_history import BASE, USER, check

from data_agent.tools.code_execution import SandboxClient

PASSWORD = os.environ["E2E_PASSWORD"]

# コードを実行する環境に送るコード。仕込まれたコードが、パスワードを総当たりで試すのと同じこと
BRUTE_FORCE = """
import json, urllib.error, urllib.parse, urllib.request
codes = []
for i in range(8):
    data = urllib.parse.urlencode({"username": USER, "password": f"guess-{i}"}).encode()
    try:
        codes.append(urllib.request.urlopen(urllib.request.Request("http://data-agent:8000/login", data=data), timeout=10).status)
    except urllib.error.HTTPError as e:
        codes.append(e.code)
    except Exception as e:
        codes.append(type(e).__name__)
print(json.dumps(codes))
"""


def try_login(password: str) -> tuple[int, str | None, dict]:
    """ログインを 1 回試す。応答の番号と、Retry-After と、本文（失敗のとき）を返す。"""
    data = urllib.parse.urlencode({"username": USER, "password": password}).encode()
    try:
        return urllib.request.urlopen(f"{BASE}/login", data=data, timeout=10).status, None, {}
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Retry-After"), json.loads(e.read() or b"{}")


def statuses(*passwords: str) -> list[int]:
    return [try_login(password)[0] for password in passwords]


def from_sandbox() -> list:
    client = SandboxClient(os.environ.get("SANDBOX_URL", "http://sandbox:8080"))
    result = asyncio.run(client.run(f"USER = {USER!r}\n" + BRUTE_FORCE, [], timeout=60))
    return json.loads(result.output.strip().splitlines()[-1])


def main() -> None:
    codes = from_sandbox()
    check(
        "コードを実行する環境から総当たりで試すと、5 回で受け付けなくなる（6 回目から 429）",
        codes == [401] * 5 + [429] * 3,
        codes,
    )
    codes = statuses(PASSWORD)
    check("仕込まれたコードが失敗を重ねても、画面の利用者は締め出されない（接続元ごとに数える）", codes == [200], codes)
    codes = statuses(*["typo"] * 4, PASSWORD)
    check("4 回間違えても、5 回目に正しいパスワードなら入れる", codes == [401] * 4 + [200], codes)
    codes = statuses(*["typo"] * 5)
    check("ログインに成功すると、数え直す（また 5 回まで試せる）", codes == [401] * 5, codes)

    status, retry_after, body = try_login(PASSWORD)
    check("回数を使い切ると、正しいパスワードでも受け付けない（429）", status == 429, status)
    check(
        "待つ秒数を Retry-After で知らせる（1 回目は 60 秒まで）",
        retry_after is not None and 1 <= int(retry_after) <= 60,
        retry_after,
    )
    check("理由を日本語で返す", "ログインの失敗が続いた" in str(body.get("detail")), body)


if __name__ == "__main__":
    main()
