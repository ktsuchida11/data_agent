"""ガードレールの検出ルール（guardrails）と、エージェントへの差し込み（guard_middleware）。

キーや番号は、どれも形だけを合わせた偽物。
"""

import json
import logging
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from data_agent.safety.guard_middleware import GuardrailMiddleware
from data_agent.safety.guardrails import MASK_PREFIX, Finding, Guard

FAKE_KEY = "sk-ant-api03-FAKE0000FAKE0000FAKE0000"
SECRET_MASK = "[マスク済み:秘密情報]"


def _kinds(findings: list[Finding]) -> dict[str, tuple[int, str]]:
    return {f.kind: (f.count, f.action) for f in findings}


# ---------- 検出ルール ----------
@pytest.mark.parametrize(
    "secret",
    [
        FAKE_KEY,
        "sk-proj-abc123def456ghi789jkl012",
        "AKIAIOSFODNN7EXAMPLE",
        "ghp_" + "a1" * 18,
        "github_pat_" + "a1B2" * 6,
        "AIza" + "a1B2c" * 7,
        "xoxb-1234567890-abcdefghij",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEfake\nfake==\n-----END RSA PRIVATE KEY-----",
    ],
)
def test_secrets_are_masked(secret: str) -> None:
    masked, findings = Guard().scan(f"設定は {secret} です")
    assert masked == f"設定は {SECRET_MASK} です"
    assert _kinds(findings) == {"secret": (1, "mask")}


@pytest.mark.parametrize(
    "text",
    [
        "クラス名は sk-three-bounce-container です",  # 数字のない sk- は CSS のクラス名など
        "task-0123456789abcdefghijklmnop を実行",  # 単語の途中の sk-
        "時刻は 1731480000000 ミリ秒",  # 13桁の数字だが、カード番号の形ではない
        "注文番号 123456789012",  # 12桁だが、マイナンバーの検査用数字に合わない
        "バージョン 1.2.3、ポート 8000",
    ],
)
def test_lookalikes_are_left_alone(text: str) -> None:
    masked, findings = Guard().scan(text)
    assert masked == text
    assert findings == []


def test_card_number_and_my_number_are_masked() -> None:
    masked, findings = Guard().scan("カードは 4111 1111 1111 1111、マイナンバーは 1234-5678-9018 です")
    assert masked == "カードは [マスク済み:カード番号]、マイナンバーは [マスク済み:マイナンバー] です"
    assert _kinds(findings) == {"credit_card": (1, "mask"), "my_number": (1, "mask")}


def test_email_and_phone_are_masked_by_default() -> None:
    text = "連絡先は taro@example.com と 090-1234-5678 です"
    masked, findings = Guard().scan(text)
    assert masked == "連絡先は [マスク済み:メールアドレス] と [マスク済み:電話番号] です"
    assert _kinds(findings) == {"email": (1, "mask"), "phone": (1, "mask")}


def test_email_and_phone_can_be_set_to_record_only() -> None:
    text = "連絡先は taro@example.com と 090-1234-5678 です"
    masked, findings = Guard({"email": "log", "phone": "log"}).scan(text)
    assert masked == text
    assert _kinds(findings) == {"email": (1, "log"), "phone": (1, "log")}


# ---------- 日本語の文章と、表記ゆれ ----------
# 正規表現の \b は、ひらがなや漢字も「単語の文字」として扱う。日本語に続けて書かれたキーや番号を
# 見逃さないことを確かめる
@pytest.mark.parametrize(
    "text",
    [
        f"鍵は{FAKE_KEY}です",
        f"キー{FAKE_KEY}",
        f"APIキー：{FAKE_KEY}。",
        "鍵はAKIAIOSFODNN7EXAMPLEです",
        "トークンはghp_" + "a1" * 18 + "です",
        "鍵はAIza" + "a1B2c" * 7 + "です",
        "これはxoxb-1234567890-abcdefghijです",
    ],
)
def test_secrets_next_to_japanese_are_masked(text: str) -> None:
    masked, findings = Guard().scan(text)
    assert SECRET_MASK in masked
    assert _kinds(findings) == {"secret": (1, "mask")}


PHONE_VARIANTS = [
    "090-1234-5678",
    "電話は090-1234-5678です",
    "電話は09012345678です",
    "03-1234-5678",
    "03(1234)5678",
    "(03)1234-5678",
    "090 1234 5678",
    "090.1234.5678",
    "０９０−１２３４−５６７８",  # 全角の数字と、マイナス記号
    "０９０-１２３４-５６７８",
    "090ー1234ー5678",  # 長音記号
    "0312345678",
    "TEL:0312345678",
    "Tel.03-1234-5678",
    "+81-90-1234-5678",
    "+81 90 1234 5678",
    "0120-123-456",
    "電話０３（１２３４）５６７８",
    "コールセンター03-1234-5678",
    "山田,090-1111-2222,東京",  # CSV の1行
]


