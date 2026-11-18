"""Langfuse へのトレースの送信（tracing）。Langfuse には接続しない。"""

from data_agent.safety.guardrails import Guard
from data_agent.storage import tracing

FAKE_KEY = "sk-ant-api03-FAKE0000FAKE0000FAKE0000"
SECRET_MASK = "[マスク済み:秘密情報]"


def test_tracing_is_off_without_keys(monkeypatch) -> None:
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    assert tracing.enabled() is False
    assert tracing.build_handler(Guard()) is None
    tracing.shutdown()  # 無効のときに呼んでも何も起きない


def test_tracing_needs_both_keys(monkeypatch) -> None:
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    assert tracing.enabled() is False


def test_values_sent_to_langfuse_are_masked_at_any_depth() -> None:
    value = {
        "input": [{"role": "user", "content": f"キーは {FAKE_KEY}"}],
        "output": ("ok", f"KEY={FAKE_KEY}"),
        "tokens": 12,
        "flag": True,
    }
    assert tracing.mask_value(Guard(), value) == {
        "input": [{"role": "user", "content": f"キーは {SECRET_MASK}"}],
        "output": ("ok", f"KEY={SECRET_MASK}"),
        "tokens": 12,
        "flag": True,
    }


def test_json_text_stays_valid_after_masking() -> None:
    # Langfuse には、入力や出力が JSON の文字列として渡ることがある
    import json

    text = json.dumps({"content": f"KEY={FAKE_KEY}"}, ensure_ascii=False)
    masked = tracing.mask_value(Guard(), text)
    assert json.loads(masked) == {"content": f"KEY={SECRET_MASK}"}


def test_attributes_in_escaped_json_are_masked() -> None:
    """Langfuse は、入力や出力を JSON にして送る。日本語は \\uXXXX に書き換わるので、読み戻してからマスクする。"""
    import json

    text = json.dumps({"output": f"鍵は{FAKE_KEY}、電話03-1234-5678"})  # ensure_ascii=True（Langfuse と同じ）
    masked = tracing.mask_attribute(Guard(), text)
    output = json.loads(masked)["output"]
    assert FAKE_KEY not in masked and "03-1234-5678" not in masked
    assert output == f"鍵は{SECRET_MASK}、電話[マスク済み:電話番号]"
    assert tracing.mask_attribute(Guard(), [text, "12"]) == [masked, "12"]  # 数は、そのまま
    assert tracing.mask_attribute(Guard(), f"鍵は{FAKE_KEY}") == f"鍵は{SECRET_MASK}"  # JSON でない文字列


def test_tool_messages_are_masked_before_they_are_sent() -> None:
    """ツールの結果（ToolMessage）は、会話に入る前の、マスクしていない中身を持っている。"""
    from langchain_core.messages import ToolMessage

    masked = tracing.mask_value(Guard(), ToolMessage(content=f"鍵は{FAKE_KEY}", tool_call_id="call-1"))
    assert masked["content"] == f"鍵は{SECRET_MASK}" and masked["tool_call_id"] == "call-1"
