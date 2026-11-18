"""コード実行専用のサーバー（sandbox/server.py）の、実行と後片付け。

手元の Python でそのまま動かす（pandas などは使わない）。コンテナとしての隔離
（ネットワーク、メモリの上限など）は tests/e2e/check_sandbox.py で確かめる。
"""

from __future__ import annotations

import base64
import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location("sandbox_server", Path(__file__).resolve().parents[1] / "sandbox" / "server.py")
server = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(server)


def _file(name: str, data: bytes) -> dict:
    return {"name": name, "content_b64": base64.b64encode(data).decode()}


def _names(result: dict) -> list[str]:
    return [f["name"] for f in result["files"]]


def test_runs_code_and_returns_output_and_new_files(tmp_path: Path) -> None:
    code = "import os\nprint(open('in.txt').read().upper())\nopen('out.txt', 'w').write('made')\nos.makedirs('d'); open('d/x.txt', 'w').write('1')\n"
    result = server.run_code(code, [_file("in.txt", b"hello")], 20, work_root=tmp_path)
    assert result["exit_code"] == 0 and not result["timed_out"]
    assert result["output"] == "HELLO\n"
    assert _names(result) == ["d/x.txt", "out.txt"]  # 渡したままの in.txt は返さない
    assert base64.b64decode(result["files"][1]["content_b64"]) == b"made"
    assert list(tmp_path.iterdir()) == []  # 一時フォルダは残さない


def test_modified_input_is_returned(tmp_path: Path) -> None:
    result = server.run_code("open('in.txt', 'a').write('!')", [_file("in.txt", b"hello")], 20, work_root=tmp_path)
    assert _names(result) == ["in.txt"]


def test_error_and_exit_code(tmp_path: Path) -> None:
    result = server.run_code("raise ValueError('boom')", [], 20, work_root=tmp_path)
    assert result["exit_code"] == 1 and "ValueError: boom" in result["output"]


def test_timeout_stops_the_code_and_its_children(tmp_path: Path) -> None:
    code = (
        "import subprocess, sys, time\nsubprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\ntime.sleep(60)\n"
    )
    result = server.run_code(code, [], 1, work_root=tmp_path)
    assert result["timed_out"] and result["exit_code"] is None
    assert result["duration_ms"] < 10_000
    assert list(tmp_path.iterdir()) == []


def test_environment_is_not_inherited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANDBOX_TOKEN", "secret-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-not-leak")
    result = server.run_code("import os\nprint(sorted(os.environ))", [], 20, work_root=tmp_path)
    assert "SANDBOX_TOKEN" not in result["output"] and "ANTHROPIC_API_KEY" not in result["output"]
    assert "MPLBACKEND" in result["output"]


def test_leftover_files_outside_the_run_folder_are_removed(tmp_path: Path) -> None:
    code = "import os\nopen(os.path.join(os.path.dirname(os.path.dirname(os.getcwd())), 'left.txt'), 'w').write('x')\n"
    result = server.run_code(code, [], 20, work_root=tmp_path)
    assert result["exit_code"] == 0 and list(tmp_path.iterdir()) == []


def test_output_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "MAX_FILES", 2)
    monkeypatch.setattr(server, "MAX_FILE_BYTES", 10)
    code = "import os\nfor n in 'abc':\n    open(n + '.txt', 'w').write(n)\nopen('big.txt', 'w').write('x' * 50)\nos.symlink('/etc/hosts', 'link')\n"
    result = server.run_code(code, [], 20, work_root=tmp_path)
    assert _names(result) == ["a.txt", "b.txt"]
    assert {(s["name"], s["reason"]) for s in result["skipped"]} == {
        ("big.txt", "too_many_files"),
        ("c.txt", "too_many_files"),
        ("link", "not_a_file"),
    }
    monkeypatch.setattr(server, "MAX_FILES", 20)
    result = server.run_code("open('big.txt', 'w').write('x' * 50)", [], 20, work_root=tmp_path)
    assert result["files"] == [] and result["skipped"] == [{"name": "big.txt", "reason": "too_large"}]


def test_long_output_keeps_head_and_tail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "MAX_OUTPUT_CHARS", 200)
    for text in ("あ" * 250, "あ" * 5000, "x" * 100_000):  # 上限を少し超える場合と、大きく超える場合
        result = server.run_code(f"print('HEAD' + {text!r} + 'TAIL')", [], 20, work_root=tmp_path)
        assert result["truncated"] and len(result["output"]) <= 220, len(result["output"])
        assert result["output"].startswith("HEAD") and result["output"].rstrip().endswith("TAIL")
    result = server.run_code("print('short')", [], 20, work_root=tmp_path)
    assert result["output"] == "short\n" and not result["truncated"]


@pytest.mark.parametrize("name", ["../x", "/etc/passwd", "a/../b", "", "a\\b", "x" * 300, None, 3])
def test_unsafe_file_names_are_rejected(name: object) -> None:
    with pytest.raises(server.BadRequest):
        server.safe_name(name)


def test_safe_file_names_are_kept() -> None:
    assert server.safe_name("data/売上.csv") == "data/売上.csv"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"code": "   "},
        {"code": "print(1)", "files": "x"},
        {"code": "print(1)", "files": ["x"]},
        {"code": "print(1)", "timeout": 0},
        {"code": "print(1)", "timeout": "5"},
        {"code": "print(1)", "timeout": True},
        {"code": "print(1)", "files": [{"name": "a.txt", "content_b64": "###"}]},
        {"code": "print(1)", "files": [{"name": "a.txt", "content_b64": ""}, {"name": "a.txt", "content_b64": ""}]},
    ],
)
def test_bad_requests_are_rejected(body: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "WORK_ROOT", tmp_path)
    with pytest.raises(server.BadRequest):
        server.handle_run(body)
    assert list(tmp_path.iterdir()) == []


def test_timeout_is_capped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "WORK_ROOT", tmp_path)
    seen: list[int] = []
    monkeypatch.setattr(server, "run_code", lambda code, files, timeout: seen.append(timeout) or {})
    server.handle_run({"code": "print(1)", "timeout": 100_000})
    assert seen == [server.MAX_TIMEOUT]
