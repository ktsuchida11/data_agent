"""定時実行の予定。cron の書式の解釈と、データベースへの保存。

予定は「cron 式」と「依頼文」の組で、利用者ごとに持つ。時刻が来たら、依頼文をエージェントに渡して
無人で実行する（schedule_runner.py）。

    0 9 * * 1-5 workspace の README を読んで、今日やることを3行でまとめて
    ↑ cron 式（分 時 日 月 曜日）  ↑ 依頼文

時刻は、設定したタイムゾーン（SCHEDULE_TIMEZONE、既定は日本時間）で解釈する。
データベースには、協定世界時（UTC）で保存する。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from cronsim import CronSim, CronSimError

if TYPE_CHECKING:
    from data_agent.storage.chat_history import ChatHistoryDataLayer

GROUP_NAME = "定時実行"  # 実行結果のチャットを入れるグループ（左の一覧で名前の先頭に付く）

SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS "Schedule" (
        id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        "userId"       UUID NOT NULL REFERENCES "User" (id) ON DELETE CASCADE,
        cron           TEXT NOT NULL,
        prompt         TEXT NOT NULL,
        model          TEXT,
        enabled        BOOLEAN NOT NULL DEFAULT true,
        "createdAt"    TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
        "nextRunAt"    TIMESTAMP NOT NULL,
        "lastRunAt"    TIMESTAMP,
        "lastStatus"   TEXT,
        "lastThreadId" UUID
    )
    """,
    'CREATE INDEX IF NOT EXISTS "Schedule_due_idx" ON "Schedule" ("nextRunAt") WHERE enabled',
    'CREATE INDEX IF NOT EXISTS "Schedule_userId_idx" ON "Schedule" ("userId")',
)


class ScheduleError(ValueError):
    """予定として受け付けられない入力。メッセージは、そのまま利用者に見せる。"""


@dataclass(frozen=True)
class ParsedSchedule:
    cron: str
    prompt: str


def utc_now() -> datetime:
    """いまの時刻（UTC、タイムゾーン情報なし）。データベースの列と同じ形。"""
    return datetime.now(UTC).replace(tzinfo=None)


def next_run(cron: str, timezone: str, after: datetime | None = None) -> datetime:
    """after（UTC）より後で、最初に cron 式に合う時刻を UTC で返す。"""
    tz = ZoneInfo(timezone)
    start = (after or utc_now()).replace(tzinfo=UTC).astimezone(tz)
    return next(CronSim(cron, start)).astimezone(UTC).replace(tzinfo=None)


def upcoming(cron: str, timezone: str, count: int = 3, after: datetime | None = None) -> list[datetime]:
    """これから実行される時刻を、設定したタイムゾーンで count 件返す（利用者に見せる用）。"""
    tz = ZoneInfo(timezone)
    runs = CronSim(cron, (after or utc_now()).replace(tzinfo=UTC).astimezone(tz))
    return [next(runs) for _ in range(count)]


def parse(text: str, *, timezone: str, min_interval_minutes: int, max_prompt_chars: int) -> ParsedSchedule:
    """「cron 式（5項目）＋依頼文」の形の入力を解釈する。受け付けられなければ ScheduleError。"""
    parts = text.split()
    if len(parts) < 6:
        raise ScheduleError(
            "「分 時 日 月 曜日」の5項目のあとに、依頼文を書いてください。\n"
            "例: `0 9 * * 1-5 README を読んで、今日やることを3行でまとめて`（平日の朝9時）"
        )
    cron = " ".join(parts[:5])
    prompt = text.split(None, 5)[5].strip()
    try:
        first, second = upcoming(cron, timezone, count=2)
    except (CronSimError, StopIteration) as exc:
        raise ScheduleError(f"時刻の指定（`{cron}`）を読み取れませんでした: {exc}") from exc
    if second - first < timedelta(minutes=min_interval_minutes):
        raise ScheduleError(f"実行の間隔が短すぎます。{min_interval_minutes} 分以上あけてください。")
    if len(prompt) > max_prompt_chars:
        raise ScheduleError(f"依頼文が長すぎます（上限 {max_prompt_chars} 文字）。")
    return ParsedSchedule(cron=cron, prompt=prompt)


