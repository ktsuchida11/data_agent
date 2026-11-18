"""Langfuse に送った記録を、決めた日数が過ぎたら消す（ADR-0031）。

自分で動かす Langfuse には、残す期間の設定（Data Retention）がない（有料の Enterprise 版だけの機能）。
そのため、アプリが 1 日に 1 回、古い記録を Langfuse の公開 API で消す。日数は config/security.yaml の
tracing.retention_days（既定は 90 日。0 にすると消さない）。

- 消すのは、このアプリが送った記録だけ（印が data-agent のものと、名前を変える前の coding-agent のもの）。
  同じプロジェクトに、ほかのアプリが送った記録があっても消さない。
- 記録を消すと、Langfuse は、その中のモデルの呼び出しや評価もまとめて消す（Langfuse の裏で、順に消える）。
- 消す前に、消す記録をすべて集めてから消す（消しながら頁をめくると、頁がずれて取りこぼすため）。
- API は、SDK の型を通さずに呼ぶ（Langfuse の説明書の削除の例と同じ）。SDK は、返ってきた記録の項目が
  1 つ足りないだけで全体を失敗にする。Langfuse の版が上がって項目が変わっても止まらないよう、ID だけを読む。

手で確かめるとき（消さずに、件数だけを出す）:
    uv run python -m data_agent.storage.trace_retention
すぐに消すとき:
    uv run python -m data_agent.storage.trace_retention --delete
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from data_agent.safety.audit import audit

logger = logging.getLogger(__name__)

TAGS = ("data-agent", "coding-agent")  # このアプリが付けている印（coding-agent は、名前を変える前。ADR-0029）
PAGE_SIZE = 100
BATCH = 1000  # Langfuse が、1 回の呼び出しで消せる数の上限
MAX_PER_RUN = 20_000  # 1 回に消す数の上限。残りは次の回に消す

FIRST_DELAY_SECONDS = 60  # 起動してから、最初に消すまで（起動を遅くしないため）
EVERY_SECONDS = 24 * 60 * 60
RETRY_SECONDS = 60 * 60  # Langfuse につながらなかったときに、やり直すまで


class LangfuseTraces:
    """Langfuse の公開 API のうち、記録の一覧と削除だけを使う。"""

    def __init__(self, base_url: str, public_key: str, secret_key: str, **client_options: Any) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), auth=(public_key, secret_key), timeout=60, **client_options
        )

    @classmethod
    def from_env(cls) -> LangfuseTraces:
        """送るときと同じ環境変数を使う。送り先が書かれていなければ、使わない（よそのサーバーに向けないため）。"""
        base_url = os.environ.get("LANGFUSE_BASE_URL", "").strip()
        public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip()
        secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "").strip()
        if not (base_url and public_key and secret_key):
            raise RuntimeError("LANGFUSE_BASE_URL・LANGFUSE_PUBLIC_KEY・LANGFUSE_SECRET_KEY のどれかが設定されていません")
        return cls(base_url, public_key, secret_key)

    async def list_ids(self, *, tag: str, before: datetime, page: int, limit: int) -> tuple[list[str], int]:
        """before より前の、tag の付いた記録の ID と、全部の頁の数を返す（古い順）。"""
        response = await self._client.get(
            "/api/public/traces",
            params={
                "tags": tag,
                "toTimestamp": before.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "orderBy": "timestamp.asc",
                "fields": "core",  # 中身（入力と出力）は読まない
                "limit": limit,
                "page": page,
            },
        )
        response.raise_for_status()
        body = response.json()
        return [str(row["id"]) for row in body["data"]], int(body["meta"]["totalPages"])

    async def delete(self, trace_ids: list[str]) -> None:
        response = await self._client.request("DELETE", "/api/public/traces", json={"traceIds": trace_ids})
        response.raise_for_status()

    async def aclose(self) -> None:
        await self._client.aclose()


def cutoff_for(days: int, now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)) - timedelta(days=days)


async def find_expired(api: Any, cutoff: datetime, *, limit: int | None = None) -> list[str]:
    """cutoff より前の、このアプリの記録の ID を集める（古い順。重なりは除く）。"""
    limit = limit or MAX_PER_RUN
    found: dict[str, None] = {}
    for tag in TAGS:
        page = 1
        while len(found) < limit:
            ids, total_pages = await api.list_ids(tag=tag, before=cutoff, page=page, limit=PAGE_SIZE)
            for trace_id in ids:
                found.setdefault(trace_id, None)
            if not ids or page >= total_pages:
                break
            page += 1
    return list(found)[:limit]


async def purge(api: Any, days: int, *, now: datetime | None = None, delete: bool = True) -> int:
    """days 日より古い記録を消す。消した数（delete=False なら、消す対象の数）を返す。days が 0 なら何もしない。"""
    if days <= 0:
        return 0
    ids = await find_expired(api, cutoff_for(days, now))
    if delete:
        for start in range(0, len(ids), BATCH):
            await api.delete(ids[start : start + BATCH])
        audit("trace_retention", days=days, deleted=len(ids))
    return len(ids)


async def purge_with_env(days: int, *, delete: bool = True) -> int:
    api = LangfuseTraces.from_env()
    try:
        return await purge(api, days, delete=delete)
    finally:
        await api.aclose()


async def retention_loop(days: int) -> None:
    """アプリの中で、1 日に 1 回 purge() を呼ぶ。つながらなかったときは、1 時間後にやり直す。"""
    await asyncio.sleep(FIRST_DELAY_SECONDS)
    while True:
        try:
            deleted = await purge_with_env(days)
            if deleted:
                logger.info("Langfuse の記録のうち、%d 日より古い %d 件を消しました", days, deleted)
            wait = EVERY_SECONDS
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 止まると、以後は消されなくなる。記録して、あとでやり直す（エラーの文は、接続先などを含むので出さない）
            logger.warning("Langfuse の古い記録を消せませんでした（%s）。1 時間後にやり直します", type(exc).__name__)
            audit("trace_retention", days=days, error=type(exc).__name__)
            wait = RETRY_SECONDS
        await asyncio.sleep(wait)


def _main(argv: list[str] | None = None) -> int:
    from zoneinfo import ZoneInfo

    from data_agent.settings import get_security, get_settings  # .env もここで読まれる

    configured = get_security().tracing.retention_days
    parser = argparse.ArgumentParser(
        prog="python -m data_agent.storage.trace_retention",
        description="Langfuse に送った、このアプリの記録のうち、古いものを数える（--delete で消す）",
    )
    parser.add_argument("--days", type=int, default=configured, help=f"残す日数（既定は設定の {configured} 日）")
    parser.add_argument("--delete", action="store_true", help="数えるだけでなく、消す")
    args = parser.parse_args(argv)
    if args.days <= 0:
        print("残す日数が 0 なので、消しません（config/security.yaml の tracing.retention_days）。")
        return 0
    try:
        count = asyncio.run(purge_with_env(args.days, delete=args.delete))
    except RuntimeError as exc:
        print(f"{exc}。")
        return 1
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        reason = "鍵が違うか、権限がありません" if status in (401, 403) else "Langfuse の側のエラーです"
        print(f"Langfuse に断られました（{status}。{reason}）。")
        return 2
    except httpx.HTTPError as exc:
        print(f"Langfuse につながりませんでした（{type(exc).__name__}）。")
        return 2
    zone = get_settings().schedule_timezone  # 定時実行と同じタイムゾーンで見せる
    since = f"{cutoff_for(args.days).astimezone(ZoneInfo(zone)):%Y-%m-%d %H:%M}（{zone}）より前"
    if args.delete:
        print(f"{args.days} 日より古い記録（{since}）を {count} 件消しました。Langfuse の裏で、順に消えます。")
    else:
        print(f"{args.days} 日より古い記録（{since}）: {count} 件。消していません（消すときは --delete）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
