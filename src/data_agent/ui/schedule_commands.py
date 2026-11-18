"""定時実行のコマンド。入力欄で `/` を打つと選べる。

- 定時実行: 「cron 式（分 時 日 月 曜日）＋依頼文」を書いて送ると、内容を確認してから登録する。
- 定時実行一覧: 登録した予定を表示する。ボタンで、止める・再開する・削除する。

実行そのものは schedule_runner.py が行う。
"""

from __future__ import annotations

from typing import Any

import chainlit as cl

from data_agent.agent import approval_policy
from data_agent.features import schedules
from data_agent.features.schedules import ScheduleError, ScheduleStore
from data_agent.safety.audit import audit
from data_agent.settings import get_security, get_settings
from data_agent.storage.chat_history import ChatHistoryDataLayer
from data_agent.ui.chat_commands import button_target, command, say

SCHEDULE = "定時実行"
SCHEDULE_LIST = "定時実行一覧"

COMMANDS = [
    command(SCHEDULE, "calendar-clock", "指定した日時に依頼を実行する（「分 時 日 月 曜日」と依頼文を書いて送る）"),
    command(SCHEDULE_LIST, "list-checks", "登録した定時実行を表示して、止める・再開する・削除する"),
]

_STATUS = {"ok": "成功", "error": "失敗", "running": "実行中"}


def _local(moment: Any) -> str:
    return schedules.format_time(schedules.to_local(moment, get_settings().schedule_timezone))


async def _register(history: ChatHistoryDataLayer, user_id: str, text: str) -> None:
    settings, limits = get_settings(), get_security()
    store = ScheduleStore(history)
    try:
        parsed = schedules.parse(
            text,
            timezone=settings.schedule_timezone,
            min_interval_minutes=limits.schedules.min_interval_minutes,
            max_prompt_chars=limits.limits.max_input_chars,
        )
    except ScheduleError as exc:
        await say(str(exc))
        return
    if await store.count_for_user(user_id) >= limits.schedules.max_per_user:
        await say(
            f"登録できる予定は {limits.schedules.max_per_user} 件までです。「{SCHEDULE_LIST}」から不要なものを削除してください。"
        )
        return

    model = cl.user_session.get("model") or settings.main_model
    times = "、".join(schedules.format_time(t) for t in schedules.upcoming(parsed.cron, settings.schedule_timezone))
    # 登録は承認制。内容を見せて、ボタンで確かめる
    answer = await cl.AskActionMessage(
        content=(
            "次の予定を登録しますか？\n\n"
            f"- **時刻:** `{parsed.cron}`（{settings.schedule_timezone}）\n"
            f"- **これからの実行:** {times}\n"
            f"- **依頼文:** {parsed.prompt}\n"
            f"- **モデル:** `{model}`\n\n"
            f"無人で実行するので、確認が必要な操作（{approval_policy().unattended_note()}）は、自動で拒否されます。"
        ),
        actions=[
            cl.Action(name="schedule_confirm", payload={"decision": "confirm"}, label="登録する"),
            cl.Action(name="schedule_cancel", payload={"decision": "cancel"}, label="やめる"),
        ],
        timeout=300,
    ).send()
    if not answer or answer["payload"].get("decision") != "confirm":
        await say("登録しませんでした。")
        return

    row = await store.create(
        user_id, parsed.cron, parsed.prompt, model, schedules.next_run(parsed.cron, settings.schedule_timezone)
    )
    audit("schedule_create", schedule_id=str(row["id"]), cron=parsed.cron)
    await say(
        f"登録しました。次の実行は {_local(row['nextRunAt'])} です。\n"
        f"結果は、左の一覧の「{schedules.GROUP_NAME}」のチャットとして残ります。"
        f"確認・停止・削除は、`/` から「{SCHEDULE_LIST}」を選んでください。"
    )


async def _list(history: ChatHistoryDataLayer, user_id: str, text: str) -> None:
    rows = await ScheduleStore(history).list_for_user(user_id)
    if not rows:
        await say(f"登録した予定はありません。`/` から「{SCHEDULE}」を選ぶと登録できます。")
        return
    lines, actions = [], []
    for number, row in enumerate(rows, start=1):
        state = f"次回 {_local(row['nextRunAt'])}" if row["enabled"] else "停止中"
        last = "まだ実行していません"
        if row["lastRunAt"]:
            last = f"前回 {_local(row['lastRunAt'])}（{_STATUS.get(row['lastStatus'], row['lastStatus'])}）"
            if row["lastThreadId"]:
                last += f" — [結果を開く](/thread/{row['lastThreadId']})"
        lines.append(f"{number}. `{row['cron']}` {row['prompt'][:60]}\n   - {state}、{last}")
        schedule_id = str(row["id"])
        actions.append(
            cl.Action(
                name="schedule_toggle",
                payload={"schedule_id": schedule_id, "enable": not row["enabled"]},
                label=f"{number} を止める" if row["enabled"] else f"{number} を再開する",
            )
        )
        actions.append(cl.Action(name="schedule_delete", payload={"schedule_id": schedule_id}, label=f"{number} を削除する"))
    await say("登録した定時実行です。\n\n" + "\n".join(lines), actions)


HANDLERS = {SCHEDULE: _register, SCHEDULE_LIST: _list}


def _target(action: cl.Action) -> tuple[ScheduleStore, str, str] | None:
    """ボタンの対象（ScheduleStore、利用者の ID、対象の ID）。形式は button_target が確かめる。"""
    found = button_target(action, "schedule_id")
    return (ScheduleStore(found[0]), found[1], found[2]) if found is not None else None


@cl.action_callback("schedule_toggle")
async def _toggle(action: cl.Action) -> None:
    target = _target(action)
    enable = bool(action.payload.get("enable"))
    row = None
    if target is not None:
        store, user_id, schedule_id = target
        mine = {str(r["id"]): r for r in await store.list_for_user(user_id)}
        if schedule_id in mine:
            # 再開するときは、止まっていた間の分を実行しないよう、次の実行時刻をいまから数え直す
            following = schedules.next_run(mine[schedule_id]["cron"], get_settings().schedule_timezone)
            row = await store.set_enabled(schedule_id, user_id, enable, following)
    if row is None:
        await say("その予定は見つかりませんでした。")
        return
    audit("schedule_enable" if enable else "schedule_disable", schedule_id=str(row["id"]))
    await say(f"予定を再開しました。次の実行は {_local(row['nextRunAt'])} です。" if enable else "予定を止めました。")
    await action.remove()


@cl.action_callback("schedule_delete")
async def _delete(action: cl.Action) -> None:
    target = _target(action)
    row = await target[0].delete(target[2], target[1]) if target is not None else None
    if row is None:
        await say("その予定は見つかりませんでした。")
        return
    audit("schedule_delete", schedule_id=str(row["id"]))
    await say(f"予定（`{row['cron']}` {row['prompt'][:40]}）を削除しました。")
    await action.remove()