def format_time(moment: datetime) -> str:
    return moment.strftime("%m/%d %H:%M")


def to_local(moment: datetime, timezone: str) -> datetime:
    """データベースの時刻（UTC）を、設定したタイムゾーンに直す。"""
    return moment.replace(tzinfo=UTC).astimezone(ZoneInfo(timezone))


class ScheduleStore:
    """予定の保存。更新と削除は、本人の予定だけを対象にする。"""

    def __init__(self, history: ChatHistoryDataLayer) -> None:
        self._db = history

    async def create(self, user_id: str, cron: str, prompt: str, model: str | None, next_run_at: datetime) -> dict:
        rows = await self._db.execute_query(
            """
            INSERT INTO "Schedule" ("userId", cron, prompt, model, "nextRunAt")
            VALUES ($1, $2, $3, $4, $5) RETURNING *
            """,
            {"user_id": user_id, "cron": cron, "prompt": prompt, "model": model, "next": next_run_at},
        )
        return rows[0]

    async def list_for_user(self, user_id: str) -> list[dict[str, Any]]:
        return await self._db.execute_query(
            'SELECT * FROM "Schedule" WHERE "userId" = $1 ORDER BY "createdAt"', {"user_id": user_id}
        )

    async def count_for_user(self, user_id: str) -> int:
        rows = await self._db.execute_query('SELECT count(*) AS n FROM "Schedule" WHERE "userId" = $1', {"user_id": user_id})
        return rows[0]["n"]

    async def set_enabled(self, schedule_id: str, user_id: str, enabled: bool, next_run_at: datetime) -> dict | None:
        rows = await self._db.execute_query(
            """
            UPDATE "Schedule" SET enabled = $3, "nextRunAt" = $4
            WHERE id = $1 AND "userId" = $2 RETURNING *
            """,
            {"id": schedule_id, "user_id": user_id, "enabled": enabled, "next": next_run_at},
        )
        return rows[0] if rows else None

    async def delete(self, schedule_id: str, user_id: str) -> dict | None:
        rows = await self._db.execute_query(
            'DELETE FROM "Schedule" WHERE id = $1 AND "userId" = $2 RETURNING *',
            {"id": schedule_id, "user_id": user_id},
        )
        return rows[0] if rows else None

    async def claim_due(self, timezone: str) -> list[dict[str, Any]]:
        """実行の時刻が来た予定を、自分の担当として確保して返す。

        確保は「次の実行時刻を書き換えられたら勝ち」で決める。アプリが複数動いていても、
        同じ回を二重に実行しない。止まっていた間に過ぎた回は、まとめて1回だけ実行する。
        """
        now = utc_now()
        due = await self._db.execute_query(
            'SELECT id, cron, "nextRunAt" FROM "Schedule" WHERE enabled AND "nextRunAt" <= $1', {"now": now}
        )
        claimed = []
        for row in due:
            rows = await self._db.execute_query(
                """
                UPDATE "Schedule" SET "nextRunAt" = $3, "lastRunAt" = $4, "lastStatus" = 'running'
                WHERE id = $1 AND "nextRunAt" = $2 AND enabled RETURNING *
                """,
                {"id": row["id"], "old": row["nextRunAt"], "new": next_run(row["cron"], timezone, now), "now": now},
            )
            claimed.extend(rows)
        return claimed

    async def finish(self, schedule_id: str, status: str, thread_id: str) -> None:
        await self._db.execute_query(
            'UPDATE "Schedule" SET "lastStatus" = $2, "lastThreadId" = $3 WHERE id = $1',
            {"id": schedule_id, "status": status, "thread_id": thread_id},
        )
