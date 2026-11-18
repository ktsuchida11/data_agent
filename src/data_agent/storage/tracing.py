"""Langfuse へのトレースの送信（任意）。

LANGFUSE_PUBLIC_KEY と LANGFUSE_SECRET_KEY が設定されているときだけ有効になる。
モデルとのやりとりやツールの呼び出しを、Langfuse の画面で追えるようにする。

送る内容には、ガードレール（guardrails.py）を通す。ツールの実行の記録には、
会話に入る前（マスクする前）の結果が含まれるので、送信の手前で必ずマスクする。
マスクは2か所で行う。
- mask: Langfuse の SDK が、記録する内容を受け取ったとき。
- mask_otel_spans: 送信の直前。SDK 以外が作った記録も含めて、すべての項目を見る。
  項目は JSON の文字列で、日本語が \\uXXXX に書き換わっている（「鍵はsk-…」の「は」が \\u306f になり、
  続く鍵を見逃す）。JSON なら読み戻してからマスクし、日本語のまま書き戻す（mask_attribute）。
  ここで例外が起きると、Langfuse はそのまとまりを送らずに捨てる（マスクできないまま送ることはない）。
"""

from __future__ import annotations

import json
import os
from typing import Any

from pydantic import BaseModel

from data_agent.safety.guardrails import Guard


def enabled() -> bool:
    return bool(os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY"))


def mask_value(guard: Guard, value: Any) -> Any:
    """文字列をマスクする。入れ子になった辞書やリスト、LangChain のメッセージ（pydantic のモデル）の中も見る。"""
    if isinstance(value, str):
        return guard.scan(value)[0]
    if isinstance(value, dict):
        return {key: mask_value(guard, item) for key, item in value.items()}
    if isinstance(value, list):
        return [mask_value(guard, item) for item in value]
    if isinstance(value, tuple):
        return tuple(mask_value(guard, item) for item in value)
    if isinstance(value, BaseModel):
        # ツールの結果（ToolMessage）など。会話に入る前（マスクする前）の中身を持っているので、中を出して見る
        try:
            return mask_value(guard, value.model_dump())
        except Exception:
            return guard.scan(str(value))[0]
    return value


def mask_attribute(guard: Guard, value: Any) -> Any:
    """送信の直前の項目（文字列か、文字列のリスト）をマスクする。JSON の文字列は、読み戻してからマスクする。"""
    if isinstance(value, str):
        return _mask_text(guard, value)
    if isinstance(value, list | tuple):
        return type(value)(mask_attribute(guard, item) for item in value)
    return value


def _mask_text(guard: Guard, text: str) -> str:
    try:
        decoded = json.loads(text)
    except ValueError:
        return guard.scan(text)[0]
    if not isinstance(decoded, dict | list | str):
        return text  # 数や真偽値
    masked = mask_value(guard, decoded)
    return text if masked == decoded else json.dumps(masked, ensure_ascii=False)


def build_handler(guard: Guard) -> Any | None:
    """LangChain に渡すコールバックを返す。Langfuse を設定していなければ None。"""
    if not enabled():
        return None

    from langfuse import Langfuse
    from langfuse.langchain import CallbackHandler
    from langfuse.types import MaskOtelSpansParams, MaskOtelSpansResult, OtelSpanPatch

    def mask(*, data: Any, **kwargs: Any) -> Any:
        return mask_value(guard, data)

    def mask_otel_spans(*, params: MaskOtelSpansParams) -> MaskOtelSpansResult | None:
        patches = {}
        for identifier, span in params.spans.items():
            changed = {}
            for key, value in span.attributes.items():
                masked = mask_attribute(guard, value)
                if masked != value:
                    changed[key] = masked
            if changed:
                patches[identifier] = OtelSpanPatch(set_attributes=changed)
        return MaskOtelSpansResult(span_patches=patches) if patches else None

    # 鍵と接続先（LANGFUSE_BASE_URL）は環境変数から読まれる。ここで作ったものが、既定のクライアントになる
    Langfuse(mask=mask, mask_otel_spans=mask_otel_spans)
    return CallbackHandler()


def shutdown() -> None:
    """まだ送っていない記録を送り切る。アプリの終了時に呼ぶ。"""
    if enabled():
        from langfuse import get_client

        get_client().shutdown()
