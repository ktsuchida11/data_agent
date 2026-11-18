"""エージェントのツールの一覧と、ツールごとの扱い（ADR-0032）。

ツールを足すときは、ツールの中身と、その「扱い」（ToolPolicy）を書くだけでよい。次のものは、扱いから自動で決まる。
- 実行の前に利用者に確かめるか（approvals.py）。「毎回確認」のチャットで確かめるか
- 送る内容に、外に出したくない情報らしいものがあるときだけ確かめる、の判定（search_screen.py）
- 送る前に、引数の秘密情報や個人情報を確かめるか（guard_middleware.py）
- 1 回の依頼で呼べる回数（limits.py）
- 返事の数の照合で、結果を根拠のデータとして扱うか（grounding.py）
- 確認の画面や、定時実行の説明に出す名前

ツールの種類は 3 つ。どれを使うかは、用途パック（packs/<名前>/pack.yaml）で決める。
- 組み込み（run_python・database・web_search）: 扱いは config/security.yaml から作る
- Python で書いたツール: パックの python_tools のファイルに、@agent_tool を付けた関数を書く
- MCP サーバーのツール: パックの mcp_servers に書く（tools/mcp.py）
"""

from __future__ import annotations

import functools
import importlib.util
import inspect
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from langchain_core.tools import BaseTool, StructuredTool, ToolException

from data_agent.settings import GroundingConfig, Pack, SecurityConfig, get_pack, get_security
from data_agent.tools import code_execution, database, web_search

Approval = bool | Literal["auto"]

# Deep Agents が元から持っているツール。同じ名前のツールは足せない（中身をすり替えられないように）
RESERVED = frozenset({"ls", "read_file", "write_file", "edit_file", "glob", "grep", "delete", "execute", "task", "write_todos"})
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}")


@dataclass(frozen=True)
class ToolPolicy:
    """ツールの扱い。"""

    label: str  # 確認の画面や、定時実行の説明に出す名前（例: 受注の検索）
    # 実行の前に利用者に確かめるか。True: どのチャットでも毎回、"auto": 送る内容に、外に出したくない情報らしいもの
    # （このチャットで読んだデータの写し、見張る言葉）があるときだけ、False: 確かめない
    require_approval: Approval = False
    strict: bool = True  # 「毎回確認」のチャットで確かめる
    # 引数を送る前に、秘密情報や個人情報（ガードレールで「マスク」にしている種類）がないかを確かめ、あれば送らない
    check_arguments: bool = False
    # 送る内容を調べる引数（"auto" の判定に使う）。None なら、"auto" のときだけ、すべての引数を調べる
    screen_args: tuple[str, ...] | None = None
    grounding: bool = False  # 返事の数の照合で、このツールの結果を根拠のデータとして扱う（ADR-0026）
    max_calls_per_run: int | None = None  # 1 回の依頼で呼べる回数（サブエージェントの分も含む）

    def screened_args(self) -> tuple[str, ...] | None:
        """送る内容を調べる引数。調べないなら None、すべての引数なら空。"""
        if self.screen_args is not None:
            return self.screen_args
        return () if self.require_approval == "auto" else None


@dataclass(frozen=True)
class AgentTool:
    """ツールと、その扱い。"""

    tool: BaseTool
    policy: ToolPolicy

    @property
    def name(self) -> str:
        return self.tool.name


def agent_tool(
    *,
    label: str,
    require_approval: Approval = False,
    check_arguments: bool = True,
    grounding: bool = False,
    max_calls_per_run: int | None = None,
    strict: bool = True,
) -> Callable[[Callable[..., Any]], AgentTool]:
    """関数を、エージェントのツールにする（用途パックの python_tools のファイルで使う）。

    関数の名前がツールの名前、docstring がモデルへの説明になる（Google の書き方の Args: で、引数の説明も書ける）。
    引数には型を書く（モデルが渡す値の形になる）。戻り値は文字列にする。

        @agent_tool(label="税込みの金額", grounding=True)
        def price_with_tax(amount: int) -> str:
            \"\"\"税抜きの金額から、消費税（10%）と税込みの金額を出す。

            Args:
                amount: 税抜きの金額（円）
            \"\"\"

    関数の中でエラーが起きたときは、エラーの種類だけをモデルに返す（エラーの文には、値や接続先などが入るため）。
    モデルに理由を伝えたいときは、ToolException("理由") を投げる。
    """
    if max_calls_per_run is not None and max_calls_per_run < 1:
        raise ValueError("max_calls_per_run は 1 以上にしてください")

    def wrap(func: Callable[..., Any]) -> AgentTool:
        name = func.__name__
        _check_name(name, f"{func.__module__}.{name}")
        if not inspect.getdoc(func):
            raise ValueError(f"{name}: docstring（モデルへの説明）を書いてください")
        tool = StructuredTool.from_function(name=name, parse_docstring=True, handle_tool_error=True, **_guarded(func, label))
        policy = ToolPolicy(
            label=label,
            require_approval=require_approval,
            strict=strict,
            check_arguments=check_arguments,
            grounding=grounding,
            max_calls_per_run=max_calls_per_run,
        )
        return AgentTool(tool, policy)

    return wrap


