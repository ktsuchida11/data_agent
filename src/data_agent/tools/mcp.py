"""MCP サーバー（HTTP でつなぐもの。社内の API など）のツールを、エージェントのツールにする（ADR-0033）。

つなぐサーバーと、使うツールは、用途パックの pack.yaml の mcp_servers に書く。

- 使うのは、pack.yaml の tools に書いたツールだけ。サーバーにほかのツールがあっても、エージェントには見せない。
- ツールの名前は「サーバーの名前__ツールの名前」にする（組み込みのツールや、ほかのサーバーと重ならないように）。
- 扱い（確認、回数の上限、照合）は、pack.yaml の設定から作る（registry.ToolPolicy）。確認の既定は「毎回」。
- サーバーが返す説明と結果は、外から来たデータとして扱う。説明はガードレールで調べてから見せ、長さを切る
  （pack.yaml の description で、自分で書いた説明に差し替えられる）。結果は、ほかのツールと同じく、
  会話に入る前にガードレールでマスクする（guard_middleware.py）。長すぎる結果は切る。
- サーバーが返す「構造化した結果」（structuredContent）は使わない。マスクを通らずに、会話の保存先に残るため。
- つなぐのは、アプリの起動のとき（load）。つながらない、書いたツールがない、のどちらかなら、起動を止める
  （必要なツールがないまま、動いているように見せないため）。

確かめるとき（サーバーにつないで、ツールの一覧を出す。何も実行しない）:
    uv run python -m data_agent.tools.mcp
    docker compose exec data-agent python -m data_agent.tools.mcp   （コンテナの中から）
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool, ToolException

from data_agent.safety.audit import audit
from data_agent.safety.guardrails import Guard
from data_agent.settings import McpServerConfig, McpToolConfig, Pack
from data_agent.tools import internal_http
from data_agent.tools.registry import AgentTool, ToolPolicy

SEPARATOR = "__"
MAX_DESCRIPTION = 2_000  # サーバーが返す説明の長さの上限
MAX_PARAMETER_TEXT = 300  # 引数の説明と題名の長さの上限（引数の説明は、pack.yaml で差し替えられないため短くする）

_loaded: dict[str, tuple[AgentTool, ...]] = {}


def tool_name(server: str, tool: str) -> str:
    return f"{server}{SEPARATOR}{tool}"


def enabled_servers(pack: Pack) -> dict[str, McpServerConfig]:
    return {name: server for name, server in pack.config.mcp_servers.items() if server.enabled}


def policy_for(server: McpServerConfig, name: str, config: McpToolConfig) -> ToolPolicy:
    return ToolPolicy(
        label=config.label or name,
        require_approval=config.require_approval,
        check_arguments=server.check_arguments,
        grounding=config.grounding,
        max_calls_per_run=config.max_calls_per_run,
    )


def policies(pack: Pack) -> dict[str, ToolPolicy]:
    """pack.yaml に書いた MCP のツールの扱い（サーバーにはつながない）。"""
    return {
        tool_name(server_name, name): policy_for(server, name, config)
        for server_name, server in enabled_servers(pack).items()
        for name, config in server.tools.items()
    }


def connection(server_name: str, server: McpServerConfig) -> dict[str, Any]:
    """langchain-mcp-adapters に渡す、つなぎ方（streamable HTTP）。鍵は、ここで環境変数から読む。"""
    internal_http.check_url(server.url, allow_http=server.allow_http)
    where = f"mcp_servers.{server_name}.headers"
    return {
        "transport": "streamable_http",
        "url": server.url,
        "headers": {name: internal_http.expand_env(value, where) for name, value in server.headers.items()},
        "timeout": server.timeout_seconds,
        "sse_read_timeout": server.timeout_seconds,
        "httpx_client_factory": internal_http.make_client_factory(internal_http.ssl_context(server.ca_file)),
    }


async def list_server_tools(server_name: str, server: McpServerConfig) -> list[BaseTool]:
    """サーバーにつないで、ツールの一覧を受け取る。つながらなければ RuntimeError（理由は日本語）。"""
    from langchain_mcp_adapters.client import MultiServerMCPClient

    client = MultiServerMCPClient({server_name: connection(server_name, server)})  # type: ignore[dict-item]
    try:
        return await client.get_tools(server_name=server_name)
    except Exception as exc:
        reason = internal_http.describe_error(exc)
        raise RuntimeError(f"MCP サーバー {server_name} から、ツールの一覧を受け取れませんでした（{reason}）") from exc


async def load(pack: Pack, guard: Guard) -> tuple[AgentTool, ...]:
    """パックの MCP サーバーにつないで、使うツールを作る。アプリの起動のときに、エージェントを組み立てる前に呼ぶ。"""
    tools: list[AgentTool] = []
    for server_name, server in enabled_servers(pack).items():
        available = {tool.name: tool for tool in await list_server_tools(server_name, server)}
        missing = [name for name in server.tools if name not in available]
        if missing:
            raise RuntimeError(
                f"MCP サーバー {server_name} に、pack.yaml に書いたツールがありません: {'、'.join(missing)}"
                f"（サーバーにあるもの: {'、'.join(sorted(available)) or 'なし'}）"
            )
        for name, config in server.tools.items():
            tools.append(_wrap(server_name, server, name, config, available[name], guard))
        audit("mcp_tools_loaded", server=server_name, tools=len(server.tools), available=len(available))
    _loaded[pack.name] = tuple(tools)
    return _loaded[pack.name]


def loaded_tools(pack: Pack) -> tuple[AgentTool, ...]:
    """load で作ったツール。MCP サーバーを使わないパックなら空。"""
    if not enabled_servers(pack):
        return ()
    if pack.name not in _loaded:
        raise RuntimeError("MCP のツールを読み込む前に、エージェントを組み立てようとしました（先に mcp.load を呼んでください）")
    return _loaded[pack.name]


def clean_schema(schema: Any, guard: Guard) -> Any:
    """サーバーが返す引数の形（JSON Schema）の、説明・題名・例の文字列を、伏せて短くする。型と必須は変えない。

    引数の説明も、ツールの説明と同じく、モデルが読む。指示や秘密情報を仕込む場所にならないようにする。
    """
    if isinstance(schema, list):
        return [clean_schema(item, guard) for item in schema]
    if not isinstance(schema, dict):
        return schema
    cleaned: dict[str, Any] = {}
    for key, value in schema.items():
        if key in ("description", "title") and isinstance(value, str):
            cleaned[key] = guard.scan(value[:MAX_PARAMETER_TEXT])[0]
        elif key in ("default", "examples", "enum", "const"):
            cleaned[key] = _mask_strings(value, guard)
        else:
            cleaned[key] = clean_schema(value, guard)
    return cleaned


def _mask_strings(value: Any, guard: Guard) -> Any:
    if isinstance(value, str):
        return guard.scan(value[:MAX_PARAMETER_TEXT])[0]
    if isinstance(value, list):
        return [_mask_strings(item, guard) for item in value]
    return value


def _wrap(
    server_name: str, server: McpServerConfig, name: str, config: McpToolConfig, original: BaseTool, guard: Guard
) -> AgentTool:
    policy = policy_for(server, name, config)
    label = policy.label
    description = config.description or (original.description or "").strip() or label
    description, _ = guard.scan(description[:MAX_DESCRIPTION])  # サーバーの説明に、秘密情報が入っていたら伏せる
    limit = server.max_output_chars
    call_original = original.coroutine
    if call_original is None:
        raise RuntimeError(f"MCP のツール {name} を呼べる形にできませんでした")

    async def call(**arguments: Any) -> Any:
        try:
            content, _structured = await call_original(**arguments)
        except ToolException as exc:  # サーバーが「失敗した」と返した。中身はデータとして、そのまま返す
            raise ToolException(clip(_text(getattr(exc, "tool_content", None)) or str(exc), limit)) from exc
        except Exception as exc:  # つながらないなど。止まらずに、モデルに伝えて続ける（接続先や鍵は出さない）
            raise ToolException(f"{label}を呼べませんでした（{internal_http.describe_error(exc)}）。") from exc
        return clip_content(content, limit)

    tool = StructuredTool(
        name=tool_name(server_name, name),
        description=description,
        args_schema=clean_schema(original.args_schema, guard),
        coroutine=call,
        handle_tool_error=True,
        metadata={"mcp_server": server_name, "mcp_tool": name},
    )
    return AgentTool(tool, policy)


# ---------- 結果の形を整える ----------
def _text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for block in content if isinstance(content, list) else [content]:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "\n".join(parts)


def clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n…（長すぎるので、ここまで。全体は {len(text):,} 文字）"


def clip_content(content: Any, limit: int) -> Any:
    """結果を、モデルに渡せる形にする。文章はまとめて長さを切る。画像はそのまま、ほかの形は名前だけにする。"""
    if isinstance(content, str):
        return clip(content, limit)
    blocks = content if isinstance(content, list) else [content]
    texts: list[str] = []
    images: list[dict[str, Any]] = []
    for block in blocks:
        if isinstance(block, str):
            texts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            texts.append(str(block.get("text", "")))
        elif isinstance(block, dict) and block.get("type") == "image":
            images.append(block)
        else:
            kind = block.get("type") if isinstance(block, dict) else type(block).__name__
            texts.append(f"（{kind} の形の結果は、表示できないので省きました）")
    text = clip("\n".join(texts), limit)
    if not images:
        return text
    return [{"type": "text", "text": text}, *images]


# ---------- 手で確かめる（python -m data_agent.tools.mcp） ----------
async def _report(pack: Pack, guard: Guard) -> int:
    servers = enabled_servers(pack)
    if not servers:
        print(f"用途パック {pack.name} には、使う MCP サーバーがありません（pack.yaml の mcp_servers）。")
        return 0
    failed = False
    for server_name, server in servers.items():
        print(f"■ {server_name}（{server.url}）")
        try:
            available = await list_server_tools(server_name, server)
        except RuntimeError as exc:
            print(f"  {exc}")
            failed = True
            continue
        for tool in sorted(available, key=lambda t: t.name):
            used = "使う  " if tool.name in server.tools else "使わない"
            summary, _ = guard.scan(" ".join((tool.description or "").split())[:80])
            params = ", ".join(_params(tool.args_schema))
            print(f"  [{used}] {tool.name}({params})  {summary}")
        names = {tool.name for tool in available}
        for missing in [name for name in server.tools if name not in names]:
            print(f"  [ない  ] {missing}  ← pack.yaml に書いてあるが、サーバーにない")
            failed = True
    return 2 if failed else 0


def _params(schema: Any) -> list[str]:
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    return list(properties)


def _main(argv: list[str] | None = None) -> int:
    from data_agent.settings import get_guard, get_settings, load_pack  # .env もここで読まれる

    parser = argparse.ArgumentParser(
        prog="python -m data_agent.tools.mcp",
        description="用途パックの MCP サーバーにつないで、ツールの一覧を出す（ツールは実行しない）",
    )
    parser.add_argument("--pack", default=None, help="用途パックの名前（既定は .env の AGENT_PACK）")
    args = parser.parse_args(argv)
    try:
        pack = load_pack(args.pack or get_settings().agent_pack)
        return asyncio.run(_report(pack, get_guard()))
    except RuntimeError as exc:
        print(exc)
        return 1


if __name__ == "__main__":
    # python -m で動かすと、このファイルは __main__ として、もう一度読み込まれる。パックの tools.py が使うのは
    # data_agent.tools.mcp のほうなので、そちらの _main を使う（AgentTool を同じものとして見分けるため）
    from data_agent.tools.mcp import _main as main

    raise SystemExit(main())
