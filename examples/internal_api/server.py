"""社内 API の見本（チュートリアルと、画面の回帰確認で使う。本番では使わない）。

架空の「受注システム」を、HTTPS で公開する。中身は、決まった架空のデータだけ。

- MCP の入口: https://<ホスト>:8443/mcp （ツール: search_orders・get_order・update_order_status・delete_order）
- 鍵: 環境変数 EXAMPLE_API_TOKEN。「Authorization: Bearer <鍵>」がなければ断る（/healthz を除く）
- 証明書: 社内の認証局の代わりに、自分で認証局を作り、その認証局でサーバーの証明書を発行する。
  認証局の証明書（公開してよいもの）は /certs/ca.crt に書く。アプリは、これを ca_file として読む。
  認証局の秘密鍵は /ca-key に置き、アプリには渡さない。

    python examples/internal_api/server.py
"""

from __future__ import annotations

import datetime as dt
import hmac
import ipaddress
import json
import os
import sys
import tempfile
from pathlib import Path

import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

PORT = int(os.environ.get("EXAMPLE_API_PORT", "8443"))
HOSTS = [
    h.strip() for h in os.environ.get("EXAMPLE_API_HOSTS", "internal-api-example,localhost,127.0.0.1").split(",") if h.strip()
]
CERTS = Path(os.environ.get("EXAMPLE_API_CERTS", "/certs"))  # 認証局の証明書を書く場所（アプリと共有する）
CA_KEY_DIR = Path(os.environ.get("EXAMPLE_API_CA_KEY_DIR", "/ca-key"))  # 認証局の秘密鍵（アプリには渡さない）

# 架空のデータ。担当者の連絡先は、ガードレールでマスクされることを確かめるためのもの（実在しない）
ORDERS = [
    {
        "id": "ORD-1001",
        "customer": "はまなす商事",
        "item": "ノート PC",
        "quantity": 12,
        "amount": 1_560_000,
        "status": "出荷済み",
        "contact": "受注担当 hamanasu-order@example.com / 03-1234-5678",
    },
    {
        "id": "ORD-1002",
        "customer": "はまなす商事",
        "item": "モニター",
        "quantity": 20,
        "amount": 640_000,
        "status": "受付",
        "contact": "受注担当 hamanasu-order@example.com / 03-1234-5678",
    },
    {
        "id": "ORD-1003",
        "customer": "あおば電機",
        "item": "サーバー",
        "quantity": 2,
        "amount": 2_980_000,
        "status": "製造中",
        "contact": "購買部 aoba-buy@example.com / 06-2345-6789",
    },
    {
        "id": "ORD-1004",
        "customer": "ことり食品",
        "item": "タブレット",
        "quantity": 30,
        "amount": 1_350_000,
        "status": "受付",
        "contact": "総務部 kotori@example.com / 092-345-6789",
    },
]
STATUSES = ("受付", "製造中", "出荷済み", "取り消し")


def _find(order_id: str) -> dict | None:
    return next((o for o in ORDERS if o["id"] == order_id.strip().upper()), None)


def _summary(order: dict) -> dict:
    return {key: order[key] for key in ("id", "customer", "item", "quantity", "amount", "status")}


mcp = FastMCP(
    "orders-example",
    host="0.0.0.0",  # noqa: S104  コンテナの中で動かし、ほかのコンテナから届くようにする
    port=PORT,
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=[f"{host}:*" for host in HOSTS]
    ),
)


@mcp.tool()
def search_orders(customer: str) -> str:
    """取引先の名前（一部でもよい）で、受注を探す。受注番号・品目・数量・金額・状態を返す。"""
    found = [_summary(o) for o in ORDERS if customer.strip() and customer.strip() in o["customer"]]
    return json.dumps({"orders": found, "count": len(found)}, ensure_ascii=False)


