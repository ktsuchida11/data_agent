"""監査ログ。「誰が・いつ・何をしたか」を、1件1行の JSON で標準出力に出す。

会話の中身や、ガードレールが見つけた値そのものは記録しない（中身はデータベースの履歴にある）。
コンテナでは標準出力がログの集まる先になる（ECS なら CloudWatch）。
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Mapping
from contextvars import ContextVar
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

_logger = logging.getLogger("data_agent.audit")
_logger.setLevel(logging.INFO)
_logger.propagate = False  # ほかのログの書式と混ざらないよう、専用の出力にする
if not _logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(_handler)

# いま処理している利用者とチャット。画面側の処理の最初に bind() で設定する
_context: ContextVar[Mapping[str, str]] = ContextVar("audit_context", default=MappingProxyType({}))


def bind(**fields: str | None) -> None:
    """以後の audit() に、利用者やチャットの ID を自動で付ける。"""
    _context.set({**_context.get(), **{k: v for k, v in fields.items() if v}})


def audit(event: str, **fields: Any) -> None:
    record = {
        "log": "audit",
        "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "event": event,
        **_context.get(),
        **{k: v for k, v in fields.items() if v is not None},
    }
    _logger.info(json.dumps(record, ensure_ascii=False, default=str))
