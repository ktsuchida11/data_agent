"""Python のコードを、隔離した別のコンテナ（sandbox/）で実行するツール。

Deep Agents 標準の execute ツールは使わない。execute を有効にすると、ファイルの権限の決まり
（.env や鍵ファイルの読み書き禁止）を同時に使えなくなるため（deepagents 0.7.32 で確認）。
代わりに、コードを実行する環境には作業フォルダを見せず、ファイルを受け渡す形にしている。

- コードに渡すファイルは、権限の決まりを確かめてから、作業フォルダから読んで送る。
- コードが作ったファイルは、受け取ってから、作業フォルダの出力用のフォルダに保存する。
- 実行する環境は、インターネットにも、アプリのデータベースにもつながっていない（compose.yaml）。
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from deepagents import FilesystemPermission
from deepagents.backends.protocol import BackendProtocol
from deepagents.backends.utils import validate_path
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from data_agent.safety.audit import audit
from data_agent.safety.guardrails import SECRET, Guard
from data_agent.settings import CodeExecutionLimits, get_guard, get_security, get_settings
from data_agent.tools import outputs

TOOL_NAME = "run_python"
_MB = 1024 * 1024
_SKIP_REASONS = {
    "too_many_files": "ファイルの数が多すぎます",
    "too_large": "大きすぎます",
    "not_a_file": "ふつうのファイルではありません",
}

_DESCRIPTION = """Python のコードを、隔離された環境で実行する。計算、データの集計、グラフ、Excel・PowerPoint・Word のファイルの作成、PDF からの文字の取り出しに使う。