@mcp.tool()
def get_order(order_id: str) -> str:
    """受注番号（ORD-1001 など）で、受注の詳細（担当者の連絡先を含む）を返す。"""
    order = _find(order_id)
    return json.dumps(order or {"error": f"受注 {order_id} は見つかりません"}, ensure_ascii=False)


@mcp.tool()
def update_order_status(order_id: str, status: str) -> str:
    """受注の状態を変える（受付・製造中・出荷済み・取り消し のどれか）。書き換える操作。"""
    order = _find(order_id)
    if order is None:
        return json.dumps({"error": f"受注 {order_id} は見つかりません"}, ensure_ascii=False)
    if status not in STATUSES:
        return json.dumps({"error": f"状態は {'・'.join(STATUSES)} のどれかにしてください"}, ensure_ascii=False)
    order["status"] = status
    return json.dumps({"updated": _summary(order)}, ensure_ascii=False)


@mcp.tool()
def delete_order(order_id: str) -> str:
    """受注を消す。見本の pack.yaml には書いていないので、エージェントからは使えない（使うツールだけを書く例）。"""
    order = _find(order_id)
    if order is not None:
        ORDERS.remove(order)
    return json.dumps({"deleted": order_id}, ensure_ascii=False)


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


class BearerAuth:
    """鍵を確かめる（/healthz を除く）。"""

    def __init__(self, app, token: str) -> None:
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope["path"] != "/healthz":
            sent = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(sent, self.expected):
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


# ---------- 証明書 ----------
def _name(common: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common)])


def certificate_authority() -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    """見本の認証局。秘密鍵があれば使い続ける（アプリが読んだ認証局の証明書が、変わらないように）。"""
    key_file, cert_file = CA_KEY_DIR / "ca.key", CA_KEY_DIR / "ca.crt"
    if key_file.exists() and cert_file.exists():
        key = serialization.load_pem_private_key(key_file.read_bytes(), password=None)
        return x509.load_pem_x509_certificate(cert_file.read_bytes()), key  # type: ignore[return-value]
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name("data-agent example CA"))
        .issuer_name(_name("data-agent example CA"))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
        .sign(key, hashes.SHA256())
    )
    CA_KEY_DIR.mkdir(parents=True, exist_ok=True)
    key_file.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    key_file.chmod(0o600)
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert, key


def server_certificate(ca: x509.Certificate, ca_key: ec.EllipticCurvePrivateKey, folder: Path) -> tuple[str, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    names: list[x509.GeneralName] = []
    for host in HOSTS:
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(host)))
        except ValueError:
            names.append(x509.DNSName(host))
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name(HOSTS[0]))
        .issuer_name(ca.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=90))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    cert_path, key_path = folder / "server.crt", folder / "server.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM) + ca.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    key_path.chmod(0o600)
    return str(cert_path), str(key_path)


def main() -> int:
    token = os.environ.get("EXAMPLE_API_TOKEN", "").strip()
    if len(token) < 16:
        print("EXAMPLE_API_TOKEN（16 文字以上）を設定してください（.env に書く。docs/tutorials/04-mcp.md）。", file=sys.stderr)
        return 1
    ca, ca_key = certificate_authority()
    CERTS.mkdir(parents=True, exist_ok=True)
    (CERTS / "ca.crt").write_bytes(ca.public_bytes(serialization.Encoding.PEM))  # アプリが ca_file として読む
    workdir = Path(tempfile.mkdtemp(prefix="example-api-"))
    cert_file, key_file = server_certificate(ca, ca_key, workdir)
    print(f"社内 API の見本: https://{HOSTS[0]}:{PORT}/mcp （認証局の証明書: {CERTS / 'ca.crt'}）", flush=True)
    uvicorn.run(
        BearerAuth(mcp.streamable_http_app(), token),
        host="0.0.0.0",  # noqa: S104  コンテナの中で動かし、ほかのコンテナから届くようにする
        port=PORT,
        ssl_certfile=cert_file,
        ssl_keyfile=key_file,
        log_level="warning",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
