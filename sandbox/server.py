"""コード実行専用のサーバー。アプリ本体とは別のコンテナで動かす。

アプリから受け取った Python のコードを、実行ごとの一時フォルダの中で動かし、
標準出力と、作られたファイルを返す。

    POST /run     {"code": "...", "timeout": 60, "files": [{"name": "a.csv", "content_b64": "..."}]}
    GET  /health

このコンテナには、秘密情報も、作業フォルダも、外へ出るネットワークも持たせない
（compose.yaml の sandbox を参照）。コードが触れるのは、受け取ったファイルだけ。
実行は1件ずつ行い、終わったら一時フォルダと、残っているプロセスをすべて片付ける。

標準ライブラリだけで動く（pandas などは、実行されるコードのために入れてある）。
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import hmac
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath

MB = 1024 * 1024


def _int_env(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


WORK_ROOT = Path(os.environ.get("SANDBOX_WORK_ROOT", "/work"))
PORT = _int_env("SANDBOX_PORT", 8080)
TOKEN = os.environ.get("SANDBOX_TOKEN", "")  # 設定したときだけ、Authorization ヘッダーを確かめる
MAX_TIMEOUT = _int_env("SANDBOX_MAX_TIMEOUT", 300)  # 1回の実行の上限（秒）
MAX_OUTPUT_CHARS = _int_env("SANDBOX_MAX_OUTPUT_CHARS", 20_000)  # 返す標準出力の上限（文字）
MAX_FILES = _int_env("SANDBOX_MAX_FILES", 20)  # 返すファイルの数の上限
MAX_FILE_BYTES = _int_env("SANDBOX_MAX_FILE_MB", 20) * MB  # 返すファイル1つの上限
MAX_TOTAL_BYTES = _int_env("SANDBOX_MAX_TOTAL_MB", 40) * MB  # 返すファイルの合計の上限
MAX_REQUEST_BYTES = _int_env("SANDBOX_MAX_REQUEST_MB", 64) * MB  # 受け取るリクエストの上限
MAX_WRITE_BYTES = _int_env("SANDBOX_MAX_WRITE_MB", 100) * MB  # コードが書けるファイル1つの上限
QUEUE_SECONDS = _int_env("SANDBOX_QUEUE_SECONDS", 30)  # ほかの実行が終わるのを待つ時間
MATPLOTLIBRC = Path(__file__).with_name("matplotlibrc")  # 日本語フォントの設定

_lock = threading.Lock()  # 実行は1件ずつ（後片付けで、残ったプロセスをまとめて止めるため）


class BadRequest(ValueError):
    """リクエストの形が正しくない。"""


_KILL_PASSES = 20  # 残ったプロセスを止める回数の上限。超えたら、サーバーを止める


def safe_name(name: object) -> str:
    """ファイル名を確かめる。一時フォルダの外を指す名前は受け付けない。"""
    if not isinstance(name, str) or not name or len(name) > 200 or "\\" in name or "\x00" in name:
        raise BadRequest("ファイル名が正しくありません")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise BadRequest(f"ファイル名が正しくありません: {name}")
    return path.as_posix()


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(MB), b""):
            h.update(chunk)
    return h.hexdigest()


def _env(run_dir: Path) -> dict[str, str]:
    """実行するコードに渡す環境変数。サーバー自身の環境変数は引き継がない。"""
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(run_dir / "home"),
        "TMPDIR": str(run_dir / "tmp"),
        "MPLCONFIGDIR": str(run_dir / "mpl"),
        "MPLBACKEND": "Agg",  # 画面がないので、画像として描く
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(run_dir / "work"),
    }
    if MATPLOTLIBRC.exists():
        env["MATPLOTLIBRC"] = str(MATPLOTLIBRC)
    return env


def _limit(pid: int) -> None:
    """実行するプロセスに上限を付ける（Linux だけ）。"""
    try:
        import resource

        if hasattr(resource, "prlimit"):
            resource.prlimit(pid, resource.RLIMIT_CORE, (0, 0))
            resource.prlimit(pid, resource.RLIMIT_FSIZE, (MAX_WRITE_BYTES, MAX_WRITE_BYTES))
        # メモリが足りなくなったとき、サーバーではなく、実行中のコードが先に止められるようにする
        Path(f"/proc/{pid}/oom_score_adj").write_text("1000")
    except (OSError, ValueError, ImportError):
        pass


def _kill_group(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)


def _kill_leftovers() -> None:
    """実行のあとに残っているプロセスをすべて止める（Linux のコンテナの中だけ）。

    コードが裏でプロセスを起動したままにして、次の実行をのぞくのを防ぐ。
    実行は1件ずつなので、サーバー自身とその親のほかに、動いていてよいプロセスはない。
    止めている間に、新しいプロセスが作られることがある（二重の fork）ので、何も残らなくなるまで繰り返す。
    止めきれなければ、サーバーを止める（コンテナが起動し直して、きれいな状態に戻る）。
    """
    if os.environ.get("SANDBOX_KILL_LEFTOVERS") != "1":
        return
    keep = {os.getpid(), os.getppid(), 1}
    for _ in range(_KILL_PASSES):
        killed = _kill_others(keep)
        _reap()
        if not killed:
            break
        time.sleep(0.05)
    else:
        print("実行のあとのプロセスを止めきれませんでした。サーバーを止めます", file=sys.stderr, flush=True)
        os._exit(1)
    # 実行をまたいで書き込める場所を空にする（次の実行に、ファイルを残させない）
    for shared in ("/tmp", "/dev/shm", "/dev/mqueue"):  # noqa: S108  使い終わった一時の場所を空にする
        _empty(Path(shared))


def _kill_others(keep: set[int]) -> int:
    """サーバーのほかの、動いているプロセスに SIGKILL を送る。送れた数を返す（止まって後始末を待つものは数えない）。"""
    killed = 0
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) in keep or _is_zombie(entry):
            continue
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(int(entry.name), signal.SIGKILL)
            killed += 1
    return killed


def _is_zombie(entry: Path) -> bool:
    try:
        return entry.joinpath("stat").read_text().rsplit(")", 1)[1].split()[0] == "Z"
    except (OSError, IndexError):
        return False


def _reap() -> None:
    """止めたプロセスの後始末（終わったプロセスを残さない）。"""
    while True:
        try:
            if os.waitpid(-1, os.WNOHANG)[0] == 0:
                return
        except ChildProcessError:
            return


def _empty(folder: Path) -> None:
    """フォルダの中身をすべて消す。"""
    try:
        entries = list(folder.iterdir())
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink()
        except OSError:
            pass


def _read_output(path: Path) -> tuple[str, bool]:
    """標準出力を読む。長すぎるときは、先頭と末尾だけを返す（エラーは末尾に出るため）。"""
    half = MAX_OUTPUT_CHARS // 2
    window = half * 4  # 1文字は最大4バイトなので、これだけ読めば half 文字は必ず取れる
    size = path.stat().st_size
    if size <= window * 2:
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) <= MAX_OUTPUT_CHARS:
            return text, False
        head, tail = text[:half], text[-half:]
    else:
        with path.open("rb") as f:
            head = f.read(window).decode("utf-8", errors="replace")[:half]
            f.seek(size - window)
            tail = f.read().decode("utf-8", errors="replace")[-half:]
    return f"{head}\n…（途中を省略）…\n{tail}", True


def _collect(work: Path, inputs: dict[str, str]) -> tuple[list[dict], list[dict]]:
    """コードが作った（または書き換えた）ファイルを集める。受け取ったままのファイルは返さない。"""
    files: list[dict] = []
    skipped: list[dict] = []
    total = 0
    for path in sorted(p for p in work.rglob("*") if not p.is_dir()):
        name = path.relative_to(work).as_posix()
        if path.is_symlink() or not path.is_file():
            skipped.append({"name": name, "reason": "not_a_file"})
            continue
        size = path.stat().st_size
        if name in inputs and _digest(path) == inputs[name]:
            continue
        if len(files) >= MAX_FILES:
            skipped.append({"name": name, "reason": "too_many_files"})
        elif size > MAX_FILE_BYTES or total + size > MAX_TOTAL_BYTES:
            skipped.append({"name": name, "reason": "too_large"})
        else:
            total += size
            files.append({"name": name, "size": size, "content_b64": base64.b64encode(path.read_bytes()).decode()})
    return files, skipped


def run_code(code: str, files: list[dict], timeout: int, *, work_root: Path | None = None) -> dict:
    """コードを一時フォルダの中で実行して、結果を返す。"""
    root = work_root or WORK_ROOT
    run_dir = Path(tempfile.mkdtemp(prefix="run-", dir=root))
    try:
        work = run_dir / "work"
        for sub in ("work", "home", "tmp", "mpl"):
            (run_dir / sub).mkdir()
        inputs: dict[str, str] = {}
        for item in files:
            name = safe_name(item.get("name"))
            if name in inputs:
                raise BadRequest(f"同じ名前のファイルがあります: {name}")
            try:
                data = base64.b64decode(item.get("content_b64", ""), validate=True)
            except (binascii.Error, TypeError, ValueError):
                raise BadRequest(f"ファイルの中身が読めません: {name}") from None
            target = work / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            inputs[name] = hashlib.sha256(data).hexdigest()

        script = run_dir / "main.py"
        script.write_text(code, encoding="utf-8")
        log = run_dir / "output.log"
        started = time.monotonic()
        with log.open("wb") as out:
            proc = subprocess.Popen(  # noqa: S603  受け取ったコードを動かすのが、このサーバーの役割
                [sys.executable, "-B", str(script)],
                cwd=work,
                env=_env(run_dir),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # 時間切れのときに、子プロセスごとまとめて止められるようにする
            )
            _limit(proc.pid)
            timed_out = False
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
            _kill_group(proc.pid)  # 正常に終わったときも、残した子プロセスを止める
            proc.wait()
        duration_ms = int((time.monotonic() - started) * 1000)
        output, truncated = _read_output(log)
        collected, skipped = _collect(work, inputs)
        return {
            "exit_code": None if timed_out else proc.returncode,
            "timed_out": timed_out,
            "output": output,
            "truncated": truncated,
            "duration_ms": duration_ms,
            "files": collected,
            "skipped": skipped,
        }
    finally:
        _kill_leftovers()
        _empty(root)  # 一時フォルダのほかに、コードが置いていったファイルも消す


def handle_run(body: dict) -> dict:
    """リクエストの中身を確かめてから実行する。"""
    code = body.get("code")
    files = body.get("files") or []
    timeout = body.get("timeout", 60)
    if not isinstance(code, str) or not code.strip():
        raise BadRequest("code がありません")
    if not isinstance(files, list) or not all(isinstance(f, dict) for f in files):
        raise BadRequest("files の形が正しくありません")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 1:
        raise BadRequest("timeout は1以上の整数にしてください")
    return run_code(code, files, min(timeout, MAX_TIMEOUT))


class Handler(BaseHTTPRequestHandler):
    server_version = "sandbox"

    def log_message(self, *args: object) -> None:  # アクセスログは出さない（実行の記録は下で出す）
        pass

    def _send(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/run":
            self._send(404, {"error": "not found"})
            return
        if TOKEN and not hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {TOKEN}"):
            self._send(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._send(411, {"error": "Content-Length が必要です"})
            return
        if length > MAX_REQUEST_BYTES:
            self._send(413, {"error": "リクエストが大きすぎます"})
            return
        try:
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise BadRequest("JSON のオブジェクトを送ってください")
        except (ValueError, UnicodeDecodeError) as exc:
            self._send(400, {"error": str(exc) if isinstance(exc, BadRequest) else "JSON として読めません"})
            return
        if not _lock.acquire(timeout=QUEUE_SECONDS):
            self._send(503, {"error": "ほかのコードを実行中です。少し待ってから、もう一度実行してください"})
            return
        try:
            result = handle_run(body)
        except BadRequest as exc:
            self._send(400, {"error": str(exc)})
            return
        except Exception as exc:
            print(json.dumps({"log": "sandbox", "event": "error", "type": type(exc).__name__}), flush=True)
            self._send(500, {"error": f"実行環境の中でエラーが起きました（{type(exc).__name__}）"})
            return
        finally:
            _lock.release()
        print(  # 実行の記録。コードの中身や出力は残さない
            json.dumps(
                {
                    "log": "sandbox",
                    "exit_code": result["exit_code"],
                    "timed_out": result["timed_out"],
                    "duration_ms": result["duration_ms"],
                    "inputs": len(body.get("files") or []),
                    "outputs": len(result["files"]),
                }
            ),
            flush=True,
        )
        self._send(200, result)


def _harden() -> None:
    """実行されるコードから、サーバーのメモリや環境変数をのぞけないようにする（Linux だけ）。"""
    try:
        import ctypes

        ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE = 0
    except (OSError, AttributeError):
        pass


def main() -> None:
    _harden()
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    _empty(WORK_ROOT)  # 前回、途中で止まったときの残り
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)  # noqa: S104  外からどこまで届くかは、起動する側で決める
    server.daemon_threads = True
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    print(json.dumps({"log": "sandbox", "event": "started", "port": PORT}), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
