"""過去のチャットの保存を、画面と同じ通信（ログイン・WebSocket・API）で確かめる。

tests/e2e/fake_app.py で起動したアプリのコンテナの中で実行する。

    python tests/e2e/check_chat_history.py first             # 1通目を送り、一覧に出ることを確かめる
    （ここでアプリを再起動する）
    python tests/e2e/check_chat_history.py second <チャットID>  # 開き直して続きを送り、名前の変更と削除を確かめる

環境変数 E2E_PASSWORD にログインのパスワードを渡す。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, datetime

import socketio

BASE = "http://127.0.0.1:8000"
USER = os.environ.get("E2E_USER", "test")
NO_ANSWER = "(no answer)"  # Chat.ask_decision に入れると、確認に答えない
NO_ANSWER_SECONDS = 25  # 答えないまま待つ時間（アプリの側の待ち時間より長くしておく）


def login() -> str:
    data = urllib.parse.urlencode({"username": USER, "password": os.environ["E2E_PASSWORD"]}).encode()
    res = urllib.request.urlopen(urllib.request.Request(f"{BASE}/login", data=data), timeout=10)
    return "; ".join(c.split(";")[0] for c in res.headers.get_all("set-cookie"))


def api(cookie: str, method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Cookie": cookie, "Content-Type": "application/json"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=10).read() or "{}")


def list_threads(cookie: str, search: str | None = None) -> list[dict]:
    body = {"pagination": {"first": 20}, "filter": {"search": search} if search else {}}
    return api(cookie, "POST", "/project/threads", body)["data"]


class Chat:
    """画面の代わりにチャットへ接続する。"""

    def __init__(self) -> None:
        self.sio = socketio.AsyncClient()
        self.session_id = str(uuid.uuid4())
        self.assistant: list[str] = []
        self.events: list[str] = []
        self.actions: list[dict] = []  # メッセージに付いてきたボタン
        self.settings: list[dict] = []  # 設定欄の項目（モデルの選択など）
        self.elements: list[dict] = []  # メッセージに付いてきた画像やファイル
        # 確認のボタンに、どれで答えるか（ボタンの payload の decision）。リストなら、聞かれた順に使う（最後のものは繰り返す）。
        # NO_ANSWER なら、答えずに放っておく（時間切れや、画面を閉じたときの動きを確かめるのに使う）
        self.ask_decision: str | list[str] | None = None
        self.ask_text: str = ""  # 文章で答える確認（拒否の理由など）への答え
        self.asks: list[dict] = []  # 聞かれた確認（文章と、選べたボタンの decision）
        self._done = asyncio.Event()

        @self.sio.on("*")
        async def on_any(event: str, data: dict | None = None) -> dict | None:
            self.events.append(event)
            if event == "ask":
                answer = self._answer_ask(data or {})
                if self.ask_decision == NO_ANSWER:
                    await asyncio.sleep(NO_ANSWER_SECONDS)
                return answer
            if event == "task_start":
                self._done.clear()
            if event == "chat_settings" and data:
                self.settings = data
            if event == "action" and data:
                self.actions.append(data)
            if event == "element" and data:
                self.elements.append(data)
            if (
                event in ("new_message", "update_message")
                and data
                and data.get("type") == "assistant_message"
                and data.get("output")
            ):
                self.assistant.append(data["output"])
            if event == "task_end":
                self._done.set()
            return None

    def _answer_ask(self, data: dict) -> dict | None:
        """アプリが確認を出して、答えを待っている。選んだボタン（または文章）を、返事として返す。"""
        message, spec = data.get("msg") or {}, data.get("spec") or {}
        if spec.get("type") == "text":
            self.asks.append({"content": message.get("output", ""), "choices": []})
            if not self.ask_text:
                return None
            return {
                "id": str(uuid.uuid4()),
                "threadId": message.get("threadId", ""),
                "name": USER,
                "type": "user_message",
                "output": self.ask_text,
                "createdAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            }
        buttons = [a for a in self.actions if a.get("forId") == message.get("id")] or self.actions
        self.asks.append(
            {"content": message.get("output", ""), "choices": [a.get("payload", {}).get("decision") for a in buttons]}
        )
        decision = self.ask_decision
        if isinstance(decision, list):
            decision = decision.pop(0) if len(decision) > 1 else (decision[0] if decision else None)
        return next((a for a in reversed(buttons) if a.get("payload", {}).get("decision") == decision), None)

    async def open(self, cookie: str, thread_id: str = "") -> None:
        await self.sio.connect(
            BASE,
            socketio_path="/ws/socket.io",
            headers={"Cookie": cookie, "Origin": BASE},
            auth={
                "clientType": "webapp",
                "sessionId": self.session_id,
                "threadId": thread_id,
                "userEnv": "{}",
                "chatProfile": "",
            },
            transports=["websocket"],
        )
        await self.sio.emit("connection_successful")
        await asyncio.sleep(2)  # 最初の挨拶、または過去のメッセージの復元を待つ

    async def say(self, text: str, command: str | None = None) -> str:
        """メッセージを送り、返事を返す。command を渡すと、`/` で選んだコマンド付きで送る。"""
        self._done.clear()
        self.assistant.clear()
        message = {
            "command": command,
            "id": str(uuid.uuid4()),
            "threadId": "",
            "name": USER,
            "type": "user_message",
            "output": text,
            "createdAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
        await self.sio.emit("client_message", {"message": message, "fileReferences": None})
        # 確認のボタンに答えると、処理がいったん終わってから再開する。再開していたら、もう一度待つ
        for _ in range(80):
            await asyncio.wait_for(self._done.wait(), 60)
            await asyncio.sleep(1)  # 保存が終わるのを待つ
            if self._done.is_set():
                break
        return self.assistant[-1] if self.assistant else ""

    async def close(self) -> None:
        await self.sio.disconnect()


def check(label: str, ok: bool, detail: object = "") -> None:
    print(f"{'OK  ' if ok else 'FAIL'} {label}" + (f": {detail}" if detail != "" else ""))
    if not ok:
        raise SystemExit(1)


async def first() -> None:
    cookie = login()
    check("最初は一覧が空", list_threads(cookie) == [])
    chat = Chat()
    await chat.open(cookie)
    reply = await chat.say("最初のメッセージです")
    check("1通目の返事が届く", "1 通目" in reply, reply)
    await chat.close()

    threads = list_threads(cookie)
    check("一覧に1件出る", len(threads) == 1, [t["name"] for t in threads])
    thread = api(cookie, "GET", f"/project/thread/{threads[0]['id']}")
    kinds = [s["type"] for s in thread["steps"]]
    check("人とアシスタントの発言が保存されている", "user_message" in kinds and "assistant_message" in kinds, kinds)
    print(f"THREAD_ID={threads[0]['id']}")


async def second(thread_id: str) -> None:
    cookie = login()
    threads = list_threads(cookie)
    check("再起動後も一覧に残っている", [t["id"] for t in threads] == [thread_id], [t["name"] for t in threads])

    chat = Chat()
    await chat.open(cookie, thread_id=thread_id)
    check("過去のチャットとして開き直せる", "resume_thread" in chat.events, sorted(set(chat.events)))
    reply = await chat.say("2つ目のメッセージです")
    check("続きから話せる（返事が2通目になる）", "2 通目" in reply, reply)
    await chat.close()

    api(cookie, "PUT", "/project/thread", {"threadId": thread_id, "name": "名前を変えたチャット"})
    check("名前を変更できる", list_threads(cookie)[0]["name"] == "名前を変えたチャット")

    api(cookie, "DELETE", "/project/thread", {"threadId": thread_id})
    check("削除すると一覧から消える", list_threads(cookie) == [])


if __name__ == "__main__":
    asyncio.run(first() if sys.argv[1] == "first" else second(sys.argv[2]))
