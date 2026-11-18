"""定時実行の本体。時刻が来た予定を、画面なしで実行する。

実行のしかたは2通りある。どちらも run_due() を呼ぶだけで、中身は同じ。
- アプリの中で、一定の間隔で時刻を見て実行する（scheduler_loop。手元や、アプリが1つだけの構成）。
- 外から起動する（`python -m data_agent.features.schedule_runner`。AWS では EventBridge から ECS のタスクを起動する）。

無人で動くので、確認が必要な操作（ファイルの削除、メモリへの書き込みなど。approvals.py）は自動で拒否する。
確認のしかたは、設定ファイルの既定のもの（approvals.default_mode）を使う。
実行した結果は、「定時実行」グループのチャットとして残る。利用者は左の一覧から開いて、続きを話せる。
データを読んだ回の返事は、画面と同じ決まりで根拠と照合し、確かめられない数や URL があれば、返事の下に注記を付ける
（ADR-0026。無人で動くので、読む人が後から気づけるように）。
"""

from __future__ import annotations

import asyncio
import functools
import logging
import uuid
from collections.abc import Callable
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command

from data_agent.features import schedules
from data_agent.features.schedules import ScheduleStore
from data_agent.safety import grounding
from data_agent.safety.audit import audit, bind
from data_agent.safety.guardrails import Guard
from data_agent.settings import PROJECT_ROOT, get_security
from data_agent.storage.chat_history import ChatHistoryDataLayer
from data_agent.tools import registry

logger = logging.getLogger(__name__)

_AUTO_REJECT = "定時実行では利用者が確認できないので、この操作は実行しませんでした。"
_MAX_ROUNDS = 10  # 承認待ちで止まった回数の上限
_PARALLEL = 2  # 同時に実行する予定の数
_AUTHOR = "データ分析エージェント"


class _Used:
    """実行の中で使ったツールと、その結果（サブエージェントの分も含む）。返事の照合（ADR-0026）に使う。"""

    def __init__(self) -> None:
        self.calls: list[grounding.Call] = []
        self.results: list[str] = []
        self._called: set[str] = set()
        self._answered: set[str] = set()

    def add(self, data: Any) -> None:
        """エージェントの途中経過（stream_mode="updates"）を1つ読む。

        確認のあとで再開すると、止まっていた呼び出しがもう一度流れてくるので、同じ ID のものは1回だけ数える。
        """
        for node, update in (data or {}).items():
            if node == "__interrupt__" or not isinstance(update, dict):
                continue
            messages = update.get("messages") or []
            for msg in messages if isinstance(messages, list) else []:
                if isinstance(msg, AIMessage):
                    for call in msg.tool_calls:
                        if call["id"] not in self._called:
                            self._called.add(call["id"])
                            self.calls.append((call["name"], dict(call.get("args") or {})))
                elif isinstance(msg, ToolMessage) and msg.tool_call_id not in self._answered:
                    self._answered.add(msg.tool_call_id)
                    self.results.append(msg.text)


def _note(answer: str, used: _Used, messages: list[Any], guard: Guard) -> str:
    """返事を根拠と照合して、確かめられないものがあれば、注記の文を返す（なければ空）。照合できなくても、実行は止めない。"""
    try:
        _, text = grounding.review(
            [answer],
            used.calls,
            messages=messages,
            results=used.results,
            memory_dir=PROJECT_ROOT / "memories",
            config=registry.grounding_config(get_security(), registry.current()),
            where="schedule",
            labels=registry.grounding_labels(registry.current()),
        )
    except Exception:
        logger.exception("定時実行の返事の照合に失敗しました")
        return ""
    return guard.scan(text)[0] if text else ""


