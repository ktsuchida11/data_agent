"""ガードレールをエージェントに差し込むミドルウェア。

守る場所は4つ。
- モデルに渡す直前: 利用者の入力とツールの結果を、もう一度マスクする（入口で漏れた場合の備え）。
- ツールの結果: 読んだファイルの中身などを、会話に入る前にマスクする。どのツールの結果にも効く
  （用途パックの Python のツールや、MCP のツールも）。
- モデルの返事: 文章をマスクする。秘密情報を含むファイルへの書き込みは、実行せずに拒否する。
- 外に送るツールの引数: ツールの扱いで check_arguments としたもの（MCP のツールなど。tools/registry.py）は、
  引数に秘密情報や個人情報（「マスク」にしている種類）があれば、送らずに拒否する（ADR-0032）。

サブエージェントは親のミドルウェアを引き継がないので、agent.py ですべてのエージェントに付ける。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse, ToolCallRequest
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langgraph.types import Command

from data_agent.safety.audit import audit
from data_agent.safety.guardrails import SECRET, Finding, Guard, masked_only

# 書き込む内容が入っている引数
_WRITE_ARGS = {"write_file": "content", "edit_file": "new_string"}
# 監査ログに、ファイルの場所を残す道具（Deep Agents のファイルの道具だけ。ほかの道具の引数は、データのことがある）
_FILE_TOOLS = frozenset({"ls", "read_file", "write_file", "edit_file", "glob", "grep", "delete"})

_SEND_DENIED = (
    "送るのをやめました。引数に、{kinds}らしい文字列が含まれています。"
    "この情報は、ツールの送り先に送れません。利用者に、その旨を伝えてください。"
)

_DENIAL_MESSAGES = {
    "masked_placeholder": (
        "書き込みを拒否しました。内容に、マスクされた値（[マスク済み:…]）が含まれています。"
        "このまま書き込むと、ファイルにあった元の値が失われます。"
        "edit_file で、マスクされていない部分だけを変更してください。"
    ),
    SECRET: (
        "書き込みを拒否しました。内容に、秘密情報らしい文字列が含まれています。"
        "秘密情報は、利用者が自分でファイルに書く必要があります。その旨を利用者に伝えてください。"
    ),
}


class GuardrailMiddleware(AgentMiddleware):
    def __init__(self, guard: Guard, *, check_arguments: Iterable[str] = ()) -> None:
        """check_arguments は、送る前に引数を確かめるツールの名前。"""
        super().__init__()
        self._guard = guard
        self._check_arguments = frozenset(check_arguments)

    # ---------- モデルの呼び出し ----------
    def wrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]
    ) -> ModelResponse | AIMessage:
        return self._mask_response(handler(self._mask_request(request)))

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelResponse | AIMessage:
        return self._mask_response(await handler(self._mask_request(request)))

    def _mask_request(self, request: ModelRequest) -> ModelRequest:
        """利用者の入力とツールの結果をマスクしてから、モデルに渡す。"""
        findings: list[Finding] = []
        messages: list[AnyMessage] = []
        for message in request.messages:
            if isinstance(message, HumanMessage | ToolMessage):
                message = self._mask_message(message, findings)
            messages.append(message)
        # ふつうは入口とツールの結果でマスク済みなので、ここでマスクするものは見つからない。
        # 「記録だけ」の種類は、入口とツールの結果で記録済みなので、ここでは記録しない
        # （会話に残っている限り、モデルを呼ぶたびに見つかってしまうため）
        self._report("model_request", [f for f in findings if f.action == "mask"])
        return request.override(messages=messages) if _masked(findings) else request

    def _mask_response(self, response: Any) -> Any:
        """モデルの返事の文章をマスクする。"""
        findings: list[Finding] = []
        if isinstance(response, AIMessage):
            response = self._mask_message(response, findings)
        elif isinstance(response, ModelResponse):
            result = [self._mask_message(m, findings) if isinstance(m, AIMessage) else m for m in response.result]
            if _masked(findings):
                response = ModelResponse(result=result, structured_response=response.structured_response)
        self._report("output", findings)
        return response

    # ---------- ツールの呼び出し ----------
    def wrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], ToolMessage | Command]
    ) -> ToolMessage | Command:
        return self._deny_write(request) or self._deny_send(request) or self._after_tool(request, handler(request))

    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]]
    ) -> ToolMessage | Command:
        denied = self._deny_write(request) or self._deny_send(request)
        return denied or self._after_tool(request, await handler(request))

    def _deny_send(self, request: ToolCallRequest) -> ToolMessage | None:
        """外に送るツールの引数に、秘密情報や個人情報があれば、送らずに理由を返す。"""
        call = request.tool_call
        if call["name"] not in self._check_arguments:
            return None
        text = json.dumps(call.get("args") or {}, ensure_ascii=False)
        found = masked_only(self._guard.scan(text)[1])
        if not found:
            return None
        labels = self._guard.labels
        kinds = "・".join(dict.fromkeys(labels.get(f.kind, f.kind) for f in found))
        audit("send_denied", tool=call["name"], kinds=sorted({f.kind for f in found}))
        return ToolMessage(content=_SEND_DENIED.format(kinds=kinds), tool_call_id=call["id"], name=call["name"], status="error")

    def _deny_write(self, request: ToolCallRequest) -> ToolMessage | None:
        """秘密情報やマスク済みの値を含む書き込みは、実行せずに理由を返す。"""
        call = request.tool_call
        arg = _WRITE_ARGS.get(call["name"])
        text = call["args"].get(arg) if arg else None
        reason = self._guard.write_denial_reason(text) if isinstance(text, str) else None
        if reason is None:
            return None
        audit("write_denied", tool=call["name"], path=call["args"].get("file_path"), reason=reason)
        return ToolMessage(content=_DENIAL_MESSAGES[reason], tool_call_id=call["id"], name=call["name"], status="error")

    def _after_tool(self, request: ToolCallRequest, result: ToolMessage | Command) -> ToolMessage | Command:
        """ツールの結果を、会話に入る前にマスクする。"""
        call = request.tool_call
        findings: list[Finding] = []
        status = "ok"
        if isinstance(result, ToolMessage):
            status = "error" if result.status == "error" else "ok"
            result = self._mask_message(result, findings)
        elif isinstance(result, Command) and isinstance(result.update, dict) and "messages" in result.update:
            # サブエージェントの結果などは、メッセージを包んだ形で返ってくる
            messages = [self._mask_message(m, findings) if isinstance(m, ToolMessage) else m for m in result.update["messages"]]
            if _masked(findings):
                result = Command(
                    graph=result.graph,
                    update={**result.update, "messages": messages},
                    resume=result.resume,
                    goto=result.goto,
                )
        args = call["args"]
        path = args.get("file_path") or args.get("path") if call["name"] in _FILE_TOOLS else None
        audit("tool_call", tool=call["name"], path=path, status=status)
        self._report("tool_result", findings, tool=call["name"])
        return result

    # ---------- 共通 ----------
    def _mask_message(self, message: Any, findings: list[Finding]) -> Any:
        """メッセージの文章の部分だけをマスクする。画像や、モデルの思考の部分には触れない。"""
        content = message.content
        if isinstance(content, str):
            masked, found = self._guard.scan(content)
        else:
            masked, found = [], []
            for block in content:
                if isinstance(block, str):
                    text, part = self._guard.scan(block)
                    masked.append(text)
                elif isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                    text, part = self._guard.scan(block["text"])
                    masked.append({**block, "text": text})
                else:
                    masked.append(block)
                    part = []
                found.extend(part)
        findings.extend(found)
        return message.model_copy(update={"content": masked}) if _masked(found) else message

    @staticmethod
    def _report(place: str, findings: list[Finding], **fields: Any) -> None:
        for finding in findings:
            audit("guardrail", place=place, **finding.as_dict(), **fields)


def _masked(findings: list[Finding]) -> bool:
    return any(f.action == "mask" for f in findings)