@pytest.mark.parametrize("text", PHONE_VARIANTS)
def test_phone_number_variants_are_masked(text: str) -> None:
    masked, findings = Guard().scan(text)
    assert "[マスク済み:電話番号]" in masked
    assert not any(ch.isdigit() for ch in masked)  # 数字が一部だけ残っていない
    assert _kinds(findings) == {"phone": (1, "mask")}


@pytest.mark.parametrize(
    ("text", "masked"),
    [
        ("連絡先はtaro@example.co.jpです", "連絡先は[マスク済み:メールアドレス]です"),
        ("メール：taro.yamada+a@example.com。", "メール：[マスク済み:メールアドレス]。"),
        ("カードは4111111111111111です", "カードは[マスク済み:カード番号]です"),
        ("４１１１−１１１１−１１１１−１１１１", "[マスク済み:カード番号]"),
        ("番号は123456789018です", "番号は[マスク済み:マイナンバー]です"),
        ("１２３４ ５６７８ ９０１８", "[マスク済み:マイナンバー]"),
        # 口座番号は、前に付く言葉を残して、番号だけをマスクする
        ("普通 1234567", "普通 [マスク済み:口座番号]"),
        ("口座番号：１２３４５６７", "口座番号：[マスク済み:口座番号]"),
        ("東京支店 普通預金 7654321 ヤマダ", "東京支店 普通預金 [マスク済み:口座番号] ヤマダ"),
        ("当座,2345678", "当座,[マスク済み:口座番号]"),
    ],
)
def test_numbers_next_to_japanese_and_full_width_are_masked(text: str, masked: str) -> None:
    assert Guard().scan(text)[0] == masked


@pytest.mark.parametrize(
    "text",
    [
        "2026-11-14",
        "注文番号 0001-0002-0003",  # 電話番号の桁数ではない
        "0.0312345678",  # 小数の、小数点より後ろ
        "社員番号 0000012345",
        "1234567890",
        "価格 090円",
        "3.14159-2653-5897",
        "v0.12-345-6789",
        "売上 1234567 円",  # 7桁の数字だが、口座を表す言葉がない
        "普通の日でした。12345678 件",  # 8桁
        "010 20 3000",
        "2025-01,東京,32650,41",  # 集計結果の CSV の1行
        "郵便番号 100-0001",
        "合計 1,234,567 円（前年比 103.2%）",
    ],
)
def test_ordinary_numbers_are_not_mistaken_for_personal_data(text: str) -> None:
    masked, findings = Guard().scan(text)
    assert masked == text
    assert findings == []


def test_actions_can_be_changed_per_kind() -> None:
    guard = Guard({"email": "mask", "secret": "off"})
    masked, findings = guard.scan(f"taro@example.com / {FAKE_KEY}")
    assert masked == f"[マスク済み:メールアドレス] / {FAKE_KEY}"
    assert _kinds(findings) == {"email": (1, "mask")}


def test_disabled_guard_does_nothing() -> None:
    assert Guard(enabled=False).scan(FAKE_KEY) == (FAKE_KEY, [])
    assert Guard(enabled=False).write_denial_reason(FAKE_KEY) is None


def test_several_matches_are_counted() -> None:
    masked, findings = Guard().scan(f"{FAKE_KEY} と AKIAIOSFODNN7EXAMPLE")
    assert masked == f"{SECRET_MASK} と {SECRET_MASK}"
    assert _kinds(findings) == {"secret": (2, "mask")}


def test_write_denial_reasons() -> None:
    guard = Guard()
    assert guard.write_denial_reason("print('hello')") is None
    assert guard.write_denial_reason(f"API_KEY={FAKE_KEY}") == "secret"
    # マスクされた値をそのまま書き戻すと、ファイルにあった元の値が失われる
    assert guard.write_denial_reason(f"API_KEY={SECRET_MASK}") == "masked_placeholder"
    # メールアドレスだけなら書き込める
    assert guard.write_denial_reason("contact = 'taro@example.com'") is None


