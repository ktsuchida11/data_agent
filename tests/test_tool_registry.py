"""用途パック（packs/）と、ツールの登録（tools/registry.py）と、どのツールにも効く守り（ADR-0032）。"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import ToolException, tool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError

from data_agent import settings as settings_module
from data_agent.safety import approvals, search_screen
from data_agent.safety.approvals import STANDARD, STRICT, SUSPICIOUS, ApprovalPolicy
from data_agent.safety.guard_middleware import GuardrailMiddleware
from data_agent.safety.guardrails import Guard, custom_rule
from data_agent.safety.watch_terms import WatchTerms
from data_agent.settings import GuardrailConfig, PackConfig, SecurityConfig, load_pack
from data_agent.tools import registry
from data_agent.tools.registry import AgentTool, ToolPolicy, agent_tool

SECURITY = SecurityConfig()


# ---------- 用途パック ----------
def test_the_data_pack_keeps_todays_tools_and_prompt() -> None:
    pack = load_pack("data")
    assert pack.config.builtin_tools == ["run_python", "database", "web_search"]
    assert "データ分析エージェント" in pack.system_prompt()
    assert pack.config.greeting.startswith("データ分析エージェントです。")


def test_the_example_pack_loads() -> None:
    pack = load_pack("internal_api_example")
    assert set(pack.config.mcp_servers) == {"orders"}
    assert [t.name for t in registry.python_tools(pack)] == ["price_with_tax"]


def test_the_example_python_tool_computes_the_price_with_tax() -> None:
    """見本のパックの Python のツール。社内 API は呼ばない（社内 API は MCP でつなぐ）。"""
    tool = registry.python_tools(load_pack("internal_api_example"))[0]
    assert tool.policy == ToolPolicy("税込みの金額", check_arguments=True, grounding=True)
    reply = asyncio.run(tool.tool.ainvoke({"amount": 1_560_000}))
    assert reply == "税抜き 1,560,000 円の消費税は 156,000 円、税込みは 1,716,000 円です（10%）。"
    assert "円の消費税は 0 円" in asyncio.run(tool.tool.ainvoke({"amount": 9}))  # 1 円未満は切り捨て
    assert "0 以上" in asyncio.run(tool.tool.ainvoke({"amount": -1}))


def _write_pack(root: Path, name: str, text: str, files: dict[str, str] | None = None) -> None:
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "pack.yaml").write_text(text, encoding="utf-8")
    for relative, content in (files or {"SYSTEM.md": "役割"}).items():
        (folder / relative).write_text(content, encoding="utf-8")


def test_pack_errors_say_what_is_wrong(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings_module, "PACKS_DIR", tmp_path)
    _write_pack(tmp_path, "ok", "title: a\ngreeting: b\n")
    assert load_pack("ok").system_prompt() == "役割"
    with pytest.raises(RuntimeError, match="あるもの: ok"):
        load_pack("missing")
    with pytest.raises(RuntimeError, match="英小文字"):
        load_pack("../etc")
    _write_pack(tmp_path, "typo", "title: a\ngreeting: b\nbuiltin_tool: [web_search]\n")  # 書き間違い
    with pytest.raises(RuntimeError, match="builtin_tool"):
        load_pack("typo")
    _write_pack(tmp_path, "noprompt", "title: a\ngreeting: b\nprompt: NONE.md\n")
    with pytest.raises(RuntimeError, match=r"NONE\.md"):
        load_pack("noprompt")
    _write_pack(tmp_path, "outside", "title: a\ngreeting: b\nprompt: ../ok/SYSTEM.md\n")
    with pytest.raises(RuntimeError, match="外のファイル"):
        load_pack("outside")


# ---------- ツールの扱い ----------
def test_builtin_policies_match_the_rules_before_packs() -> None:
    """パックの仕組みを入れる前と、同じ確認・回数の上限になる。"""
    policies = registry.builtin_policies(["run_python", "database", "web_search"], SECURITY)
    assert {n for n, p in policies.items() if p.require_approval is True} == set()
    assert {n for n, p in policies.items() if p.require_approval == "auto"} == {"web_search"}
    assert {n for n, p in policies.items() if p.strict} == {"run_python", "query_database", "web_search"}
    assert {n: p.max_calls_per_run for n, p in policies.items() if p.max_calls_per_run} == {"web_search": 5}
    assert {n: p.screened_args() for n, p in policies.items() if p.screened_args() is not None} == {"web_search": ("question",)}
    locked = SecurityConfig.model_validate(
        {
            "code_execution": {"require_approval": True},
            "database": {"require_approval": True},
            "web_search": {"require_approval": True},
        }
    )
    configured = {
        n
        for n, p in registry.builtin_policies(["run_python", "database", "web_search"], locked).items()
        if p.require_approval is True
    }
    assert configured == {"run_python", "query_database", "web_search"}
    assert registry.builtin_policies([], SECURITY) == {}


def test_agent_tool_makes_a_tool_and_a_policy() -> None:
    @agent_tool(label="受注の合計", grounding=True, max_calls_per_run=3)
    async def order_total(customer: str, limit: int = 10) -> str:
        """取引先の受注の合計を返す。

        Args:
            customer: 取引先の名前
            limit: 数える件数の上限
        """
        return f"{customer}: {limit}"

    assert isinstance(order_total, AgentTool) and order_total.name == "order_total"
    assert order_total.tool.description == "取引先の受注の合計を返す。"
    assert order_total.tool.args["customer"]["description"] == "取引先の名前"
    assert order_total.policy == ToolPolicy(
        "受注の合計", check_arguments=True, grounding=True, max_calls_per_run=3
    )  # 既定: 確かめない、毎回確認では確かめる、送る前に引数を確かめる
    assert asyncio.run(order_total.tool.ainvoke({"customer": "はまなす商事"})) == "はまなす商事: 10"


def test_errors_inside_a_tool_reach_the_model_without_details() -> None:
    @agent_tool(label="社内 API")
    def broken(customer: str) -> str:
        """壊れたツール。"""
        raise ConnectionError("https://secret.internal/api?token=abc につながらない")

    @agent_tool(label="社内 API")
    def explained(customer: str) -> str:
        """理由を伝えるツール。"""
        raise ToolException("その取引先は登録されていません")

    call = {"name": "broken", "args": {"customer": "x"}, "id": "c1", "type": "tool_call"}
    result = broken.tool.invoke(call)
    assert result.status == "error" and result.content == "社内 APIでエラーが起きました（ConnectionError）。"
    assert "secret.internal" not in result.content
    result = explained.tool.invoke({**call, "name": "explained"})
    assert result.status == "error" and result.content == "その取引先は登録されていません"


@pytest.mark.parametrize("name", ["write_file", "task", "1abc"])
def test_tool_names_cannot_replace_builtin_tools(name: str) -> None:
    def func(x: str) -> str:
        """説明。"""
        return x

    func.__name__ = name
    with pytest.raises(ValueError):
        agent_tool(label="x")(func)


def test_a_tool_needs_a_description() -> None:
    def quiet(x: str) -> str:
        return x

    with pytest.raises(ValueError, match="docstring"):
        agent_tool(label="x")(quiet)


def test_names_must_not_overlap_between_kinds(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings_module, "PACKS_DIR", tmp_path)
    tools_py = (
        "from data_agent.tools.registry import agent_tool\n"
        "@agent_tool(label='a')\n"
        "def web_search(q: str) -> str:\n"
        "    '''組み込みと同じ名前。'''\n"
        "    return q\n"
    )
    _write_pack(tmp_path, "clash", "title: a\ngreeting: b\nbuiltin_tools: [web_search]\npython_tools: [tools.py]\n",
                {"SYSTEM.md": "役割", "tools.py": tools_py})  # fmt: skip
    with pytest.raises(RuntimeError, match="重なっています"):
        registry.policies(load_pack("clash"), SECURITY)
    _write_pack(
        tmp_path, "empty", "title: a\ngreeting: b\npython_tools: [tools.py]\n", {"SYSTEM.md": "役割", "tools.py": "x = 1\n"}
    )
    with pytest.raises(RuntimeError, match="@agent_tool"):
        registry.python_tools(load_pack("empty"))


def test_example_pack_policies() -> None:
    policies = registry.policies(load_pack("internal_api_example"), SECURITY)
    assert set(policies) == {
        "run_python",
        "price_with_tax",
        "orders__search_orders",
        "orders__get_order",
        "orders__update_order_status",
    }
    update = policies["orders__update_order_status"]
    assert update.require_approval is True and update.max_calls_per_run == 3 and update.check_arguments
    assert policies["orders__search_orders"].require_approval is False and policies["orders__search_orders"].grounding
    config = registry.grounding_config(SECURITY, policies)
    assert {"orders__search_orders", "orders__get_order", "price_with_tax"} <= set(config.tools)
    assert registry.grounding_labels(policies)["orders__get_order"] == "受注の詳細"


# ---------- ガードレール: 独自のルール ----------
def test_custom_rules_mask_use_case_identifiers() -> None:
    config = GuardrailConfig.model_validate(
        {"custom_rules": [{"kind": "employee_id", "label": "社員番号", "pattern": r"EMP-\d{6}"}]}
    )
    rule = config.custom_rules[0]
    guard = Guard(config.actions, custom=[(custom_rule(rule.kind, rule.label, rule.pattern), rule.action)])
    masked, findings = guard.scan("担当は EMP-123456 です（連絡先 taro@example.com）")
    assert masked == "担当は [マスク済み:社員番号] です（連絡先 [マスク済み:メールアドレス]）"
    assert {f.kind for f in findings} == {"employee_id", "email"}
    assert guard.labels["employee_id"] == "社員番号"


@pytest.mark.parametrize(
    ("rule", "message"),
    [
        ({"kind": "email", "label": "x", "pattern": "a"}, "元からある"),
        ({"kind": "Bad", "label": "x", "pattern": "a"}, "英小文字"),
        ({"kind": "code", "label": "x", "pattern": "("}, "正規表現"),
        ({"kind": "code", "label": "x", "pattern": "a*"}, "空の文字列"),
        ({"kind": "code", "label": "a]b", "pattern": "a"}, "label"),
        ({"kind": "code", "label": "x", "pattern": "a", "acton": "mask"}, "acton"),
    ],
)
def test_bad_custom_rules_are_refused(rule: dict, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        GuardrailConfig.model_validate({"custom_rules": [rule]})


# ---------- ガードレール: 外に送るツールの引数 ----------
def _tool_request(name: str, args: dict) -> SimpleNamespace:
    return SimpleNamespace(tool_call={"name": name, "args": args, "id": "call-1"}, tool=None, state={}, runtime=None)


@pytest.fixture
def audit_events(monkeypatch) -> list[dict]:
    from data_agent.safety import guard_middleware

    events: list[dict] = []
    monkeypatch.setattr(guard_middleware, "audit", lambda event, **fields: events.append({"event": event, **fields}))
    return events


def test_arguments_with_secrets_are_not_sent(audit_events: list[dict]) -> None:
    middleware = GuardrailMiddleware(Guard(), check_arguments={"orders__search_orders"})
    key = "sk-ant-api03-ARGS0000ARGS0000ARGS0000"
    called: list[Any] = []
    result = middleware.wrap_tool_call(
        _tool_request("orders__search_orders", {"customer": f"鍵 {key}", "note": "taro@example.com"}), called.append
    )
    assert called == [] and result.status == "error"
    assert "送るのをやめました" in result.content and "秘密情報" in result.content and "メールアドレス" in result.content
    denied = [e for e in audit_events if e["event"] == "send_denied"]
    assert denied == [{"event": "send_denied", "tool": "orders__search_orders", "kinds": ["email", "secret"]}]
    assert key not in json.dumps(audit_events)


def test_audit_keeps_paths_only_for_file_tools(audit_events: list[dict]) -> None:
    """MCP などの道具の引数（path という名前でも）は、データのことがあるので、監査ログに残さない。"""
    middleware = GuardrailMiddleware(Guard())
    ok = ToolMessage(content="ok", tool_call_id="call-1")
    middleware.wrap_tool_call(_tool_request("read_file", {"file_path": "/data/a.csv"}), lambda _: ok)
    middleware.wrap_tool_call(_tool_request("orders__lookup", {"path": "/customers/はまなす商事"}), lambda _: ok)
    calls = [e for e in audit_events if e["event"] == "tool_call"]
    assert calls[0]["path"] == "/data/a.csv" and calls[1]["tool"] == "orders__lookup" and calls[1].get("path") is None


def test_clean_arguments_and_unchecked_tools_go_through() -> None:
    middleware = GuardrailMiddleware(Guard(), check_arguments={"orders__search_orders"})
    ok = ToolMessage(content="ok", tool_call_id="call-1")
    assert middleware.wrap_tool_call(_tool_request("orders__search_orders", {"customer": "はまなす商事"}), lambda _: ok) is ok
    # 確かめると決めていないツール（ファイルの読み取りなど）は、引数を見ない
    assert middleware.wrap_tool_call(_tool_request("read_file", {"file_path": "/taro@example.com.txt"}), lambda _: ok) is ok


def test_masked_placeholders_in_arguments_are_not_blocked() -> None:
    """入口でマスクした値をモデルがそのまま渡しても、送るのは「伏せた印」だけなので止めない。"""
    middleware = GuardrailMiddleware(Guard(), check_arguments={"orders__search_orders"})
    ok = ToolMessage(content="ok", tool_call_id="call-1")
    request = _tool_request("orders__search_orders", {"customer": "[マスク済み:メールアドレス]"})
    assert middleware.wrap_tool_call(request, lambda _: ok) is ok


# ---------- 確認: パックのツール ----------
def _policy(**kwargs: Any) -> ApprovalPolicy:
    return ApprovalPolicy(permissions=[], backend=None, **kwargs)


def test_pack_tools_are_described_with_their_labels() -> None:
    rules = _policy(configured=["orders__update_order_status"], labels={"orders__update_order_status": "受注の状態の変更"})
    review = rules.review("orders__update_order_status", {"order_id": "ORD-1001", "status": "出荷済み"}, STANDARD)
    assert review is not None and review.kind == approvals.CONFIGURED
    assert review.title == "受注の状態の変更（orders__update_order_status）を実行します"
    assert '"order_id": "ORD-1001"' in review.detail
    assert "受注の状態の変更" in rules.unattended_note()


def test_suspicious_pack_tools_explain_that_the_content_is_sent() -> None:
    hits = [search_screen.Hit(search_screen.LOCAL, "aoba-7q4kx", "read_file /x")]
    rules = _policy(screened=["orders__search_orders"], labels={"orders__search_orders": "受注の検索"})
    review = rules.review("orders__search_orders", {"customer": "AOBA-7Q4KX"}, STANDARD, hits)
    assert review is not None and review.kind == SUSPICIOUS
    assert "ツールの送り先に送られます" in review.reason and "検索のために" not in review.reason
    assert rules.kind("orders__search_orders", {"customer": "x"}, STANDARD, []) is None
    assert "受注の検索（外に出したくない情報らしいものを含むとき）" in rules.unattended_note()


def test_strict_mode_asks_for_pack_tools() -> None:
    rules = _policy(strict=[*approvals.STRICT_FILE_TOOLS, "orders__search_orders"])
    assert rules.kind("orders__search_orders", {"customer": "x"}, STRICT, []) == approvals.STRICT
    assert rules.kind("orders__search_orders", {"customer": "x"}, STANDARD, []) is None


# ---------- 送る内容の判定: パックのツール（エージェントに組み込んだとき） ----------
SECRET_FILE = "# 来期の見通し（社外秘）\n\n- 未公表の取引先のコード: AOBA-7Q4KX\n"


class Scripted(BaseChatModel):
    replies: list[AIMessage] = []

    @property
    def _llm_type(self) -> str:
        return "scripted-for-tests"

    def _generate(self, messages: list, stop: Any = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
        message = self.replies.pop(0) if self.replies else AIMessage("おわり")
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(self, tools: Any, **kwargs: Any) -> Scripted:
        return self


def _calls(name: str, args: dict) -> AIMessage:
    return AIMessage("", tool_calls=[{"name": name, "args": args, "id": f"call-{uuid.uuid4().hex[:6]}", "type": "tool_call"}])


def test_in_the_agent_copying_read_data_into_a_pack_tool_stops_until_someone_answers() -> None:
    sent: list[str] = []

    @tool
    def read_file(file_path: str) -> str:
        """ファイルを読む。"""
        return SECRET_FILE

    @tool("orders__search_orders")
    def search_orders(customer: str) -> str:
        """受注を探す。"""
        sent.append(customer)
        return "なし"

    name = "orders__search_orders"
    rules = _policy(screened=[name], labels={name: "受注の検索"})
    model = Scripted(replies=[_calls("read_file", {"file_path": "/plan.md"}), _calls(name, {"customer": "AOBA-7Q4KX"})])
    agent = create_agent(
        model=model,
        tools=[read_file, search_orders],
        middleware=[
            search_screen.SearchScreenMiddleware(lambda: WatchTerms(), root=True, screened={name: ()}),
            HumanInTheLoopMiddleware(interrupt_on=rules.interrupt_on()),
        ],
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "t1"}}
    agent.invoke({"messages": [HumanMessage("来期の見通しをまとめて")]}, config)
    state = agent.get_state(config)
    assert len(state.interrupts) == 1 and sent == []
    action = state.interrupts[0].value["action_requests"][0]
    review = rules.review_action(action, STANDARD)
    assert review is not None and review.kind == SUSPICIOUS and "aoba-7q4kx" in review.detail


def test_sent_text_reads_the_chosen_arguments() -> None:
    args = {"customer": "はまなす商事", "filters": {"status": "受付"}, "limit": None}
    assert search_screen.sent_text(args, ()) == 'はまなす商事\n{"status": "受付"}'
    assert search_screen.sent_text(args, ("customer",)) == "はまなす商事"


def test_pack_config_rejects_unknown_builtin_tools() -> None:
    with pytest.raises(ValidationError):
        PackConfig.model_validate({"title": "a", "greeting": "b", "builtin_tools": ["shell"]})


# ---------- 手で確かめるコマンド ----------
def test_the_pack_command_lists_tools_and_their_rules() -> None:
    """python -m で動かしても、パックの Python のツールを見落とさない（モジュールが 2 回読み込まれる落とし穴）。"""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "data_agent.tools.registry", "--pack", "internal_api_example"],
        capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    assert result.returncode == 0, result.stdout + result.stderr
    assert "price_with_tax / 税込みの金額 / 確認: しない" in result.stdout
    assert "orders__update_order_status / 受注の状態の変更 / 確認: 毎回 / 確かめる / する / 3" in result.stdout
    missing = subprocess.run(
        [sys.executable, "-m", "data_agent.tools.registry", "--pack", "nope"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert missing.returncode == 1 and "あるもの: data、internal_api_example" in missing.stdout


def test_the_guardrail_command_shows_what_is_masked(capsys) -> None:
    from data_agent.safety import guardrails

    assert guardrails._main(["担当は taro@example.com です"]) == 0
    out = capsys.readouterr().out
    assert "結果: 担当は [マスク済み:メールアドレス] です" in out and "メールアドレス（email）: 1 件、扱い: mask" in out
