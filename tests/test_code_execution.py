"""コードの実行ツール（code_execution.py）の、ファイルの受け渡しと権限の確認。

実行する環境（sandbox/）は偽物に差し替え、作業フォルダは一時フォルダを使う。
権限の決まりは、agent.py と同じもの（test_approvals.py が読み込んだもの）を使う。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from deepagents.backends import CompositeBackend, FilesystemBackend
from test_approvals import PERMISSIONS as RULES

from data_agent.safety.guardrails import DEFAULT_ACTIONS, Guard
from data_agent.settings import CodeExecutionLimits
from data_agent.tools import code_execution
from data_agent.tools.code_execution import CodeRunner, SandboxError, SandboxResult

KEY = "sk-ant-api03-TEST0000TEST0000TEST0000"  # 形だけを合わせた偽物


class FakeClient:
    """実行する環境の偽物。受け取ったものを覚えておき、決めておいた結果を返す。"""

    def __init__(self, result: SandboxResult | Exception | None = None) -> None:
        self.calls: list[tuple[str, list[tuple[str, bytes]], int]] = []
        self.result = result or SandboxResult(exit_code=0, timed_out=False, output="ok\n", truncated=False, duration_ms=1200)

    async def run(self, code: str, files: list[tuple[str, bytes]], timeout: int) -> SandboxResult:
        self.calls.append((code, files, timeout))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _result(**fields: object) -> SandboxResult:
    base = {"exit_code": 0, "timed_out": False, "output": "", "truncated": False, "duration_ms": 500}
    return SandboxResult(**{**base, **fields})


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    (root / "data").mkdir(parents=True)
    (root / "data" / "sales.csv").write_text("地域,売上\n東京,120\n", encoding="utf-8")
    (root / ".env").write_text(f"API_KEY={KEY}\n", encoding="utf-8")
    (root / "link.txt").symlink_to(root / ".env")
    return root


def _runner(workspace: Path, client: FakeClient, **limits: object) -> CodeRunner:
    backend = CompositeBackend(
        default=FilesystemBackend(root_dir=workspace, virtual_mode=True),
        routes={"/skills/": FilesystemBackend(root_dir=workspace.parent / "skills", virtual_mode=True)},
    )
    return CodeRunner(client, backend, RULES, CodeExecutionLimits(**limits), Guard(DEFAULT_ACTIONS, enabled=True))


def _run(runner: CodeRunner, *args: object, **kwargs: object) -> tuple[str, dict]:
    return asyncio.run(runner.run(*args, **kwargs))


def test_input_files_are_sent_and_outputs_are_saved(workspace: Path) -> None:
    client = FakeClient(_result(output="245\n", files=[("chart.png", b"\x89PNG..."), ("sub/report.xlsx", b"PK..")]))
    text, artifact = _run(_runner(workspace, client), "print(245)", ["/data/sales.csv"])

    code, files, timeout = client.calls[0]
    assert code == "print(245)"
    assert files == [("sales.csv", "地域,売上\n東京,120\n".encode())]
    assert timeout == 60  # 既定の時間

    paths = [f["path"] for f in artifact["files"]]
    assert len(paths) == 2 and all(p.startswith("/outputs/") for p in paths)
    assert paths[0].endswith("/chart.png") and paths[1].endswith("/sub/report.xlsx")
    assert (workspace / paths[0].lstrip("/")).read_bytes() == b"\x89PNG..."
    assert "終了コード: 0" in text and "245" in text and paths[0] in text


@pytest.mark.parametrize("path", ["/.env", "/link.txt", "/certs/server.pem", "/.git/config"])
def test_denied_files_are_not_sent(workspace: Path, path: str) -> None:
    client = FakeClient()
    text, artifact = _run(_runner(workspace, client), "print(1)", [path])
    assert client.calls == []  # 実行する環境には、何も送らない
    assert "permission denied" in text and artifact["error"] is True
    assert KEY not in text


@pytest.mark.parametrize(
    ("paths", "message"),
    [
        (["/data/missing.csv"], "読めませんでした"),
        (["/../outside.txt"], "正しくありません"),
        (["/data/sales.csv", "/copy/sales.csv"], "同じファイル名"),
        (["/a", "/b", "/c"], "2 個まで"),
    ],
)
def test_bad_inputs_are_rejected(workspace: Path, paths: list[str], message: str) -> None:
    client = FakeClient()
    text, _ = _run(_runner(workspace, client, max_input_files=2), "print(1)", paths)
    assert client.calls == [] and message in text


def test_total_input_size_is_limited(workspace: Path) -> None:
    (workspace / "big.bin").write_bytes(b"0" * (2 * 1024 * 1024))
    client = FakeClient()
    text, _ = _run(_runner(workspace, client, max_input_mb=1), "print(1)", ["/big.bin"])
    assert client.calls == [] and "大きすぎます" in text


def test_unsafe_outputs_are_not_saved(workspace: Path) -> None:
    client = FakeClient(
        _result(
            files=[
                ("ok.txt", b"plain"),
                (".env", b"X=1"),  # 権限の決まりで書けない名前
                ("server.pem", b"cert"),
                ("leak.txt", f"KEY={KEY}".encode()),  # 秘密情報らしい文字列
                ("../escape.txt", b"x"),  # フォルダの外を指す名前
            ],
            skipped=[("big.bin", "too_large")],
        )
    )
    text, artifact = _run(_runner(workspace, client), "print(1)")
    saved = [f["path"] for f in artifact["files"]]
    assert len(saved) == 1 and saved[0].endswith("/ok.txt")
    written = sorted(p.name for p in (workspace / "outputs").rglob("*") if p.is_file())
    assert written == ["ok.txt"]
    assert not (workspace / "escape.txt").exists() and not (workspace / "outputs" / "escape.txt").exists()
    for name in (".env", "server.pem", "leak.txt", "../escape.txt", "big.bin"):
        assert name in text
    assert "秘密情報" in text and "大きすぎます" in text and KEY not in text


def test_timeout_is_clamped(workspace: Path) -> None:
    client = FakeClient()
    runner = _runner(workspace, client, timeout_seconds=30, max_timeout_seconds=120)
    _run(runner, "print(1)")
    _run(runner, "print(1)", timeout=9999)
    _run(runner, "print(1)", timeout=-5)
    assert [call[2] for call in client.calls] == [30, 120, 1]


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"timed_out": True, "exit_code": None}, "時間切れ"),
        ({"exit_code": -9}, "メモリ"),
        ({"exit_code": 1, "output": "ValueError: x"}, "終了コード: 1"),
    ],
)
def test_status_is_reported(workspace: Path, fields: dict, expected: str) -> None:
    text, artifact = _run(_runner(workspace, FakeClient(_result(**fields))), "print(1)")
    assert expected in text and artifact["files"] == []


def test_sandbox_failure_is_returned_as_text(workspace: Path) -> None:
    client = FakeClient(SandboxError("コードを実行する環境に接続できませんでした（ConnectError）。"))
    text, artifact = _run(_runner(workspace, client), "print(1)")
    assert "接続できませんでした" in text and artifact == {"error": True, "files": []}


def test_tool_is_built_only_when_configured(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backend = FilesystemBackend(root_dir=workspace, virtual_mode=True)
    settings = SimpleNamespace(sandbox_url=None, sandbox_token=None, schedule_timezone="Asia/Tokyo")
    monkeypatch.setattr(code_execution, "get_settings", lambda: settings)
    assert code_execution.build_tool(backend, RULES) is None  # SANDBOX_URL がなければ、ツールは付けない

    settings.sandbox_url = "http://sandbox:8080"
    client = FakeClient(_result(output="hello\n", files=[("a.png", b"\x89PNG")]))
    monkeypatch.setattr(code_execution, "SandboxClient", lambda url, token: client)
    tool = code_execution.build_tool(backend, RULES)
    assert tool is not None and tool.name == "run_python"
    assert "/outputs/" in tool.description and "60 秒" in tool.description

    call = {"type": "tool_call", "id": "call-1", "name": "run_python", "args": {"code": "print('hello')"}}
    message = asyncio.run(tool.ainvoke(call))
    assert "hello" in message.content
    files = code_execution.artifact_files(message.artifact)
    assert len(files) == 1 and files[0]["path"].endswith("/a.png")
    assert code_execution.artifact_files(None) == [] and code_execution.artifact_files({"files": ["x"]}) == []
