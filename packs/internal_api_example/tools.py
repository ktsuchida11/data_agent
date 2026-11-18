"""用途パック「社内 API（見本）」の、Python で書いたツール（docs/tutorials/05-python-tool.md）。

社内の API は MCP でつなぐ（pack.yaml の mcp_servers）。Python で書くのは、計算のように、
毎回同じ答えにしたい処理。@agent_tool を付けた関数が、エージェントのツールになる。
関数の名前がツールの名前、docstring がモデルへの説明。
"""

from __future__ import annotations

from data_agent.tools.registry import agent_tool

TAX_RATE_PERCENT = 10  # 消費税の率（見本なので 10% で決め打ち）


@agent_tool(label="税込みの金額", grounding=True)
def price_with_tax(amount: int) -> str:
    """税抜きの金額から、消費税（10%）と税込みの金額を出す。1 円未満は切り捨てる。

    Args:
        amount: 税抜きの金額（円。受注の amount）
    """
    if amount < 0:
        return "金額は 0 以上にしてください。"
    tax = amount * TAX_RATE_PERCENT // 100
    total = amount + tax
    return f"税抜き {amount:,} 円の消費税は {tax:,} 円、税込みは {total:,} 円です（{TAX_RATE_PERCENT}%）。"
