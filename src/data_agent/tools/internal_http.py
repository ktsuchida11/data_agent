"""MCP サーバー（社内の API）に HTTPS でつなぐための部品（tools/mcp.py で使う。ADR-0033）。

- 鍵は、設定ファイルやコードに書かない。${環境変数名} と書いて、.env に置く（expand_env）。
- 社内の認証局が発行した証明書を使うサーバーは、その認証局の証明書（PEM）を ca_file に指定する（ssl_context）。
  指定すると、その認証局が発行した証明書だけを信用する。指定しなければ、ふつうの証明書（公的な認証局）で確かめる。
- リダイレクトは追わない（鍵を付けたまま、別の場所へ送らないため）。プロキシの環境変数や .netrc も使わない。
"""

from __future__ import annotations

import os
import re
import ssl
from collections.abc import Mapping
from urllib.parse import urlsplit

import httpx

from data_agent.settings import PROJECT_ROOT

_ENV_REF = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def expand_env(text: str, where: str) -> str:
    """文字列の中の ${環境変数名} を、環境変数（.env）の値に置き換える。なければ RuntimeError（値は出さない）。"""

    def value(match: re.Match[str]) -> str:
        name = match.group(1)
        found = os.environ.get(name, "")
        if not found.strip():
            raise RuntimeError(f"{where} に書いた ${{{name}}} が、.env（環境変数）にありません")
        return found

    return _ENV_REF.sub(value, text)


def ca_path(ca_file: str) -> str:
    """ca_file（プロジェクトのフォルダからの場所）を、確かめてから絶対パスにする。"""
    path = (PROJECT_ROOT / ca_file).resolve()
    if not path.is_relative_to(PROJECT_ROOT.resolve()):
        raise RuntimeError(f"ca_file は、プロジェクトのフォルダの中に置いてください: {ca_file}")
    if not path.is_file():
        raise RuntimeError(f"ca_file のファイルがありません: {ca_file}")
    return str(path)


def ssl_context(ca_file: str | None) -> ssl.SSLContext | bool:
    """証明書の確かめ方。ca_file があれば、その認証局だけを信用する。なければ True（ふつうの確かめ方）。"""
    if not ca_file:
        return True
    try:
        return ssl.create_default_context(cafile=ca_path(ca_file))
    except ssl.SSLError as exc:
        raise RuntimeError(f"ca_file を証明書として読めません（{ca_file}）。PEM の形の証明書を指定してください") from exc


def check_url(url: str, *, allow_http: bool = False) -> None:
    parts = urlsplit(url)
    if parts.scheme == "http" and not allow_http:
        raise RuntimeError(f"http:// の通信は暗号化されません。https:// を使ってください: {url}")
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise RuntimeError(f"https:// で始まる URL にしてください: {url}")


def make_client_factory(verify: ssl.SSLContext | bool):
    """MCP のライブラリに渡す、httpx のクライアントを作る関数。"""

    def factory(
        headers: Mapping[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers=dict(headers or {}),
            timeout=timeout if timeout is not None else httpx.Timeout(30),
            auth=auth,
            verify=verify,
            follow_redirects=False,
            trust_env=False,
        )

    return factory


def describe_error(exc: BaseException) -> str:
    """つながらなかったときの理由（利用者とモデルに見せる）。接続先や鍵は入れない。"""
    leaf = _leaf(exc)
    if isinstance(leaf, httpx.HTTPStatusError):
        status = leaf.response.status_code
        if status in (401, 403):
            return f"鍵が違うか、権限がありません（{status}）"
        return f"サーバーがエラーを返しました（{status}）"
    if _is_certificate_error(leaf):
        return "サーバーの証明書を確かめられませんでした。ca_file を確かめてください"
    if isinstance(leaf, httpx.TimeoutException):
        return "時間内に返事がありませんでした"
    if isinstance(leaf, httpx.ConnectError):
        return "サーバーにつながりませんでした"
    return type(leaf).__name__


def _leaf(exc: BaseException) -> BaseException:
    """まとめて投げられた例外（ExceptionGroup）から、最初の 1 つを取り出す。"""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


def _is_certificate_error(exc: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(current):
            return True
        current = current.__cause__ or current.__context__
    return False
