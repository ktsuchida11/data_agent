"""MCP サーバーのツール（tools/mcp.py、tools/internal_http.py。ADR-0033）。

社内 API の見本（examples/internal_api/server.py）を、手元に http で立てて、実際に MCP でつなぐ。
HTTPS（社内の認証局の証明書）は、作業用のサンドボックスでは証明書のファイルを読めないので、
画面の回帰確認（tests/e2e/check_mcp.py。Docker の中）で確かめる。
"""

from __future__ import annotations

import asyncio
import importlib.util
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from langchain_core.tools import StructuredTool, ToolException
from pydantic import ValidationError

from data_agent.safety.guardrails import Guard
from data_agent.settings import McpServerConfig, McpToolConfig, Pack, PackConfig
from data_agent.tools import internal_http, mcp

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "test-token-0123456789abcdef"
FAKE_KEY = "sk-ant-api03-DESC0000DESC0000DESC0000"


@pytest.fixture(autouse=True)
def no_certificate_files(monkeypatch) -> None:
    # 作業用のサンドボックスは、証明書の束（*.pem）を読めない。http で立てたサーバーにだけつなぐので、使わない
    monkeypatch.setattr(internal_http, "ssl_context", lambda ca_file: False)


@pytest.fixture(scope="module")
def example_url() -> Any:
    spec = importlib.util.spec_from_file_location("example_internal_api", ROOT / "examples/internal_api/server.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["example_internal_api"] = module
    spec.loader.exec_module(module)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            module.BearerAuth(module.mcp.streamable_http_app(), TOKEN), host="127.0.0.1", port=port, log_level="warning"
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(5)


def _data(url: str) -> dict[str, Any]:
    return {
        "url": f"{url}/mcp",
        "allow_http": True,
        "headers": {"Authorization": "Bearer ${TEST_MCP_TOKEN}"},
        "tools": {"search_orders": {"label": "受注の検索", "require_approval": False, "grounding": True}, "get_order": {}},
    }


def _server(url: str, **overrides: Any) -> McpServerConfig:
    return McpServerConfig.model_validate({**_data(url), **overrides})


def _pack(name: str = "t", **servers: McpServerConfig) -> Pack:
    return Pack(name, ROOT, PackConfig(title="t", greeting="t", mcp_servers=servers))


@pytest.fixture
def token(monkeypatch) -> None:
    monkeypatch.setenv("TEST_MCP_TOKEN", TOKEN)


# ---------- 設定 ----------
@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"url": "http://x/mcp", "allow_http": False}, "allow_http"),
        ({"url": "ftp://x/mcp"}, "https://"),
        ({"url": "https://user:pw@x/mcp"}, "名前やパスワード"),
        ({"headers": {"Authorization": "Bearer abc"}}, "環境変数名"),
        ({"headers": {"X-Api-Key": "abc"}}, "環境変数名"),
        ({"tools": {}}, "1 つ以上"),
        ({"tools": {"bad name": {}}}, "英数字"),
        ({"timout_seconds": 3}, "timout_seconds"),  # 書き間違いに気づけるように
    ],
)
def test_server_settings_refuse_risky_or_wrong_values(change: dict, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        McpServerConfig.model_validate({**_data("https://x"), **change})


def test_plain_headers_and_env_references_are_accepted() -> None:
    server = _server(
        "https://orders.example.internal", allow_http=False, headers={"X-Client": "data-agent", "Authorization": "Bearer ${T}"}
    )
    assert server.headers["X-Client"] == "data-agent"
    with pytest.raises(ValidationError, match="20 文字以内"):
        PackConfig(title="t", greeting="t", mcp_servers={"Bad-Name": server})


def test_policies_come_from_the_settings_without_connecting() -> None:
    policies = mcp.policies(_pack(srv=_server("https://x")))
    assert set(policies) == {"srv__search_orders", "srv__get_order"}
    search, detail = policies["srv__search_orders"], policies["srv__get_order"]
    assert search.label == "受注の検索" and search.require_approval is False and search.grounding
    assert detail.label == "get_order" and detail.require_approval is True  # 既定は「毎回確かめる」
    assert search.check_arguments and detail.check_arguments  # 既定は、送る前に引数を確かめる
    off = _server("https://x", enabled=False)
    assert mcp.policies(_pack(srv=off)) == {}


def test_keys_are_read_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("TEST_MCP_TOKEN", "value-from-env")
    connection = mcp.connection("srv", _server("https://x"))
    assert connection["headers"] == {"Authorization": "Bearer value-from-env"}
    assert connection["transport"] == "streamable_http"
    monkeypatch.delenv("TEST_MCP_TOKEN")
    with pytest.raises(RuntimeError, match=r"\$\{TEST_MCP_TOKEN\}.*\.env"):
        mcp.connection("srv", _server("https://x"))


def test_ca_file_must_exist_inside_the_project() -> None:
    with pytest.raises(RuntimeError, match="ありません"):
        internal_http.ca_path("private/no-such-ca.crt")
    with pytest.raises(RuntimeError, match="プロジェクトのフォルダの中"):
        internal_http.ca_path("../outside.crt")


def test_clients_do_not_follow_redirects_or_read_proxy_settings() -> None:
    client = internal_http.make_client_factory(False)({"Authorization": "Bearer x"}, httpx.Timeout(5))
    assert client.follow_redirects is False and client._trust_env is False
    asyncio.run(client.aclose())


# ---------- サーバーにつなぐ ----------
def test_only_listed_tools_are_used(example_url: str, token: None) -> None:
    pack = _pack(srv=_server(example_url))
    tools = asyncio.run(mcp.load(pack, Guard()))
    assert [t.name for t in tools] == ["srv__search_orders", "srv__get_order"]  # サーバーにある delete_order などは使わない
    assert mcp.loaded_tools(pack) == tools
    search = tools[0].tool
    assert "取引先の名前" in search.description
    assert set(search.args) == {"customer"}
    result = asyncio.run(search.ainvoke({"customer": "はまなす商事"}))
    assert "ORD-1001" in result and "ORD-1003" not in result
    detail = asyncio.run(tools[1].tool.ainvoke({"order_id": "ORD-1003"}))
    assert "あおば電機" in detail  # 連絡先のマスクは、会話に入る前にガードレールで行う（ここではツールの結果のまま）


def test_descriptions_can_be_replaced_and_are_checked(example_url: str, token: None) -> None:
    server = _server(example_url, tools={"search_orders": {"description": f"受注を探す。鍵 {FAKE_KEY}"}})
    tools = asyncio.run(mcp.load(_pack("t2", srv=server), Guard()))
    assert tools[0].tool.description == "受注を探す。鍵 [マスク済み:秘密情報]"


def test_a_missing_tool_stops_the_start(example_url: str, token: None) -> None:
    server = _server(example_url, tools={"search_orders": {}, "export_everything": {}})
    with pytest.raises(RuntimeError, match="export_everything") as error:
        asyncio.run(mcp.load(_pack("t3", srv=server), Guard()))
    assert "delete_order" in str(error.value)  # サーバーにあるものを示す


def test_a_wrong_key_is_reported_plainly(example_url: str, monkeypatch) -> None:
    monkeypatch.setenv("TEST_MCP_TOKEN", "wrong-token-000000000000")
    with pytest.raises(RuntimeError, match="鍵が違うか、権限がありません（401）"):
        asyncio.run(mcp.load(_pack("t4", srv=_server(example_url)), Guard()))


def test_a_stopped_server_is_reported_plainly(token: None) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]  # 閉じたので、誰も待っていない
    with pytest.raises(RuntimeError, match="サーバーにつながりませんでした"):
        asyncio.run(mcp.load(_pack("t5", srv=_server(f"http://127.0.0.1:{port}")), Guard()))


