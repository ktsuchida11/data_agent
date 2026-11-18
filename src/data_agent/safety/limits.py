"""1回の依頼で使える回数の上限。

モデルの呼び出しと、ツールの呼び出しを数えて、上限を超えたら止める。モデルが同じ操作を
繰り返し続けて、料金がふくらむのを防ぐ。

LangChain 標準の ModelCallLimitMiddleware と ToolCallLimitMiddleware は使わない。標準のものは、
エージェントごと・実行ごとに数えるので、次の2つの場合に上限が効かなかった（2026-11-14 に確かめた）。
- サブエージェントの中の呼び出しは、親の回数に入らない。サブエージェントには、上限そのものがなかった。
- 承認（人の確認）を待って止まり、再開すると、回数が 0 に戻る。

ここでは、チャット（thread_id）ごとに1つの台帳を持ち、親とサブエージェントのすべての呼び出しを、
同じ台帳に数える。台帳は、親のエージェントが新しい依頼を受け取ったときに作り直す。
承認のあとの再開では、作り直さない。

台帳は、プロセスのメモリに持つ。アプリを再起動すると、数えかけの回数は失われる
（承認を待ったまま再起動した依頼は、0 から数え直す）。
"""

from __future__ import annotations

import threading
from collections import Counter, OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse, ToolCallRequest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.config import get_config
from langgraph.types import Command

from data_agent.safety.audit import audit

_MAX_LEDGERS = 2_000  # 覚えておく台帳の数。超えたら、古いものから捨てる


@dataclass
class Ledger:
    """1回の依頼の中で使った回数。"""

    model_calls: int = 0
    tool_calls: int = 0
    by_tool: Counter[str] = field(default_factory=Counter)
    counted: set[str] = field(default_factory=set)  # 数え終えたツール呼び出しの ID
    reported: set[str] = field(default_factory=set)  # 監査ログに書いた上限の種類（同じ依頼で何度も書かない）


class RequestLimits:
    """上限の値と、台帳の置き場所。親とサブエージェントのミドルウェアで、同じものを使う。"""

    def __init__(self, max_model_calls: int, max_tool_calls: int, per_tool: Mapping[str, int] | None = None) -> None:
        self.max_model_calls = max_model_calls
        self.max_tool_calls = max_tool_calls
        self.per_tool = dict(per_tool or {})
        self._ledgers: OrderedDict[str, Ledger] = OrderedDict()
        self._lock = threading.Lock()  # サブエージェントは並んで動くことがある

    def reset(self, thread_id: str) -> None:
        """新しい依頼が始まった。台帳を作り直す。"""
        with self._lock:
            self._ledgers.pop(thread_id, None)
            self._ledger(thread_id)

    def usage(self, thread_id: str) -> Ledger:
        """いまの回数（写し）。"""
        with self._lock:
            ledger = self._ledger(thread_id)
            return Ledger(
                ledger.model_calls, ledger.tool_calls, Counter(ledger.by_tool), set(ledger.counted), set(ledger.reported)
            )

    def take_model_call(self, thread_id: str) -> bool:
        """モデルを1回呼ぶ。上限に達していたら False（呼んではいけない）。"""
        with self._lock:
            ledger = self._ledger(thread_id)
            if ledger.model_calls >= self.max_model_calls:
                self._report(ledger, "model_calls", self.max_model_calls)
                return False
            ledger.model_calls += 1
            return True

    def take_tool_call(self, thread_id: str, name: str, call_id: str | None = None) -> str | None:
        """ツールを1回呼ぶ。上限に達していたら、モデルに返す文章を返す（実行してはいけない）。

        call_id は、ツール呼び出しの ID。サブエージェントの中で承認を待って止まると、再開のときに、
        サブエージェントを呼んだ側の呼び出し（task）がもう一度ここを通る。同じ ID は、1回だけ数える。
        """
        with self._lock:
            ledger = self._ledger(thread_id)
            if call_id and call_id in ledger.counted:
                return None
            if ledger.tool_calls >= self.max_tool_calls:
                self._report(ledger, "tool_calls", self.max_tool_calls)
                return (
                    f"1回の依頼でツールを呼べる回数の上限（{self.max_tool_calls} 回）に達したので、実行しませんでした。"
                    "これ以上ツールは使えません。ここまでの結果をまとめて、利用者に伝えてください。"
                )
            limit = self.per_tool.get(name)
            if limit is not None and ledger.by_tool[name] >= limit:
                self._report(ledger, f"tool:{name}", limit)
                return (
                    f"1回の依頼で {name} を呼べる回数の上限（{limit} 回）に達したので、実行しませんでした。"
                    "すでに得た結果を使ってください。"
                )
            ledger.tool_calls += 1
            ledger.by_tool[name] += 1
            if call_id:
                ledger.counted.add(call_id)
            return None

    def _ledger(self, thread_id: str) -> Ledger:
        ledger = self._ledgers.get(thread_id)
        if ledger is None:
            ledger = self._ledgers[thread_id] = Ledger()
            while len(self._ledgers) > _MAX_LEDGERS:
                self._ledgers.popitem(last=False)
        else:
            self._ledgers.move_to_end(thread_id)
        return ledger

    @staticmethod
    def _report(ledger: Ledger, kind: str, limit: int) -> None:
        if kind not in ledger.reported:
            ledger.reported.add(kind)
            audit("limit_reached", kind=kind, limit=limit)


def _thread_id() -> str:
    """いま処理しているチャットの ID。サブエージェントの中でも、親と同じ ID が返る。"""
    try:
        return str(get_config().get("configurable", {}).get("thread_id") or "")
    except RuntimeError:  # エージェントの外から呼ばれた
        return ""


class RequestLimitMiddleware(AgentMiddleware):
    """回数を数えて、上限を超えた呼び出しを止める。

    親のエージェントには root=True で、サブエージェントには root=False で付ける
    （台帳を作り直すのは、親が新しい依頼を受け取ったときだけ）。
    """

    def __init__(self, limits: RequestLimits, *, root: bool) -> None:
        super().__init__()
        self._limits = limits
        self._root = root

    # ---------- 依頼の始まり ----------
    def before_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        # 承認のあとの再開では、ここは通らない（止まったところから続きが動く）
        if self._root:
            self._limits.reset(_thread_id())
        return None

    async def abefore_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        return self.before_agent(state, runtime)

    # ---------- モデルの呼び出し ----------
    def wrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]
    ) -> ModelResponse | AIMessage:
        return handler(request) if self._limits.take_model_call(_thread_id()) else self._stopped()

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelResponse | AIMessage:
        return await handler(request) if self._limits.take_model_call(_thread_id()) else self._stopped()

    def _stopped(self) -> AIMessage:
        # ツールを呼ばない返事を返すと、エージェントはそこで終わる
        return AIMessage(
            content=(
                f"1回の依頼でモデルを呼べる回数の上限（{self._limits.max_model_calls} 回）に達したので、ここで止めました。"
                "続けるときは、「続けて」と送ってください。"
            )
        )

    # ---------- ツールの呼び出し ----------
    def wrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], ToolMessage | Command]
    ) -> ToolMessage | Command:
        return self._refused(request) or handler(request)

    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]]
    ) -> ToolMessage | Command:
        return self._refused(request) or await handler(request)

    def _refused(self, request: ToolCallRequest) -> ToolMessage | None:
        call = request.tool_call
        reason = self._limits.take_tool_call(_thread_id(), call["name"], call.get("id"))
        if reason is None:
            return None
        return ToolMessage(content=reason, tool_call_id=call["id"], name=call["name"], status="error")
