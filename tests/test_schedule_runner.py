"""定時実行の本体（schedule_runner.py）。データベースの代わりに偽の保存先を使い、本物のエージェントの仕組みで動かす。

画面と同じ通信での確認は、tests/e2e/check_schedules.py で行う。ここでは、返事の照合（ADR-0026）を確かめる。
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest
from fake_model import FakeModel
from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from data_agent.features import schedule_runner
from data_agent.safety.grounding import NOTE_TITLE
from data_agent.safety.guardrails import Guard

SALES = "地域,売上\n東京,120\n大阪,80\n福岡,45"
READ_SALES = {"name": "read_file", "args": {"file_path": "/data/sales.csv"}}
FABRICATED = "東京の売上は 777,777 円です。出典: https://example.com/fake-source"


class _Scripted(BaseChatModel):
    """最初の呼び出しでは、決めたツールを呼ぶ。ツールの結果が届いたら（呼ぶものがなければ、最初から）、決めた返事をする。"""

    calls: list[dict] = []
    reply: str = ""

    @property
    def _llm_type(self) -> str:
        return "scripted-for-tests"

    def _generate(self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
        answered = any(m.type == "tool" for m in messages)
        if self.calls and not answered:
            calls = [{**call, "id": f"call-{uuid.uuid4().hex[:8]}", "type": "tool_call"} for call in self.calls]
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=calls))])
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.reply))])

    def bind_tools(self, tools: Any, **kwargs: Any) -> _Scripted:
        return self


@tool
def read_file(file_path: str) -> str:
    """ファイルを読む。"""
    return SALES


@tool
def delete(file_path: str) -> str:
    """ファイルを消す（確認が必要）。"""
    return "消しました"


_inner = create_agent(FakeModel(), tools=[read_file])


@tool
async def task(description: str) -> str:
    """サブエージェントに頼む。"""
    result = await _inner.ainvoke({"messages": [{"role": "user", "content": description}]})
    return result["messages"][-1].text


class _History:
    """会話の保存先の代わり。保存した返事を覚えておく。"""

    def __init__(self) -> None:
        self.replies: list[str] = []

    async def get_user_identifier(self, user_id: str) -> str:
        return "test"

    async def create_thread(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def add_message(self, thread_id: str, kind: str, author: str, text: str) -> None:
        if kind == "assistant_message":
            self.replies.append(text)

    async def execute_query(self, query: str, params: dict | None = None) -> list:
        return []


@pytest.fixture(autouse=True)
def _no_memories(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """長期メモは、空の一時フォルダから読む（手元のメモの数に、結果が左右されないように）。"""
    monkeypatch.setattr(schedule_runner, "PROJECT_ROOT", tmp_path)
    return tmp_path


def _run(calls: list[dict], reply: str, *, tools: list | None = None, interrupt_on: dict | None = None) -> str:
    """予定を1つ実行して、保存された返事を返す。"""
    middleware = [HumanInTheLoopMiddleware(interrupt_on=interrupt_on)] if interrupt_on else []
    agent = create_agent(
        _Scripted(calls=calls, reply=reply),
        tools=tools or [read_file, delete, task],
        middleware=middleware,
        checkpointer=InMemorySaver(),
    )
    history = _History()
    row = {"id": "s1", "userId": "u1", "model": None, "prompt": "売上をまとめて"}
    status = asyncio.run(
        schedule_runner.run_schedule(
            row, history=history, agent_for=lambda _: agent, guard=Guard(), default_model="fake", timezone="Asia/Tokyo"
        )
    )
    assert status == "ok" and len(history.replies) == 1
    return history.replies[0]


def test_unverified_numbers_in_a_scheduled_reply_get_a_note() -> None:
    saved = _run([READ_SALES], FABRICATED)
    assert saved.startswith(FABRICATED)  # 返事そのものは、そのまま残る
    assert NOTE_TITLE in saved and "- 数: 777,777" in saved
    assert "- URL: `https://example.com/fake-source`" in saved
    assert "- 根拠に使ったもの: `/data/sales.csv`" in saved


def test_correct_sum_in_a_scheduled_reply_has_no_note() -> None:
    saved = _run([READ_SALES], "合計は 245 です。東京は 120 です。")
    assert saved == "合計は 245 です。東京は 120 です。"


def test_scheduled_run_without_data_is_not_checked() -> None:
    saved = _run([], "サーバーのポートは 8443 で、待ち時間は 1500 ミリ秒です。")
    assert NOTE_TITLE not in saved


def test_numbers_in_long_term_memory_count_as_grounds(_no_memories: Path) -> None:
    (_no_memories / "memories").mkdir()
    (_no_memories / "memories" / "goals.md").write_text("今期の目標は 777,777 円\n", encoding="utf-8")
    saved = _run([READ_SALES], "目標は 777,777 円です。")
    assert NOTE_TITLE not in saved


def test_what_a_subagent_read_counts_as_grounds() -> None:
    """サブエージェントが読んだファイルも、根拠に入る（画面と同じ）。親が task しか呼ばなくても、照合する。"""
    ask = {"name": "task", "args": {"description": 'read_file {"file_path": "/data/sales.csv"}'}}
    saved = _run([ask], "東京は 120 円で、合計は 777,777 円です。")
    assert NOTE_TITLE in saved and "- 数: 777,777" in saved and "120" not in saved.split(NOTE_TITLE)[1]
    assert "`/data/sales.csv`" in saved


def test_note_comes_after_the_auto_rejection_notice() -> None:
    """確認が必要な操作を自動で拒否した回も、照合する。注記は、拒否の知らせのあと（返事のいちばん下）に付く。"""
    saved = _run([READ_SALES, {"name": "delete", "args": {"file_path": "/note.txt"}}], FABRICATED, interrupt_on={"delete": True})
    assert "自動で拒否しました" in saved and NOTE_TITLE in saved
    assert saved.index("自動で拒否しました") < saved.index(NOTE_TITLE)
    assert saved.count("`/data/sales.csv`") == 1
