"""ツールが作ったファイルを、作業フォルダに保存するときの共通の処理。

コードの実行（code_execution.py）と、データベースの問い合わせ（database.py）で使う。
"""

from __future__ import annotations

import secrets
from datetime import datetime
from zoneinfo import ZoneInfo

from deepagents import FilesystemPermission
from deepagents.backends.protocol import BackendProtocol

# 権限の判定は、ファイルツールと同じ関数を使う（リンクの先も確かめる）。
# deepagents の内部の関数なので、deepagents を上げたときは tests/test_code_execution.py で確かめる
from deepagents.middleware.filesystem import _check_fs_permission, _permission_error

_MB = 1024 * 1024


def denial_reason(permissions: list[FilesystemPermission], backend: BackendProtocol, operation: str, path: str) -> str | None:
    """権限の決まりで使えないファイルなら、その理由を返す。使えるなら None。"""
    if _check_fs_permission(permissions, operation, path) == "interrupt":
        return f"承認が必要なファイルは、このツールでは扱えません: {path}"
    return _permission_error(permissions, operation, backend, path)


def new_folder(output_dir: str, timezone: ZoneInfo) -> str:
    """保存先のフォルダの名前を作る。呼び出しごとに別のフォルダになる（例: /outputs/20261114-153000-ab12）。"""
    stamp = datetime.now(timezone).strftime("%Y%m%d-%H%M%S")
    return f"{output_dir.rstrip('/')}/{stamp}-{secrets.token_hex(2)}"


def size_text(size: int) -> str:
    return f"{size / _MB:.1f} MB" if size >= _MB else f"{max(size / 1024, 0.1):.1f} KB"
