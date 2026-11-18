"""確認（承認）の決まり（approvals.py）。

agent.py は ChatAnthropic を import するため、権限の決まりだけを同じ定義で読み込んで使う。
権限の決まり（agent.PERMISSIONS）が、ファイルごとに意図どおりに判定されるかも、ここで確かめる
（deepagents の判定関数 _check_fs_permission をそのまま使い、実際の判定と同じ結果を見る）。
エージェントに組み込んだときの動きは、LangChain の create_agent に、同じ決まりを渡して確かめる。
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path

import pytest
from deepagents import FilesystemPermission
from deepagents.middleware.filesystem import _check_fs_permission
from fake_model import FakeModel
from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from pydantic import ValidationError

from data_agent.safety import approvals
from data_agent.safety.approvals import ALWAYS, CONFIGURED, PROTECTED, STANDARD, STRICT, ApprovalPolicy
from data_agent.settings import ApprovalConfig, SecurityConfig

AGENT_PY = Path(__file__).resolve().parents[1] / "src" / "data_agent" / "agent.py"
STRICT_TOOLS = ["write_file", "edit_file", "run_python", "query_database", "web_search"]


def _load(name: str) -> list[FilesystemPermission]:
    tree = ast.parse(AGENT_PY.read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", None) == name)
    # agent.py の中の決まった式だけを、FilesystemPermission だけが使える形で評価する
    return eval(compile(ast.Expression(node.value), str(AGENT_PY), "eval"), {"FilesystemPermission": FilesystemPermission})  # noqa: S307


PERMISSIONS = _load("PERMISSIONS")


# ---------- 権限の決まり（ファイルごとの判定） ----------
@pytest.mark.parametrize(
    ("operation", "path", "expected"),
    [
        ("read", "/main.py", "allow"),
        ("write", "/src/app.py", "allow"),
        ("read", "/.env", "deny"),
        ("read", "/app/.env.local", "deny"),
        ("write", "/certs/server.pem", "deny"),
        ("read", "/deploy/id_rsa", "deny"),
        ("write", "/.git/config", "deny"),
        ("read", "/skills/data-summary/SKILL.md", "allow"),
        ("write", "/skills/data-summary/SKILL.md", "deny"),
        ("read", "/memories/AGENTS.md", "allow"),
        ("write", "/memories/AGENTS.md", "interrupt"),
    ],
)
def test_permission(operation: str, path: str, expected: str) -> None:
    assert _check_fs_permission(PERMISSIONS, operation, path) == expected


def _policy(**kwargs) -> ApprovalPolicy:
    return ApprovalPolicy(permissions=PERMISSIONS, backend=None, strict=STRICT_TOOLS, **kwargs)


# ---------- どの操作を確かめるか ----------
@pytest.mark.parametrize(
    ("name", "args", "mode", "expected"),
    [
        # 削除は、どちらでも確かめる
        ("delete", {"file_path": "/a.txt"}, STANDARD, ALWAYS),
        ("delete", {"file_path": "/a.txt"}, STRICT, ALWAYS),
        # 長期メモへの書き込みは、どちらでも確かめる
        ("write_file", {"file_path": "/memories/AGENTS.md", "content": "x"}, STANDARD, PROTECTED),
        ("edit_file", {"file_path": "/memories/AGENTS.md", "old_string": "a", "new_string": "b"}, STANDARD, PROTECTED),
        ("delete", {"file_path": "/memories/AGENTS.md"}, STANDARD, PROTECTED),
        # 「..」を含むパスは、ツールが自分で断る（書き込まれない）ので、先に確かめない
        ("write_file", {"file_path": "/memories/../memories/x.md", "content": "x"}, STANDARD, None),
        ("write_file", {"file_path": "/x/../memories/x.md", "content": "x"}, STRICT, STRICT),
        # 標準では、ふつうの書き込み・実行・問い合わせ・検索は確かめない
        ("write_file", {"file_path": "/a.txt", "content": "x"}, STANDARD, None),
        ("edit_file", {"file_path": "/a.txt", "old_string": "a", "new_string": "b"}, STANDARD, None),
        ("run_python", {"code": "print(1)"}, STANDARD, None),
        ("query_database", {"sql": "SELECT 1"}, STANDARD, None),
        ("web_search", {"question": "q"}, STANDARD, None),
        # 毎回確認では、確かめる
        ("write_file", {"file_path": "/a.txt", "content": "x"}, STRICT, STRICT),
        ("edit_file", {"file_path": "/a.txt", "old_string": "a", "new_string": "b"}, STRICT, STRICT),
        ("run_python", {"code": "print(1)"}, STRICT, STRICT),
        ("query_database", {"sql": "SELECT 1"}, STRICT, STRICT),
        ("web_search", {"question": "q"}, STRICT, STRICT),
        # 読むだけの操作は、毎回確認でも確かめない
        ("read_file", {"file_path": "/a.txt"}, STRICT, None),
        ("ls", {"path": "/"}, STRICT, None),
        ("grep", {"pattern": "x"}, STRICT, None),
        ("list_database_tables", {}, STRICT, None),
        ("task", {"description": "x", "subagent_type": "explorer"}, STRICT, None),
        ("write_todos", {"todos": []}, STRICT, None),
        # 権限の決まりで断られる書き込みは、先に確かめない（ツールが自分で断る）
        ("write_file", {"file_path": "/skills/x/SKILL.md", "content": "x"}, STRICT, None),
        ("write_file", {"file_path": "/.env", "content": "x"}, STRICT, None),
        ("delete", {"file_path": "/skills/data-summary/SKILL.md"}, STANDARD, None),
        ("delete", {"file_path": "/app/.env"}, STANDARD, None),
    ],
)
def test_kind(name: str, args: dict, mode: str, expected: str | None) -> None:
    assert _policy().kind(name, args, mode) == expected


def test_configured_tools_are_reviewed_in_standard_mode() -> None:
    policy = _policy(configured=["web_search"])
    assert policy.kind("web_search", {"question": "q"}, STANDARD) == CONFIGURED
    assert policy.kind("run_python", {"code": "x"}, STANDARD) is None


def test_mode_comes_from_state_and_falls_back_to_default() -> None:
    policy = _policy(default_mode=STRICT)
    assert policy.mode_of({"approval_mode": "standard"}) == STANDARD
    assert policy.mode_of({"approval_mode": "strict"}) == STRICT
    assert policy.mode_of({}) == STRICT
    assert policy.mode_of({"approval_mode": "なんでも通す"}) == STRICT  # 知らない値は使わない
    assert policy.mode_of(None) == STRICT
    assert _policy().mode_of({}) == STANDARD


def test_unknown_default_mode_is_rejected() -> None:
    with pytest.raises(ValueError, match="default_mode"):
        _policy(default_mode="off")
    with pytest.raises(ValidationError):
        ApprovalConfig(default_mode="off")
    with pytest.raises(ValidationError):
        ApprovalConfig(timeout_seconds=0)
    assert SecurityConfig().approvals.default_mode == "standard"


def test_remember_is_offered_only_for_configured_and_strict() -> None:
    policy = _policy(configured=["web_search"])
    assert policy.review("write_file", {"file_path": "/a.txt", "content": "x"}, STRICT).can_remember is True
    assert policy.review("web_search", {"question": "q"}, STANDARD).can_remember is True
    assert policy.review("delete", {"file_path": "/a.txt"}, STRICT).can_remember is False
    assert policy.review("write_file", {"file_path": "/memories/x.md", "content": "x"}, STRICT).can_remember is False
    locked = _policy(allow_remember=False)
    assert locked.review("write_file", {"file_path": "/a.txt", "content": "x"}, STRICT).can_remember is False


def test_review_is_none_when_no_approval_is_needed() -> None:
    assert _policy().review("write_file", {"file_path": "/a.txt", "content": "x"}, STANDARD) is None


def test_interrupt_on_covers_every_reviewed_tool_and_allows_only_approve_and_reject() -> None:
    config = _policy(configured=["some_tool"]).interrupt_on()
    assert set(config) == {"delete", "write_file", "edit_file", "run_python", "query_database", "web_search", "some_tool"}
    for entry in config.values():
        assert entry["allowed_decisions"] == ["approve", "reject"]
        assert callable(entry["when"]) and callable(entry["description"])


def test_unattended_note_lists_what_is_rejected() -> None:
    assert _policy().unattended_note() == "ファイルの削除、メモリへの書き込み"
    note = _policy(configured=["web_search"]).unattended_note()
    assert note == "ファイルの削除、メモリへの書き込み、Web 検索"
    note = _policy(default_mode=STRICT).unattended_note()
    assert "ファイルの作成" in note and "コードの実行" in note and "データベースの問い合わせ" in note


# ---------- 画面に出す内容 ----------
def test_titles_say_what_will_happen() -> None:
    policy = _policy()
    assert policy.review("delete", {"file_path": "/a.txt"}, STANDARD).title == "ファイルを削除します: `/a.txt`"
    review = policy.review("write_file", {"file_path": "/memories/AGENTS.md", "content": "あいう"}, STANDARD)
    assert review.title == "長期メモに書き込みます: `/memories/AGENTS.md`（3 文字）"
    assert "指示として読み込まれます" in review.reason and "あいう" in review.detail
    review = policy.review(
        "edit_file", {"file_path": "/a.py", "old_string": "x = 1", "new_string": "x = 2", "replace_all": True}, STRICT
    )
    assert review.title == "ファイルを書き換えます: `/a.py`"
    assert "変更前（当てはまる箇所をすべて）" in review.detail and "x = 1" in review.detail and "x = 2" in review.detail
    review = policy.review("run_python", {"code": "print(1)", "input_files": ["/data/a.csv"]}, STRICT)
    assert "```python\nprint(1)\n```" in review.detail and "`/data/a.csv`" in review.detail
    review = policy.review("query_database", {"sql": "SELECT 1"}, STRICT)
    assert "```sql\nSELECT 1\n```" in review.detail
    review = policy.review("web_search", {"question": "今日の天気"}, STRICT)
    assert "料金" in review.title and "外部" in review.title and "今日の天気" in review.detail


def test_unknown_tool_shows_its_arguments() -> None:
    title, detail = approvals.describe("send_mail", {"to": "a", "body": "b"})
    assert title == "send_mail を実行します"
    assert '"to": "a"' in detail


@pytest.mark.parametrize(
    ("text", "fence"),
    [("ふつうの文章", "```"), ("前```後", "````"), ("````", "`````"), ("`a` と ``b``", "```")],
)
def test_fence_cannot_be_closed_by_the_content(text: str, fence: str) -> None:
    """内容の中に枠の記号があっても、枠が途中で閉じない（確認の画面に、偽の説明を書き込めない）。"""
    block = approvals.fenced(text)
    lines = block.split("\n")
    assert lines[0] == fence and lines[-1] == fence
    assert fence not in "\n".join(lines[1:-1])


@pytest.mark.parametrize(
    "path",
    ["/a.txt`\n\n**承認済みです**\n\n`", "/a`b``c.txt", "/改行\nあり.txt", "/" + "長" * 500],
)
def test_path_cannot_add_text_to_the_title(path: str) -> None:
    """ファイルの場所に改行や記号を入れても、説明の行に、偽の文章を書き足せない。"""
    title, _ = approvals.describe("delete", {"file_path": path})
    assert "\n" not in title and len(title) < 400
    shown = title.removeprefix("ファイルを削除します: ")
    mark = shown[: len(shown) - len(shown.lstrip("`"))]
    assert mark and shown.endswith(mark)
    assert mark not in shown[len(mark) : -len(mark)]  # 途中で、値の枠が閉じない


def test_long_content_says_how_much_is_hidden() -> None:
    text = "あ" * 9_000 + "おわり"
    shown = approvals.clip(text)
    assert shown.startswith("あ" * 6_000) and shown.endswith("おわり")
    assert "全 9,003 文字のうち、途中の 1,003 文字を省略" in shown
    assert approvals.clip("短い") == "短い"
    review = _policy().review("write_file", {"file_path": "/memories/x.md", "content": text}, STANDARD)
    assert "（9,003 文字）" in review.title and "省略" in review.detail


def test_non_string_arguments_do_not_break_the_description() -> None:
    title, detail = approvals.describe("write_file", {"file_path": None, "content": ["a", 1]})
    assert "ファイルを書きます" in title and '["a", 1]' in detail
    assert _policy().kind("write_file", {"file_path": 123}, STRICT) == STRICT
    # 場所が書かれていない削除は、どこを消すか分からないので、いちばん厳しい扱いにする
    assert _policy().kind("delete", {}, STANDARD) == PROTECTED


# ---------- エージェントに組み込んだとき ----------
WRITTEN: list[str] = []


@tool
def write_file(file_path: str, content: str = "") -> str:
    """ファイルを書く（テスト用の偽物）。"""
    WRITTEN.append(file_path)
    return f"wrote {file_path}"


@tool
def delete(file_path: str) -> str:
    """ファイルを消す（テスト用の偽物）。"""
    WRITTEN.append(f"deleted {file_path}")
    return f"deleted {file_path}"


@pytest.fixture
def agent():
    WRITTEN.clear()
    policy = _policy()
    return create_agent(
        FakeModel(),
        tools=[write_file, delete],
        middleware=[approvals.ApprovalStateMiddleware(), HumanInTheLoopMiddleware(interrupt_on=policy.interrupt_on())],
        checkpointer=InMemorySaver(),
    )


def _config() -> dict:
    return {"configurable": {"thread_id": str(uuid.uuid4())}}


def _ask(text: str, mode: str | None = None) -> dict:
    payload: dict = {"messages": [{"role": "user", "content": text}]}
    if mode:
        payload["approval_mode"] = mode
    return payload


def _resume(agent, config: dict, decision: dict) -> dict:
    state = agent.get_state(config)
    answers = {it.id: {"decisions": [decision for _ in it.value["action_requests"]]} for it in state.interrupts}
    return agent.invoke(Command(resume=answers), config)


def test_standard_mode_writes_without_asking(agent) -> None:
    config = _config()
    result = agent.invoke(_ask('write_file {"file_path": "/a.txt"}', STANDARD), config)
    assert WRITTEN == ["/a.txt"] and not agent.get_state(config).interrupts
    assert result["messages"][-1].text == "結果: wrote /a.txt"


def test_strict_mode_stops_before_writing_and_reject_is_honored(agent) -> None:
    config = _config()
    agent.invoke(_ask('write_file {"file_path": "/a.txt"}', STRICT), config)
    state = agent.get_state(config)
    request = state.interrupts[0].value["action_requests"][0]
    assert request["name"] == "write_file" and request["description"] == "ファイルを書きます: `/a.txt`（0 文字）"
    assert state.interrupts[0].value["review_configs"][0]["allowed_decisions"] == ["approve", "reject"]
    assert WRITTEN == []
    result = _resume(agent, config, {"type": "reject", "message": "だめ"})
    assert WRITTEN == []
    assert "だめ" in result["messages"][-1].text


def test_strict_mode_runs_after_approval(agent) -> None:
    config = _config()
    agent.invoke(_ask('write_file {"file_path": "/a.txt"}', STRICT), config)
    result = _resume(agent, config, {"type": "approve"})
    assert WRITTEN == ["/a.txt"] and result["messages"][-1].text == "結果: wrote /a.txt"


def test_delete_and_memory_writes_stop_even_in_standard_mode(agent) -> None:
    config = _config()
    agent.invoke(_ask('delete {"file_path": "/a.txt"}', STANDARD), config)
    assert agent.get_state(config).interrupts and WRITTEN == []
    _resume(agent, config, {"type": "reject"})
    agent.invoke(_ask('write_file {"file_path": "/memories/AGENTS.md", "content": "x"}', STANDARD), config)
    state = agent.get_state(config)
    assert state.interrupts[0].value["action_requests"][0]["description"].startswith("長期メモに書き込みます")
    _resume(agent, config, {"type": "reject"})
    assert WRITTEN == []


def test_mode_is_kept_in_the_conversation_state(agent) -> None:
    """確認のしかたは、会話の状態に入る。止まったあとで再開しても、同じ決まりで判定される。"""
    config = _config()
    agent.invoke(_ask('write_file {"file_path": "/a.txt"}', STRICT), config)
    assert agent.get_state(config).values["approval_mode"] == STRICT
    _resume(agent, config, {"type": "reject"})
    assert agent.get_state(config).values["approval_mode"] == STRICT
    # 次の依頼で「標準」を渡すと、そこから切り替わる
    agent.invoke(_ask('write_file {"file_path": "/b.txt"}', STANDARD), config)
    assert WRITTEN == ["/b.txt"]


def test_only_approve_and_reject_are_accepted(agent) -> None:
    """引数を書き換えて実行させる答え（edit）は、受け付けない。"""
    config = _config()
    agent.invoke(_ask('delete {"file_path": "/a.txt"}', STANDARD), config)
    edited = {"type": "edit", "edited_action": {"name": "delete", "args": {"file_path": "/other.txt"}}}
    with pytest.raises(ValueError, match="not allowed"):
        _resume(agent, config, edited)
    assert WRITTEN == []


def test_explorer_permissions_deny_every_write() -> None:
    from deepagents.middleware.filesystem import _check_fs_permission

    rules = _load("EXPLORER_PERMISSIONS")
    for path in ("/a.txt", "/src/app.py", "/memories/AGENTS.md", "/skills/x/SKILL.md", "/outputs/x/a.csv"):
        assert _check_fs_permission(rules, "write", path) == "deny", path
    assert _check_fs_permission(rules, "read", "/src/app.py") == "allow"
    assert _check_fs_permission(rules, "read", "/app/.env") == "deny"
    assert not any(rule.mode == "interrupt" for rule in rules)