def test_the_agent_cannot_be_built_before_loading() -> None:
    with pytest.raises(RuntimeError, match=r"mcp\.load"):
        mcp.loaded_tools(_pack("never-loaded", srv=_server("https://x")))
    assert mcp.loaded_tools(_pack("no-servers")) == ()


# ---------- ツールを呼んだときの失敗と、結果の長さ ----------
def _wrapped(coroutine: Any, limit: int = 1_000) -> StructuredTool:
    original = StructuredTool(name="search_orders", description="受注を探す", args_schema={"type": "object", "properties": {"customer": {"type": "string"}}}, coroutine=coroutine)  # fmt: skip
    server = _server("https://x", max_output_chars=limit)
    return mcp._wrap("srv", server, "search_orders", McpToolConfig(label="受注の検索"), original, Guard()).tool


def _call(tool: StructuredTool) -> Any:
    return asyncio.run(tool.ainvoke({"name": tool.name, "args": {"customer": "x"}, "id": "c1", "type": "tool_call"}))


def test_connection_failures_during_a_call_reach_the_model_without_details() -> None:
    async def broken(**arguments: Any) -> Any:
        raise httpx.ConnectError("https://orders.internal/mcp?token=abc に届かない")

    result = _call(_wrapped(broken))
    assert result.status == "error" and result.content == "受注の検索を呼べませんでした（サーバーにつながりませんでした）。"


