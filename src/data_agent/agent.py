"""エージェント本体の組み立て。

01_DeepAgents利用法とClaudeCode風エージェントの作り方.md §4 のレシピがベース。

仮想ファイルシステムの見え方:
  /            → workspace/（エージェントが触れる唯一の作業フォルダ）
  /memories/   → memories/（AGENTS.md。書き込みは人の承認制）
  /skills/     → skills/（手順書。読み取り専用）
config/ や .env はどこにもマウントしないので、ファイルツールからは見えない。

役割の文（システムプロンプト）と、使うツールは、用途パック（packs/<名前>/pack.yaml。.env の AGENT_PACK）で決まる
（ADR-0032）。ツールは次の 3 種類で、確認・回数の上限・照合などの扱いは、tools/registry.py の ToolPolicy から決まる。
- 組み込み: コードの実行（run_python。SANDBOX_URL を設定したときだけ付く。Deep Agents 標準の execute は使わない）、
  データベースの読み取り（database.py。DATA_DB_HOST を設定したときだけ付く）、Web 検索（web_search.py）
- パックの Python のツール（@agent_tool を付けた関数）
- パックの MCP サーバーのツール（tools/mcp.py。アプリの起動のときに mcp.load で読み込んでおく。ADR-0033）

どのエージェント（親とサブエージェント）にも、次の4つを付ける。サブエージェントは親のミドルウェアを引き継がない。
- ガードレール（guard_middleware.py）
- Web 検索の質問文の判定（search_screen.py。外に出したくない情報らしいものがあれば、検索の前に確かめる）
- 確認のしかたを会話の状態で受け取るためのもの（approvals.py）
- 回数の上限（limits.py。親とサブエージェントで、同じ台帳に数える）

確認した API（deepagents 0.7.32 / langchain 1.4.7 / langchain-anthropic 1.7.6）:
  create_deep_agent(model, tools, *, system_prompt, middleware, subagents, skills, memory,
                    permissions, backend, interrupt_on, checkpointer, ...)
  0.7 から write_todos（TodoListMiddleware）は opt-in、デフォルトのシステムプロンプトは空。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from deepagents import FilesystemPermission, create_deep_agent
from deepagents.backends import CompositeBackend, FilesystemBackend
from deepagents.middleware.subagents import GENERAL_PURPOSE_SUBAGENT
from langchain.agents.middleware import TodoListMiddleware
from langchain_anthropic import ChatAnthropic
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from data_agent.safety import approvals, search_screen, watch_terms
from data_agent.safety.guard_middleware import GuardrailMiddleware
from data_agent.safety.limits import RequestLimitMiddleware, RequestLimits
from data_agent.settings import PROJECT_ROOT, get_guard, get_pack, get_security, get_settings
from data_agent.tools import code_execution, database, mcp, registry, web_search
from data_agent.tools.registry import ToolPolicy

# サーバー側フォールバック（安全分類器に断られたら別モデルで続ける）に対応するモデル。
# Haiku 5.5 には、サーバー側のフォールバックがない
_FALLBACK_MODELS = {
    "claude-fable-5-2",
    "claude-fable-5-1",
    "claude-opus-5-5",
    "claude-opus-5",
    "claude-sonnet-5-5",
}


def build_model(model: str, effort: str) -> ChatAnthropic:
    """ChatAnthropic を組み立てる。temperature は送らない（現行モデルでは 400 になる）。"""
    settings = get_settings()
    extra: dict = {}
    if settings.enable_refusal_fallback and model in _FALLBACK_MODELS:
        extra = {"betas": ["server-side-fallback-2026-07-01"], "model_kwargs": {"fallbacks": "default"}}
    return ChatAnthropic(
        model=model,
        max_tokens=64_000,
        output_config={"effort": effort},
        streaming=True,
        **extra,
    )


def backend_for(workspace: Path, memories: Path | None = None, skills: Path | None = None) -> CompositeBackend:
    """エージェントから見えるファイルの置き場所を作る。memories と skills は、省略するとプロジェクトのもの。"""
    # virtual_mode=True で、root_dir の外（../ や絶対パス）に出られないようにする
    return CompositeBackend(
        default=FilesystemBackend(root_dir=workspace, virtual_mode=True),
        routes={
            "/memories/": FilesystemBackend(root_dir=memories or PROJECT_ROOT / "memories", virtual_mode=True),
            "/skills/": FilesystemBackend(root_dir=skills or PROJECT_ROOT / "skills", virtual_mode=True),
        },
    )


# 書いた順に評価され、最初に一致したものが使われる（どれにも一致しなければ許可）
PERMISSIONS = [
    # 秘密情報になりがちなファイルは読み書きとも禁止
    FilesystemPermission(
        operations=["read", "write"],
        paths=["/**/.env", "/**/.env.*", "/**/*.pem", "/**/*.key", "/**/id_rsa*", "/**/.git/**"],
        mode="deny",
    ),
    FilesystemPermission(operations=["write"], paths=["/skills/**"], mode="deny"),
    FilesystemPermission(operations=["write"], paths=["/memories/**"], mode="interrupt"),
]

# 調べるだけのサブエージェント（explorer）用。秘密情報になりがちなファイルは読めず、どこにも書き込めない。
# 「ファイルは変更しない」とプロンプトに書くだけでは、書き込みも削除もできてしまう（2026-11-14 に確かめた）
EXPLORER_PERMISSIONS = [
    FilesystemPermission(
        operations=["read", "write"],
        paths=["/**/.env", "/**/.env.*", "/**/*.pem", "/**/*.key", "/**/id_rsa*", "/**/.git/**"],
        mode="deny",
    ),
    FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
]


def tool_policies() -> dict[str, ToolPolicy]:
    """用途パックで使う、すべてのツールの扱い（tools/registry.py）。確認・回数の上限・照合は、ここから決まる。"""
    return registry.current()


@lru_cache
def approval_policy() -> approvals.ApprovalPolicy:
    """どの操作を、実行する前に利用者に確かめるか。画面（ui/app.py）と定時実行でも、同じものを使う。"""
    settings, security, policies = get_settings(), get_security(), tool_policies()
    return approvals.ApprovalPolicy(
        permissions=PERMISSIONS,
        backend=backend_for(settings.workspace_dir),
        configured=[name for name, policy in policies.items() if policy.require_approval is True],
        # auto: 送る内容を調べて、外に出したくない情報らしいものがあるときだけ確かめる（search_screen.py）
        screened=[name for name, policy in policies.items() if policy.require_approval == "auto"],
        strict=[*approvals.STRICT_FILE_TOOLS, *(name for name, policy in policies.items() if policy.strict)],
        labels=registry.labels(policies),
        default_mode=security.approvals.default_mode,
        allow_remember=security.approvals.allow_remember,
    )


@lru_cache
def request_limits() -> RequestLimits:
    """1回の依頼で使える回数の上限。モデルを切り替えても、同じ台帳に数える。"""
    security = get_security()
    return RequestLimits(
        max_model_calls=security.limits.max_model_calls_per_run,
        max_tool_calls=security.limits.max_tool_calls_per_run,
        # ツールごとの上限（Web 検索は、1回ごとに料金がかかるので決めてある）
        per_tool={name: policy.max_calls_per_run for name, policy in tool_policies().items() if policy.max_calls_per_run},
    )


def screened_arguments() -> dict[str, tuple[str, ...]]:
    """送る内容を調べるツールと、調べる引数（空なら、すべての引数）。"""
    return {name: args for name, policy in tool_policies().items() if (args := policy.screened_args()) is not None}


def checked_arguments() -> frozenset[str]:
    """送る前に、引数の秘密情報や個人情報を確かめるツール。"""
    return frozenset(name for name, policy in tool_policies().items() if policy.check_arguments)


def pack_tools(backend: CompositeBackend) -> list[BaseTool]:
    """用途パックで使うツールを作る。MCP のツールは、先に mcp.load で読み込んでおく。"""
    pack = get_pack()
    enabled = pack.config.builtin_tools
    tools: list[BaseTool] = []
    # コードの実行
    if "run_python" in enabled and (run_python := code_execution.build_tool(backend, PERMISSIONS)) is not None:
        tools.append(run_python)
    # データベースの読み取り
    if "database" in enabled:
        tools += database.build_tools(backend, PERMISSIONS)
    # Web 検索
    if "web_search" in enabled and (search := web_search.build_tool(build_model)) is not None:
        tools.append(search)
    tools += [item.tool for item in registry.python_tools(pack)]
    tools += [item.tool for item in mcp.loaded_tools(pack)]
    return tools


def build_agent(
    checkpointer: BaseCheckpointSaver | None = None,
    main_model: str | None = None,
    *,
    backend: CompositeBackend | None = None,
    tools: list[BaseTool] | None = None,
):
    """エージェントを組み立てる。

    checkpointer は会話の保存先（persistence.open_checkpointer で用意する）。
    省略するとメモリ上に保存し、再起動で消える。
    main_model はメインのエージェントが使うモデル。省略すると MAIN_MODEL。
    会話は保存先にあるので、同じ会話を別のモデルのエージェントで続けられる。

    backend と tools は、点検（redteam）のためのもの。使い捨てのフォルダと、外へ送らないツールに差し替えて、
    ほかの部分（プロンプト、権限、確認、ガードレール、回数の上限）は、ふだんと同じものを使う。
    省略すると、ふだんの作業フォルダと、ふだんのツールになる。
    """
    settings = get_settings()
    if backend is None:
        settings.workspace_dir.mkdir(parents=True, exist_ok=True)
        backend = backend_for(settings.workspace_dir)

    # どのエージェントにも付けるもの。サブエージェントは親のミドルウェアを引き継がないので、1つずつ付ける
    guard = GuardrailMiddleware(get_guard(), check_arguments=checked_arguments())
    approval_state = approvals.ApprovalStateMiddleware()
    limits = request_limits()
    screened = screened_arguments()
    subagent_middleware = [
        guard,
        search_screen.SearchScreenMiddleware(watch_terms.from_settings(), root=False, screened=screened),
        approval_state,
        RequestLimitMiddleware(limits, root=False),
    ]

    explorer = {
        "name": "explorer",
        "description": "コードベースや資料を調べて要点だけ返す。ファイルは書き換えない。大量に読む調査はこれに任せる。",
        "system_prompt": "調査だけを行い、結論・根拠となるファイルのパス・不明点を500字以内で返す。ファイルは変更しない。",
        "model": build_model(settings.fast_model, "low"),
        "middleware": subagent_middleware,
        "tools": [],  # 手元の資料を調べるだけの役なので、独自のツールは持たせない（書かなければ、親のツールを引き継ぐ）
        "permissions": EXPLORER_PERMISSIONS,  # 書き込みは、権限の決まりで止める
        "interrupt_on": {},  # 書き込めないので、確かめる操作がない（書かなければ、親の決まりを引き継ぐ）
    }
    # 独自のツール。汎用のサブエージェントにも引き継がれる
    if tools is None:
        tools = pack_tools(backend)

    # Deep Agents が標準で足す汎用のサブエージェント。同じ名前で定義し直して、ガードレールなどを付ける
    # （定義しなければ、何も付かないまま自動的に足される）。権限と、確かめる操作の決まりは、親のものを引き継ぐ
    general_purpose = {**GENERAL_PURPOSE_SUBAGENT, "skills": ["/skills/"], "middleware": subagent_middleware}

    return create_deep_agent(
        model=build_model(main_model or settings.main_model, settings.main_effort),
        tools=tools,
        system_prompt=get_pack().system_prompt(),
        backend=backend,
        middleware=[
            guard,
            search_screen.SearchScreenMiddleware(watch_terms.from_settings(), root=True, screened=screened),
            approval_state,
            TodoListMiddleware(),  # 0.7 以降は明示的に有効化
            RequestLimitMiddleware(limits, root=True),
        ],
        subagents=[general_purpose, explorer],
        memory=["/memories/AGENTS.md"],
        skills=["/skills/"],
        permissions=PERMISSIONS,
        # 実行する前に利用者に確かめる操作（approvals.py）。/memories/ への書き込みの分も、この中に入っている
        interrupt_on=approval_policy().interrupt_on(),
        checkpointer=checkpointer if checkpointer is not None else InMemorySaver(),
        name="data-agent",
    )