# ---------- エージェントへの差し込み ----------
@pytest.fixture
def audit_events():
    """監査ログに出た内容を、テストの中で読めるようにする。"""
    events: list[dict] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            events.append(json.loads(record.getMessage()))

    logger = logging.getLogger("data_agent.audit")
    handler = Collect()
    logger.addHandler(handler)
    yield events
    logger.removeHandler(handler)


def _tool_request(name: str, args: dict) -> SimpleNamespace:
    return SimpleNamespace(tool_call={"name": name, "args": args, "id": "call-1"}, tool=None, state={}, runtime=None)


def test_tool_result_is_masked_before_it_enters_the_conversation(audit_events: list[dict]) -> None:
    middleware = GuardrailMiddleware(Guard())
    request = _tool_request("read_file", {"file_path": "/config.txt"})
    result = middleware.wrap_tool_call(
        request, lambda _: ToolMessage(content=f"API_KEY={FAKE_KEY}", tool_call_id="call-1", name="read_file")
    )
    assert result.content == f"API_KEY={SECRET_MASK}"
    events = {e["event"]: e for e in audit_events}
    assert events["tool_call"]["tool"] == "read_file" and events["tool_call"]["path"] == "/config.txt"
    assert events["guardrail"]["place"] == "tool_result" and events["guardrail"]["kind"] == "secret"
    assert FAKE_KEY not in json.dumps(audit_events)  # 見つけた値そのものは記録しない


@pytest.mark.parametrize(
    ("tool", "args", "reason"),
    [
        ("write_file", {"file_path": "/out.txt", "content": f"KEY={FAKE_KEY}"}, "secret"),
        ("edit_file", {"file_path": "/out.txt", "old_string": "a", "new_string": f"KEY={SECRET_MASK}"}, "masked_placeholder"),
    ],
)
def test_writing_secrets_is_refused_without_running_the_tool(
    tool: str, args: dict, reason: str, audit_events: list[dict]
) -> None:
    middleware = GuardrailMiddleware(Guard())
    called = []
    result = middleware.wrap_tool_call(_tool_request(tool, args), lambda request: called.append(request))
    assert called == []  # ツールは実行されない
    assert result.status == "error" and "書き込みを拒否しました" in result.content
    assert next(e for e in audit_events if e["event"] == "write_denied")["reason"] == reason
    assert FAKE_KEY not in json.dumps(audit_events)


def test_ordinary_write_goes_through() -> None:
    middleware = GuardrailMiddleware(Guard())
    request = _tool_request("write_file", {"file_path": "/main.py", "content": "print('hello')"})
    result = middleware.wrap_tool_call(request, lambda _: ToolMessage(content="ok", tool_call_id="call-1"))
    assert result.content == "ok"


class Request:
    """モデルへの依頼の代わり（messages と override だけを持つ）。"""

    def __init__(self, messages: list) -> None:
        self.messages = messages

    def override(self, messages: list) -> "Request":
        return Request(messages)


def test_record_only_kinds_are_not_logged_again_on_every_model_call(audit_events: list[dict]) -> None:
    # 「記録だけ」にした種類は会話に残るので、モデルを呼ぶたびに見つかる。記録は入口の1回だけでよい
    middleware = GuardrailMiddleware(Guard({"email": "log"}))
    request = Request([HumanMessage(content="連絡先は taro@example.com です")])
    middleware.wrap_model_call(request, lambda _: AIMessage(content="了解です"))
    assert [e for e in audit_events if e["event"] == "guardrail"] == []


def test_model_request_and_reply_are_masked() -> None:
    middleware = GuardrailMiddleware(Guard())
    seen: list[list] = []

    def handler(request: Request) -> AIMessage:
        seen.append(request.messages)
        return AIMessage(
            content=[{"type": "text", "text": f"キーは {FAKE_KEY} です"}, {"type": "thinking", "thinking": FAKE_KEY}]
        )

    request = Request(
        [
            HumanMessage(content=f"これを使って {FAKE_KEY}"),
            AIMessage(content="了解です"),
            ToolMessage(content=f"KEY={FAKE_KEY}", tool_call_id="call-1"),
        ]
    )
    reply = middleware.wrap_model_call(request, handler)
    # モデルに渡る前に、利用者の入力とツールの結果がマスクされる
    assert seen[0][0].content == f"これを使って {SECRET_MASK}"
    assert seen[0][2].content == f"KEY={SECRET_MASK}"
    # 返事は文章の部分だけをマスクし、モデルの思考の部分には触れない
    assert reply.content[0]["text"] == f"キーは {SECRET_MASK} です"
    assert reply.content[1]["thinking"] == FAKE_KEY
    assert MASK_PREFIX in reply.content[0]["text"]