async def run_schedule(
    row: dict[str, Any],
    *,
    history: ChatHistoryDataLayer,
    agent_for: Callable[[str], Any],
    guard: Guard,
    default_model: str,
    timezone: str,
    tracing_handler: Any | None = None,
) -> str:
    """予定を1つ実行して、結果の状態（ok か error）を返す。"""
    store = ScheduleStore(history)
    user_id = str(row["userId"])
    thread_id = str(uuid.uuid4())
    model = row["model"] or default_model
    user = await history.get_user_identifier(user_id) or "(unknown)"
    bind(user=user, thread_id=thread_id)
    audit("schedule_run_start", schedule_id=str(row["id"]), model=model)

    # 依頼文は登録のときにマスクしてあるが、設定が変わっていることもあるので、もう一度通す
    prompt, _ = guard.scan(row["prompt"])
    started = schedules.format_time(schedules.to_local(schedules.utc_now(), timezone))
    await history.create_thread(thread_id, user_id, f"{prompt[:40]}（{started}）", group=schedules.GROUP_NAME, model=model)
    await history.add_message(thread_id, "user_message", user, prompt)

    config: dict[str, Any] = {"configurable": {"thread_id": thread_id}}
    if tracing_handler is not None:
        config["callbacks"] = [tracing_handler]
        config["metadata"] = {
            "langfuse_session_id": thread_id,
            "langfuse_user_id": user,
            "langfuse_tags": ["data-agent", "scheduled", model],
        }

    status, rejected = "ok", 0
    used = _Used()
    try:
        agent = agent_for(model)
        payload: Any = {"messages": [{"role": "user", "content": prompt}]}
        for _ in range(_MAX_ROUNDS):
            # ツールの呼び出しと結果を、サブエージェントの分も集める（返事の照合の根拠にする）
            async for _namespace, data in agent.astream(payload, config, stream_mode="updates", subgraphs=True):
                used.add(data)
            state = await agent.aget_state(config)
            if not state.interrupts:
                break
            # 承認待ちで止まった操作を、すべて拒否して先へ進める
            answers = {}
            for interrupt in state.interrupts:
                actions = interrupt.value.get("action_requests", [])
                for action in actions:
                    rejected += 1
                    audit("approval", tool=action.get("name"), decision="reject", answered=False, auto=True)
                answers[interrupt.id] = {"decisions": [{"type": "reject", "message": _AUTO_REJECT} for _ in actions]}
            payload = Command(resume=answers)
        else:
            raise RuntimeError("確認待ちが繰り返されたので、打ち切りました。")
        messages = state.values.get("messages", [])
        answer, _ = guard.scan(grounding.final_text(messages))
        note = _note(answer, used, messages, guard)  # 返事がないときは、照合しない
        answer = answer or "（返事がありませんでした）"
        if rejected:
            answer += f"\n\n（確認が必要な操作を {rejected} 件、自動で拒否しました）"
        if note:
            answer += f"\n\n{note}"
    except Exception as exc:
        logger.exception("定時実行に失敗しました（予定 %s）", row["id"])
        status = "error"
        answer = f"実行中にエラーが起きました（{type(exc).__name__}）。詳しくはアプリのログを見てください。"

    await history.add_message(thread_id, "assistant_message", _AUTHOR, answer)
    await store.finish(row["id"], status, thread_id)
    audit("schedule_run_end", schedule_id=str(row["id"]), status=status, rejected=rejected)
    return status


async def run_due(**context: Any) -> int:
    """実行の時刻が来た予定を、すべて実行する。実行した数を返す。"""
    store = ScheduleStore(context["history"])
    rows = await store.claim_due(context["timezone"])
    if not rows:
        return 0
    limit = asyncio.Semaphore(_PARALLEL)

    async def one(row: dict[str, Any]) -> None:
        async with limit:
            await run_schedule(row, **context)

    await asyncio.gather(*(one(row) for row in rows))
    return len(rows)


async def scheduler_loop(poll_seconds: int, **context: Any) -> None:
    """アプリの中で、一定の間隔で run_due() を呼び続ける。"""
    while True:
        try:
            await run_due(**context)
        except asyncio.CancelledError:
            raise
        except Exception:
            # ここで止まると、以後の予定がすべて動かなくなる。記録して、次の回に進む
            logger.exception("定時実行の確認に失敗しました")
        await asyncio.sleep(poll_seconds)


async def _main() -> None:
    """外から起動されたとき。時刻が来た予定を実行して終わる。"""
    from data_agent.agent import build_agent
    from data_agent.settings import get_guard, get_settings
    from data_agent.storage import tracing
    from data_agent.storage.chat_history import build_data_layer, ensure_schema
    from data_agent.storage.persistence import open_checkpointer

    settings = get_settings()
    checkpointer, pool = await open_checkpointer(settings)
    if pool is None:
        raise SystemExit("定時実行には、会話の保存先（DB_HOST）の設定が必要です。")
    history = build_data_layer(settings, mask=lambda text: get_guard().scan(text)[0])
    agent_for = functools.cache(lambda model: build_agent(checkpointer, main_model=model))  # モデルごとに 1 回だけ作る

    try:
        await ensure_schema(pool, schedules.SCHEMA)
        await history.connect()
        count = await run_due(
            history=history,
            agent_for=agent_for,
            guard=get_guard(),
            default_model=settings.main_model,
            timezone=settings.schedule_timezone,
            tracing_handler=tracing.build_handler(get_guard()),
        )
        print(f"実行した予定: {count} 件")
    finally:
        tracing.shutdown()
        await history.close()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(_main())
