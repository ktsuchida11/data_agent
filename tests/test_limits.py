"""1回の依頼で使える回数の上限（limits.py）。

サブエージェントの中の呼び出しと、承認のあとの続きも、同じ依頼として数えること。
LangChain 標準の上限は、この2つの場合に効かなかった（ADR-0020）。
"""

from __future__ import annotations

import uuid

from fake_model import FakeModel
from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from data_agent.safety.limits import RequestLimitMiddleware, RequestLimits


def _config() -> dict:
    return {"configurable": {"thread_id": str(uuid.uuid4())}}


def _ask(text: str) -> dict:
    return {"messages": [{"role": "user", "content": text}]}


@tool
def note(text: str = "") -> str:
    """メモを取る。"""
    return f"noted {text}"


@tool
def search(question: str = "") -> str:
    """調べる。"""
    return f"found {question}"


def _agent(limits: RequestLimits, *, root: bool = True, tools: list | None = None, interrupt_on: dict | None = None):
    middleware = [RequestLimitMiddleware(limits, root=root)]
    if interrupt_on:
        middleware.append(HumanInTheLoopMiddleware(interrupt_on=interrupt_on))
    return create_agent(FakeModel(), tools=tools or [note, search], middleware=middleware, checkpointer=InMemorySaver())


# ---------- 台帳 ----------
def test_model_calls_stop_at_limit() -> None:
    limits = RequestLimits(max_model_calls=3, max_tool_calls=10)
    assert [limits.take_model_call("t") for _ in range(5)] == [True, True, True, False, False]
    assert limits.usage("t").model_calls == 3


def test_tool_calls_stop_at_limit_and_per_tool_limit() -> None:
    limits = RequestLimits(max_model_calls=10, max_tool_calls=4, per_tool={"search": 2})
    assert limits.take_tool_call("t", "search") is None
    assert limits.take_tool_call("t", "search") is None
    refused = limits.take_tool_call("t", "search")
    assert refused is not None and "search" in refused and "2 回" in refused
    assert limits.take_tool_call("t", "note") is None
    assert limits.take_tool_call("t", "note") is None
    refused = limits.take_tool_call("t", "note")  # 全体の上限（4 回）
    assert refused is not None and "4 回" in refused
    usage = limits.usage("t")
    assert (usage.tool_calls, dict(usage.by_tool)) == (4, {"search": 2, "note": 2})


def test_same_call_id_is_counted_once() -> None:
    """サブエージェントの中で承認を待つと、再開のときに、呼んだ側の呼び出しがもう一度通る。"""
    limits = RequestLimits(max_model_calls=10, max_tool_calls=2)
    assert limits.take_tool_call("t", "task", "call-1") is None
    assert limits.take_tool_call("t", "task", "call-1") is None
    assert limits.take_tool_call("t", "task", "call-1") is None
    assert limits.usage("t").tool_calls == 1


def test_threads_are_counted_separately_and_reset_starts_over() -> None:
    limits = RequestLimits(max_model_calls=1, max_tool_calls=1)
    assert limits.take_model_call("a") is True
    assert limits.take_model_call("a") is False
    assert limits.take_model_call("b") is True
    limits.reset("a")
    assert limits.take_model_call("a") is True


def test_old_ledgers_are_dropped() -> None:
    limits = RequestLimits(max_model_calls=1, max_tool_calls=1)
    for index in range(2_100):
        limits.take_model_call(f"thread-{index}")
    assert len(limits._ledgers) == 2_000
    assert limits.take_model_call("thread-0") is True  # 古い台帳は捨てられ、数え直しになる


# ---------- エージェントに組み込んだとき ----------
def test_repeating_tool_calls_stop_with_a_notice() -> None:
    limits = RequestLimits(max_model_calls=5, max_tool_calls=100)
    agent, config = _agent(limits), _config()
    result = agent.invoke(_ask('繰り返して 50 note {"text": "a"}'), config)
    assert "モデルを呼べる回数の上限（5 回）" in result["messages"][-1].text
    usage = limits.usage(config["configurable"]["thread_id"])
    assert (usage.model_calls, usage.tool_calls) == (5, 5)


