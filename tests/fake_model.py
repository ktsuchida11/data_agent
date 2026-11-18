"""単体テスト用の偽のモデル。利用者の発言の先頭の言葉で、呼び出すツールを決める。

    「<ツール名> <引数の JSON>」           そのツールを1回呼ぶ
    「繰り返して <回数> <ツール名> <JSON>」  そのツールを、指定した回数だけ続けて呼ぶ
    それ以外                               「返事」と返す

ツールを呼んだあとは、その結果を「結果: …」と返す。
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult


def _call(text: str) -> dict[str, Any] | None:
    name, _, raw = text.partition(" ")
    try:
        args = json.loads(raw or "{}")
    except ValueError:
        return None
    return {"name": name, "args": args} if re.fullmatch(r"[a-z_]+", name) and isinstance(args, dict) else None


class FakeModel(BaseChatModel):
    calls: int = 0  # 呼び出された回数

    @property
    def _llm_type(self) -> str:
        return "fake-for-tests"

    def _generate(self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
        self.calls += 1
        humans = [i for i, m in enumerate(messages) if m.type == "human"]
        text = messages[humans[-1]].text if humans else ""
        results = [m for m in messages[humans[-1] + 1 :] if m.type == "tool"] if humans else []
        call = None
        if repeat := re.match(r"繰り返して (\d+) (.+)", text, re.DOTALL):
            if len(results) < int(repeat.group(1)):
                call = _call(repeat.group(2))
        elif not results:
            call = _call(text)
        if call:
            message = AIMessage(content="", tool_calls=[{**call, "id": f"call-{uuid.uuid4().hex[:8]}", "type": "tool_call"}])
        else:
            message = AIMessage(content=f"結果: {results[-1].text}" if results else "返事")
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(self, tools: Any, **kwargs: Any) -> FakeModel:
        return self