def _guarded(func: Callable[..., Any], label: str) -> dict[str, Any]:
    """関数の中のエラーを、種類だけを書いた ToolException に替える（止まらずに、モデルに伝えて続ける）。

    StructuredTool に渡す形で返す。非同期の関数は coroutine、ふつうの関数は func。
    """
    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def run_async(*args: Any, **kwargs: Any) -> Any:
            try:
                return await func(*args, **kwargs)
            except ToolException:
                raise
            except Exception as exc:
                raise ToolException(_failure(label, exc)) from exc

        return {"coroutine": run_async}

    @functools.wraps(func)
    def run_sync(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except ToolException:
            raise
        except Exception as exc:
            raise ToolException(_failure(label, exc)) from exc

    return {"func": run_sync}


def _failure(label: str, exc: Exception) -> str:
    return f"{label}でエラーが起きました（{type(exc).__name__}）。"


def _check_name(name: str, where: str) -> None:
    if not _NAME.fullmatch(name):
        raise ValueError(f"ツールの名前は、英字で始まる、英数字・_・- の 64 文字以内にしてください: {name}（{where}）")
    if name in RESERVED:
        raise ValueError(f"ツールの名前 {name} は、元からあるツールと同じです。別の名前にしてください（{where}）")


# ---------- 組み込みのツール ----------
def builtin_policies(enabled: list[str], security: SecurityConfig) -> dict[str, ToolPolicy]:
    """組み込みのツールの扱い。設定（config/security.yaml）から作る。"""
    policies: dict[str, ToolPolicy] = {}
    if "run_python" in enabled:
        policies[code_execution.TOOL_NAME] = ToolPolicy("コードの実行", require_approval=security.code_execution.require_approval)
    if "database" in enabled:
        policies[database.TABLES_TOOL] = ToolPolicy("データベースの表の一覧", strict=False)
        policies[database.QUERY_TOOL] = ToolPolicy(
            "データベースの問い合わせ", require_approval=security.database.require_approval
        )
    if "web_search" in enabled:
        # 質問文の秘密情報は、web_search.py の中で確かめて断る（check_arguments は使わない）
        policies[web_search.TOOL_NAME] = ToolPolicy(
            "Web 検索",
            require_approval=security.web_search.require_approval,
            screen_args=("question",),  # 確認のしかたにかかわらず、判定を記録する（ADR-0022）
            max_calls_per_run=security.web_search.max_calls_per_run,
        )
    return policies


# ---------- 用途パックの Python のツール ----------
_loaded_python: dict[str, tuple[AgentTool, ...]] = {}


def python_tools(pack: Pack) -> tuple[AgentTool, ...]:
    """パックの python_tools のファイルを読み込み、@agent_tool を付けた関数を集める。"""
    if pack.name in _loaded_python:
        return _loaded_python[pack.name]
    found: list[AgentTool] = []
    for relative in pack.config.python_tools:
        path = pack.path(relative)
        module_name = f"data_agent_packs.{pack.name}.{path.stem}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"packs/{pack.name}/{relative} を読み込めません")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        tools = [value for value in vars(module).values() if isinstance(value, AgentTool)]
        if not tools:
            raise RuntimeError(f"packs/{pack.name}/{relative} に、@agent_tool を付けた関数がありません")
        found += tools
    _loaded_python[pack.name] = tuple(found)
    return _loaded_python[pack.name]


# ---------- まとめ ----------
def policies(pack: Pack | None = None, security: SecurityConfig | None = None) -> dict[str, ToolPolicy]:
    """パックで使う、すべてのツールの扱い（名前 → 扱い）。名前が重なっていれば RuntimeError。

    MCP のツールの扱いは、設定（pack.yaml）だけから作る（サーバーにはつながない）。
    """
    from data_agent.tools import mcp  # MCP のライブラリは、使うときだけ読み込む

    pack = pack or get_pack()
    security = security or get_security()
    result = builtin_policies(pack.config.builtin_tools, security)

    def add(name: str, policy: ToolPolicy, where: str) -> None:
        try:
            _check_name(name, where)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        if name in result:
            raise RuntimeError(f"ツールの名前 {name} が重なっています（{where}）")
        result[name] = policy

    for item in python_tools(pack):
        add(item.name, item.policy, f"packs/{pack.name} の Python のツール")
    for name, policy in mcp.policies(pack).items():
        add(name, policy, f"packs/{pack.name}/pack.yaml の mcp_servers")
    return result


@functools.lru_cache
def current() -> dict[str, ToolPolicy]:
    """いまの用途パック（.env の AGENT_PACK）の、すべてのツールの扱い。"""
    return policies(get_pack(), get_security())


def labels(tool_policies: Mapping[str, ToolPolicy]) -> dict[str, str]:
    return {name: policy.label for name, policy in tool_policies.items()}


def grounding_labels(tool_policies: Mapping[str, ToolPolicy]) -> dict[str, str]:
    """返事の照合の注記で「根拠に使ったもの」に並べる、パックのツールの名前。"""
    return {name: policy.label for name, policy in tool_policies.items() if policy.grounding}


def grounding_config(security: SecurityConfig, tool_policies: Mapping[str, ToolPolicy]) -> GroundingConfig:
    """返事の照合の設定。設定ファイルのツールに、扱いで grounding としたツールを足す。"""
    config = security.grounding
    extra = [name for name, policy in tool_policies.items() if policy.grounding and name not in config.tools]
    return config.model_copy(update={"tools": [*config.tools, *extra]}) if extra else config


# ---------- 手で確かめる（python -m data_agent.tools.registry） ----------
def _yes(value: object) -> str:
    return {True: "毎回", False: "しない", "auto": "怪しいとき"}.get(value, str(value))  # type: ignore[call-overload]


def _main(argv: list[str] | None = None) -> int:
    """用途パックの中身（使うツールと、その扱い）を出す。MCP サーバーにはつながない。"""
    import argparse

    from data_agent.settings import get_settings, load_pack

    parser = argparse.ArgumentParser(
        prog="python -m data_agent.tools.registry",
        description="用途パックの中身（使うツールと、その扱い）を出す。サーバーにはつながず、何も実行しない",
    )
    parser.add_argument("--pack", default=None, help="用途パックの名前（既定は .env の AGENT_PACK）")
    args = parser.parse_args(argv)
    try:
        pack = load_pack(args.pack or get_settings().agent_pack)
        security = get_security()
        tool_policies = policies(pack, security)
    except (RuntimeError, ValueError) as exc:
        print(exc)
        return 1
    print(f"用途パック: {pack.name}（{pack.config.title}）")
    print(f"役割の文: packs/{pack.name}/{pack.config.prompt}（{len(pack.system_prompt()):,} 文字）")
    print(f"あいさつ: {pack.config.greeting}")
    print("ツール（名前 / 画面の名前 / 確認 / 毎回確認のチャット / 送る前の確かめ / 回数の上限 / 照合）:")
    grounded = set(grounding_config(security, tool_policies).tools)  # 設定ファイルの grounding.tools も含める
    for name, policy in tool_policies.items():
        limit = policy.max_calls_per_run or "-"
        print(
            f"  {name} / {policy.label} / 確認: {_yes(policy.require_approval)} / "
            f"{'確かめる' if policy.strict else '確かめない'} / {'する' if policy.check_arguments else 'しない'} / "
            f"{limit} / {'根拠にする' if name in grounded else '-'}"
        )
    print("（ほかに、ファイルの読み書きなど、Deep Agents が元から持つツールがある）")
    return 0


if __name__ == "__main__":
    # python -m で動かすと、このファイルは __main__ として、もう一度読み込まれる。パックの tools.py が使うのは
    # data_agent.tools.registry のほうなので、そちらの _main を使う（AgentTool を同じものとして見分けるため）
    from data_agent.tools.registry import _main as main

    raise SystemExit(main())
