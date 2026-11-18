"""Web 検索のツール（web_search.py）。検索する役のモデルは、偽物に差し替える。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ValidationError

from data_agent.safety.guardrails import DEFAULT_ACTIONS, Guard
from data_agent.settings import WebSearchLimits
from data_agent.tools import web_search
from data_agent.tools.web_search import WebSearcher

KEY = "sk-ant-api03-TEST0000TEST0000TEST0000"  # 形だけを合わせた偽物


def _search(query: str) -> dict:
    return {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": query}}


def _results(*pages: tuple[str, str]) -> dict:
    return {
        "type": "web_search_tool_result",
        "tool_use_id": "srvtoolu_1",
        "content": [{"type": "web_search_result", "title": t, "url": u, "encrypted_content": "..."} for t, u in pages],
    }


def _text(text: str, *cited: tuple[str, str]) -> dict:
    block: dict = {"type": "text", "text": text}
    if cited:
        block["citations"] = [{"type": "web_search_result_location", "title": t, "url": u, "cited_text": "…"} for t, u in cited]
    return block


class FakeSearchModel(BaseChatModel):
    """決めておいた返事を、順に返す。受け取ったものを覚えておく。"""

    replies: list[Any]
    seen: list[list[BaseMessage]] = []
    bound: list[Any] = []

    @property
    def _llm_type(self) -> str:
        return "fake"

    def _generate(self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
        self.seen.append(list(messages))
        reply = self.replies[min(len(self.seen), len(self.replies)) - 1]
        if isinstance(reply, Exception):
            raise reply
        return ChatResult(generations=[ChatGeneration(message=reply)])

    def bind_tools(self, tools: Any, **kwargs: Any) -> FakeSearchModel:
        self.bound.append(tools)
        return self


def _searcher(*replies: Any, model_name: str = "claude-sonnet-5-5", **limits: object) -> tuple[WebSearcher, FakeSearchModel]:
    model = FakeSearchModel(replies=list(replies), seen=[], bound=[])
    guard = Guard(DEFAULT_ACTIONS, enabled=True)
    return WebSearcher(model, model_name, WebSearchLimits(**limits), guard), model


def test_tool_version_follows_model() -> None:
    for name in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-2", "claude-opus-4-8", "claude-sonnet-4-6"):
        assert web_search.tool_type(name) == "web_search_20260209", name
    for name in ("claude-haiku-5-5", "claude-haiku-4-5", "claude-opus-4-5", "claude-sonnet-4-5", "unknown-model"):
        assert web_search.tool_type(name) == "web_search_20250305", name


def test_tool_definition_carries_limits() -> None:
    definition = web_search.tool_definition("claude-haiku-5-5", WebSearchLimits(max_uses=3, allowed_domains=["go.jp"]))
    assert definition == {"type": "web_search_20250305", "name": "web_search", "max_uses": 3, "allowed_domains": ["go.jp"]}
    assert "blocked_domains" not in web_search.tool_definition("claude-sonnet-5-5", WebSearchLimits())
    with pytest.raises(ValidationError):
        WebSearchLimits(allowed_domains=["a.example"], blocked_domains=["b.example"])


def test_answer_and_sources_are_returned() -> None:
    reply = AIMessage(
        content=[
            _text("調べます。"),
            _search("日本 人口 2025"),
            _results(("統計局", "https://www.stat.go.jp/a"), ("別のページ", "https://example.com/b")),
            _text("日本の人口は約1億2千万人です。", ("統計局", "https://www.stat.go.jp/a")),
            _text("出典は統計局です。"),
        ]
    )
    searcher, model = _searcher(reply, max_uses=4)
    text = asyncio.run(searcher.run("日本の人口は？"))

    assert model.bound == [[{"type": "web_search_20260209", "name": "web_search", "max_uses": 4}]]
    assert [m.type for m in model.seen[0]] == ["system", "human"] and model.seen[0][1].content == "日本の人口は？"
    assert text.startswith("（Web 検索の結果です。")
    assert "日本の人口は約1億2千万人です。出典は統計局です。" in text
    assert "調べます。" not in text  # 検索の前の文章は入れない
    assert "1. 統計局 https://www.stat.go.jp/a" in text
    assert "example.com/b" not in text  # 出典は、根拠にしたページだけ
    assert "（検索 1 回）" in text


def test_found_pages_are_listed_when_nothing_is_cited() -> None:
    reply = AIMessage(content=[_search("q"), _results(("ページA", "https://a.example/")), _text("まとめ")])
    text = asyncio.run(_searcher(reply)[0].run("質問"))
    assert "1. ページA https://a.example/" in text


def test_plain_text_reply_without_search() -> None:
    text = asyncio.run(_searcher(AIMessage(content="検索せずに答えます"))[0].run("質問"))
    assert "検索せずに答えます" in text and "（検索 0 回）" in text and "出典" not in text


def test_paused_turn_is_continued() -> None:
    paused = AIMessage(
        content=[_search("q1"), _results(("A", "https://a.example/"))], response_metadata={"stop_reason": "pause_turn"}
    )
    done = AIMessage(content=[_search("q2"), _results(("B", "https://b.example/")), _text("続きのまとめ")])
    searcher, model = _searcher(paused, done)
    text = asyncio.run(searcher.run("質問"))
    assert len(model.seen) == 2 and model.seen[1][-1] is paused  # 中断された返事を、そのまま送り返す
    assert "続きのまとめ" in text and "（検索 2 回）" in text


def test_question_with_secret_is_not_sent() -> None:
    searcher, model = _searcher(AIMessage(content="x"))
    text = asyncio.run(searcher.run(f"このキーの持ち主を調べて {KEY}"))
    assert "検索しませんでした" in text and model.seen == []
    text = asyncio.run(searcher.run("カード番号 4111 1111 1111 1111 について調べて"))
    assert "検索しませんでした" in text and model.seen == []


def test_long_or_empty_question_is_refused() -> None:
    searcher, model = _searcher(AIMessage(content="x"), max_question_chars=10)
    assert "長すぎます" in asyncio.run(searcher.run("あ" * 11))
    assert "空です" in asyncio.run(searcher.run("   "))
    assert model.seen == []


def test_search_errors_are_reported() -> None:
    error = {
        "type": "web_search_tool_result",
        "tool_use_id": "x",
        "content": {"type": "web_search_tool_result_error", "error_code": "too_many_requests"},
    }
    text = asyncio.run(_searcher(AIMessage(content=[_search("q"), error]))[0].run("質問"))
    assert "まとめられませんでした" in text and "too_many_requests" in text


def test_api_failure_does_not_raise() -> None:
    text = asyncio.run(_searcher(RuntimeError("boom"))[0].run("質問"))
    assert "Web 検索に失敗しました（RuntimeError）" in text and "boom" not in text


def test_long_answer_is_cut() -> None:
    text = asyncio.run(_searcher(AIMessage(content="あ" * 500), max_output_chars=100)[0].run("質問"))
    assert "あ" * 100 + "\n…（省略）" in text and "あ" * 101 not in text


def test_search_is_screened_by_default():
    """Web 検索は、質問文を調べて、外に出したくない情報らしいものがあるときだけ確かめる（ADR-0022）。

    設定ファイルに書かなくても、調べる形になる。
    """
    from data_agent.settings import get_security

    assert WebSearchLimits().require_approval == "auto"
    assert get_security().web_search.require_approval == "auto"
    assert WebSearchLimits(require_approval=True).require_approval is True
    assert WebSearchLimits(require_approval=False).require_approval is False
    with pytest.raises(ValidationError):
        WebSearchLimits(require_approval="sometimes")