def test_per_tool_limit_returns_error_and_run_continues() -> None:
    limits = RequestLimits(max_model_calls=20, max_tool_calls=100, per_tool={"search": 2})
    agent, config = _agent(limits), _config()
    result = agent.invoke(_ask('繰り返して 4 search {"question": "q"}'), config)
    tools = [m for m in result["messages"] if m.type == "tool"]
    assert [m.status for m in tools] == ["success", "success", "error", "error"]
    assert "search を呼べる回数の上限（2 回）" in tools[-1].text
    assert result["messages"][-1].text.startswith("結果: ")  # モデルは、そのあとも返事を書ける


def test_tool_limit_refuses_further_tools() -> None:
    limits = RequestLimits(max_model_calls=20, max_tool_calls=3)
    agent, config = _agent(limits), _config()
    result = agent.invoke(_ask("繰り返して 5 note {}"), config)
    tools = [m for m in result["messages"] if m.type == "tool"]
    assert [m.status for m in tools] == ["success", "success", "success", "error", "error"]
    assert "ツールを呼べる回数の上限（3 回）" in tools[-1].text


def test_next_request_starts_a_new_count() -> None:
    limits = RequestLimits(max_model_calls=4, max_tool_calls=100)
    agent, config = _agent(limits), _config()
    first = agent.invoke(_ask("繰り返して 50 note {}"), config)
    assert "上限" in first["messages"][-1].text
    second = agent.invoke(_ask('note {"text": "b"}'), config)
    assert second["messages"][-1].text == "結果: noted b"


def test_count_is_kept_across_approvals() -> None:
    """承認を待って止まり、再開しても、回数は 0 に戻らない。"""
    limits = RequestLimits(max_model_calls=6, max_tool_calls=100)
    agent = _agent(limits, interrupt_on={"note": {"allowed_decisions": ["approve", "reject"]}})
    config = _config()
    agent.invoke(_ask("繰り返して 50 note {}"), config)
    approvals = 0
    while (state := agent.get_state(config)).interrupts:
        approvals += 1
        assert approvals < 20, "上限で止まらなかった"
        answers = {it.id: {"decisions": [{"type": "approve"}]} for it in state.interrupts}
        agent.invoke(Command(resume=answers), config)
    assert approvals == 6
    assert "モデルを呼べる回数の上限（6 回）" in state.values["messages"][-1].text
    assert limits.usage(config["configurable"]["thread_id"]).model_calls == 6


def test_subagent_calls_are_counted_in_the_same_ledger() -> None:
    """サブエージェント（別のエージェント）の中の呼び出しも、同じ依頼の回数に入る。"""
    limits = RequestLimits(max_model_calls=8, max_tool_calls=100, per_tool={"search": 3})
    inner = create_agent(FakeModel(), tools=[note, search], middleware=[RequestLimitMiddleware(limits, root=False)])

    @tool
    def delegate(description: str) -> str:
        """別のエージェントに頼む。"""
        return inner.invoke({"messages": [{"role": "user", "content": description}]})["messages"][-1].text

    agent = _agent(limits, tools=[delegate, search])
    config = _config()
    thread_id = config["configurable"]["thread_id"]

    # サブエージェントの中で繰り返しても、親と合わせた回数で止まる
    result = agent.invoke(_ask(json_call("delegate", "繰り返して 50 note {}")), config)
    assert "上限（8 回）" in result["messages"][-1].text
    assert limits.usage(thread_id).model_calls == 8

    # ツールごとの上限も、サブエージェントの分と合わせて数える
    result = agent.invoke(_ask(json_call("delegate", '繰り返して 5 search {"question": "q"}')), config)
    usage = limits.usage(thread_id)
    assert usage.by_tool["search"] == 3
    assert "search を呼べる回数の上限（3 回）" in result["messages"][-1].text


def json_call(name: str, description: str) -> str:
    import json

    return f"{name} {json.dumps({'description': description}, ensure_ascii=False)}"