def test_errors_reported_by_the_server_are_passed_on_as_data() -> None:
    async def refused(**arguments: Any) -> Any:
        error = ToolException("x")
        error.tool_content = [{"type": "text", "text": "その取引先は見つかりません"}]  # type: ignore[attr-defined]
        raise error

    result = _call(_wrapped(refused))
    assert result.status == "error" and result.content == "その取引先は見つかりません"


def test_long_results_are_cut_and_structured_results_are_dropped() -> None:
    async def long(**arguments: Any) -> Any:
        return [{"type": "text", "text": "あ" * 3_000}], {"structured_content": {"raw": "マスクを通らない値"}}

    result = _call(_wrapped(long, limit=1_000))
    assert result.content.startswith("あ" * 1_000) and "全体は 3,000 文字" in result.content
    assert result.artifact is None


def test_result_blocks_are_kept_in_shapes_the_model_can_read() -> None:
    image = {"type": "image", "base64": "AAAA", "mime_type": "image/png"}
    assert mcp.clip_content(["一", {"type": "text", "text": "二"}], 100) == "一\n二"
    assert mcp.clip_content([{"type": "text", "text": "表"}, image], 100) == [{"type": "text", "text": "表"}, image]
    assert "file の形の結果は、表示できない" in mcp.clip_content([{"type": "file", "url": "x"}], 100)


# ---------- 手で確かめる ----------
def test_the_check_command_lists_used_and_unused_tools(example_url: str, token: None, capsys) -> None:
    assert asyncio.run(mcp._report(_pack(srv=_server(example_url)), Guard())) == 0
    out = capsys.readouterr().out
    assert "[使う  ] search_orders(customer)" in out and "[使わない] delete_order(order_id)" in out
    missing = _server(example_url, tools={"search_orders": {}, "export_everything": {}})
    assert asyncio.run(mcp._report(_pack(srv=missing), Guard())) == 2
    assert "[ない  ] export_everything" in capsys.readouterr().out


def test_parameter_descriptions_are_masked_and_clipped() -> None:
    """引数の説明も、モデルが読む。秘密情報を伏せて、短くする。型と必須は変えない。"""
    schema = {
        "type": "object",
        "properties": {
            "customer": {"type": "string", "description": f"取引先の名前。鍵 {FAKE_KEY}" + "あ" * 1000},
            "status": {"type": "string", "enum": ["受付", f"鍵 {FAKE_KEY}"], "title": "状態"},
        },
        "required": ["customer"],
    }
    cleaned = mcp.clean_schema(schema, Guard())
    customer = cleaned["properties"]["customer"]
    assert FAKE_KEY not in str(cleaned)
    assert customer["description"].startswith("取引先の名前。鍵 [マスク済み:秘密情報]")
    assert len(customer["description"]) <= mcp.MAX_PARAMETER_TEXT
    assert cleaned["properties"]["status"]["enum"][0] == "受付"
    assert cleaned["type"] == "object" and cleaned["required"] == ["customer"] and customer["type"] == "string"