- 環境: Python 3.12。pandas、numpy、matplotlib、openpyxl、xlsxwriter、python-pptx、python-docx、pypdf、pdfplumber、pillow、tabulate が使える。パッケージの追加はできない。
- インターネットにも、作業フォルダにも、つながっていない。使いたいファイルは input_files で渡す。カレントフォルダに、同じファイル名で置かれる。
- カレントフォルダに保存したファイルは、実行のあとで作業フォルダの {output_dir}/ の下に保存され、利用者が受け取れる。保存先のパスは結果に書かれる。
- 実行のたびに新しい環境になる。変数やファイルは、次の実行には残らない。
- 結果として返るのは、標準出力とエラー出力。確かめたい値は print する。
- matplotlib は日本語をそのまま描ける（フォントは設定済み）。画面はないので、plt.show() ではなく plt.savefig("名前.png") で保存する。
- 実行できる時間は、既定で {timeout} 秒。timeout で {max_timeout} 秒まで延ばせる。"""


class SandboxError(Exception):
    """コードを実行する環境とのやりとりに失敗した。メッセージは、そのままモデルに返す。"""


@dataclass
class SandboxResult:
    exit_code: int | None
    timed_out: bool
    output: str
    truncated: bool
    duration_ms: int
    files: list[tuple[str, bytes]] = field(default_factory=list)  # (名前, 中身)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (名前, 理由)


class SandboxClient:
    """コードを実行するコンテナ（sandbox/server.py）を呼び出す。"""

    def __init__(self, url: str, token: str | None = None) -> None:
        self._url = url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}

    async def run(self, code: str, files: list[tuple[str, bytes]], timeout: int) -> SandboxResult:
        body = {
            "code": code,
            "timeout": timeout,
            "files": [{"name": name, "content_b64": base64.b64encode(data).decode()} for name, data in files],
        }
        try:
            # trust_env=False: プロキシの設定があっても、実行環境には直接つなぐ。
            # 待ち時間は、実行の時間に、順番待ちと受け渡しの分を足す
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout + 60, connect=5), trust_env=False, headers=self._headers
            ) as client:
                response = await client.post(f"{self._url}/run", json=body)
        except httpx.HTTPError as exc:
            raise SandboxError(f"コードを実行する環境に接続できませんでした（{type(exc).__name__}）。") from exc
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code != 200:
            detail = data.get("error") if isinstance(data, dict) else None
            raise SandboxError(f"コードを実行できませんでした: {detail or f'HTTP {response.status_code}'}")
        return SandboxResult(
            exit_code=data.get("exit_code"),
            timed_out=bool(data.get("timed_out")),
            output=str(data.get("output") or ""),
            truncated=bool(data.get("truncated")),
            duration_ms=int(data.get("duration_ms") or 0),
            files=[(str(f["name"]), base64.b64decode(f["content_b64"])) for f in data.get("files") or []],
            skipped=[(str(s["name"]), str(s["reason"])) for s in data.get("skipped") or []],
        )


class RunPythonInput(BaseModel):
    code: str = Field(description="実行する Python のコード（スクリプトの全体）。")
    input_files: list[str] = Field(
        default_factory=list,
        description="コードから読みたいファイルのパス（例: /data/sales.csv）。カレントフォルダに、同じファイル名で置かれる。",
    )
    timeout: int | None = Field(default=None, description="実行できる時間（秒）。省略すると既定の時間。")


class CodeRunner:
    """run_python ツールの中身。ファイルの受け渡しと、権限の確認を行う。"""

    def __init__(
        self,
        client: SandboxClient,
        backend: BackendProtocol,
        permissions: list[FilesystemPermission],
        limits: CodeExecutionLimits,
        guard: Guard,
        timezone: str = "Asia/Tokyo",
    ) -> None:
        self._client = client
        self._backend = backend
        self._permissions = permissions
        self._limits = limits
        self._guard = guard
        self._timezone = ZoneInfo(timezone)

    async def run(self, code: str, input_files: list[str] | None = None, timeout: int | None = None) -> tuple[str, dict]:
        """コードを実行する。モデルに返す文章と、画面で使う情報（保存したファイルなど）を返す。"""
        limits = self._limits
        seconds = max(1, min(timeout or limits.timeout_seconds, limits.max_timeout_seconds))
        try:
            files = await self._read_inputs(input_files or [])
            result = await self._client.run(code, files, seconds)
        except SandboxError as exc:
            audit("code_execution", status="error", inputs=len(input_files or []))
            return str(exc), {"error": True, "files": []}

        saved, not_saved = await self._save_outputs(result)
        audit(
            "code_execution",
            status="timeout" if result.timed_out else "ok",
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
            inputs=len(files),
            outputs=len(saved),
            not_saved=len(not_saved) or None,
        )
        lines = [self._status_line(result, seconds)]
        if result.output.strip():
            lines += ["--- 出力 ---", result.output.rstrip()]
        if saved:
            lines += ["--- 保存したファイル ---", *(f"{path}（{outputs.size_text(size)}）" for path, size in saved)]
        if not_saved:
            lines += ["--- 保存しなかったファイル ---", *(f"{name}: {reason}" for name, reason in not_saved)]
        artifact = {
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "files": [{"path": path, "size": size} for path, size in saved],
        }
        return "\n".join(lines), artifact

    def _status_line(self, result: SandboxResult, seconds: int) -> str:
        took = f"{result.duration_ms / 1000:.1f} 秒"
        if result.timed_out:
            limit = self._limits.max_timeout_seconds
            return f"時間切れで止めました（{seconds} 秒）。timeout を指定すると、{limit} 秒まで延ばせます。"
        if result.exit_code == 0:
            return f"終了コード: 0（{took}）"
        if result.exit_code is not None and result.exit_code < 0:
            return f"途中で止められました（シグナル {-result.exit_code}、{took}）。メモリの使いすぎの可能性があります。"
        return f"終了コード: {result.exit_code}（{took}）"

    async def _read_inputs(self, paths: list[str]) -> list[tuple[str, bytes]]:
        """コードに渡すファイルを、権限を確かめてから読む。"""
        limits = self._limits
        if len(paths) > limits.max_input_files:
            raise SandboxError(f"渡せるファイルは {limits.max_input_files} 個までです。")
        validated: list[str] = []
        names: set[str] = set()
        for raw in paths:
            try:
                path = validate_path(raw)
            except ValueError:
                raise SandboxError(f"ファイルのパスが正しくありません: {raw}") from None
            if reason := self._denied("read", path):
                raise SandboxError(reason)
            name = PurePosixPath(path).name
            if name in names:
                raise SandboxError(f"同じファイル名のものは、一度に渡せません: {name}")
            names.add(name)
            validated.append(path)
        if not validated:
            return []
        files: list[tuple[str, bytes]] = []
        total = 0
        for response in await self._backend.adownload_files(validated):
            if response.error or response.content is None:
                raise SandboxError(f"ファイルを読めませんでした: {response.path}（{response.error}）")
            total += len(response.content)
            if total > limits.max_input_mb * _MB:
                raise SandboxError(f"渡すファイルの合計が大きすぎます（上限 {limits.max_input_mb} MB）。")
            files.append((PurePosixPath(response.path).name, response.content))
        return files

    async def _save_outputs(self, result: SandboxResult) -> tuple[list[tuple[str, int]], list[tuple[str, str]]]:
        """コードが作ったファイルを、作業フォルダに保存する。保存したものと、しなかったものを返す。"""
        not_saved = [(name, _SKIP_REASONS.get(reason, reason)) for name, reason in result.skipped]
        if not result.files:
            return [], not_saved
        folder = outputs.new_folder(self._limits.output_dir, self._timezone)
        uploads: list[tuple[str, bytes]] = []
        for name, data in result.files:
            try:
                path = validate_path(f"{folder}/{name}")
            except ValueError:
                not_saved.append((name, "ファイル名が正しくありません"))
                continue
            if self._denied("write", path):
                not_saved.append((name, "この名前のファイルは保存できません"))
            elif self._has_secret(data):
                # write_file と同じ扱い。秘密情報らしい文字列を含むファイルは作らせない
                audit("write_denied", tool=TOOL_NAME, path=path, reason="secret")
                not_saved.append((name, "秘密情報らしい文字列が含まれています"))
            else:
                uploads.append((path, data))
        saved: list[tuple[str, int]] = []
        if uploads:
            sizes = {path: len(data) for path, data in uploads}
            for response in await self._backend.aupload_files(uploads):
                if response.error:
                    not_saved.append((PurePosixPath(response.path).name, f"保存に失敗しました（{response.error}）"))
                else:
                    saved.append((response.path, sizes[response.path]))
        return saved, not_saved

    def _denied(self, operation: str, path: str) -> str | None:
        """権限の決まりで使えないファイルなら、その理由を返す。"""
        return outputs.denial_reason(self._permissions, self._backend, operation, path)

    def _has_secret(self, data: bytes) -> bool:
        """文章として読めるファイルに、秘密情報らしい文字列があるか。Excel などの中は調べられない。"""
        if len(data) > 2 * _MB:
            return False
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return False
        return self._guard.write_denial_reason(text) == SECRET


def build_tool(backend: BackendProtocol, permissions: list[FilesystemPermission]) -> BaseTool | None:
    """run_python ツールを作る。SANDBOX_URL が未設定か、設定で止めてあれば None を返す。"""
    settings = get_settings()
    limits = get_security().code_execution
    if not settings.sandbox_url or not limits.enabled:
        return None
    token = settings.sandbox_token.get_secret_value() if settings.sandbox_token else None
    runner = CodeRunner(
        SandboxClient(settings.sandbox_url, token), backend, permissions, limits, get_guard(), settings.schedule_timezone
    )
    return as_tool(runner, limits)


def as_tool(runner: CodeRunner, limits: CodeExecutionLimits) -> BaseTool:
    """コードを実行する役を、エージェントのツールの形にする。点検（redteam）では、実行しない役を渡して使う。"""

    async def run_python(code: str, input_files: list[str] | None = None, timeout: int | None = None) -> tuple[str, dict]:
        return await runner.run(code, input_files, timeout)

    def run_python_sync(code: str, input_files: list[str] | None = None, timeout: int | None = None) -> tuple[str, dict]:
        return asyncio.run(runner.run(code, input_files, timeout))

    return StructuredTool.from_function(
        func=run_python_sync,
        coroutine=run_python,
        name=TOOL_NAME,
        description=_DESCRIPTION.format(
            output_dir=limits.output_dir.rstrip("/"),
            timeout=limits.timeout_seconds,
            max_timeout=limits.max_timeout_seconds,
        ),
        args_schema=RunPythonInput,
        response_format="content_and_artifact",
    )


def artifact_files(artifact: Any) -> list[dict]:
    """ツールの結果に付けた情報から、保存したファイルの一覧を取り出す。"""
    files = artifact.get("files") if isinstance(artifact, dict) else None
    return [f for f in files or [] if isinstance(f, dict) and isinstance(f.get("path"), str)]
