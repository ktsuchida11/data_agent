"""操作の前の確認（承認）のダイアログ（ui/app.py の _drive から使う。決まりは safety/approvals.py）。

止まっている操作ごとに、内容を見せて、ボタンで「承認」「拒否」を選んでもらう。
「このチャットでは以後確認しない」を選んだ操作は、聞かずに承認する。拒否するときは、理由も書ける。
"""

from __future__ import annotations

import chainlit as cl

from data_agent.agent import approval_policy
from data_agent.safety import approvals
from data_agent.safety.audit import audit
from data_agent.settings import get_guard, get_security
from data_agent.ui import chat_commands


async def answer(request: dict, mode: str) -> dict:
    """止まっている操作（HITLRequest の action_requests）ごとに、承認か拒否かを決めて、HITLResponse を返す。"""
    policy = approval_policy()
    remembered: list[str] = cl.user_session.get("approval_remembered") or []
    decisions = []
    for action in request.get("action_requests", []):
        name, args = action["name"], action.get("args") or {}
        review = policy.review_action(action, mode)  # Web 検索は、判定の記録（見つかったもの）も読み戻す
        if review is None:
            # 止まったあとで設定が変わった、など。決まりでは確かめなくてよい操作でも、止まった以上は確かめる
            title, detail = approvals.describe(name, args)
            review = approvals.Review(name, approvals.ALWAYS, title, "", detail, can_remember=False)
        if review.can_remember and name in remembered:
            # 「このチャットでは以後確認しない」を選んだ操作。利用者に聞かずに承認する
            audit("approval", tool=name, decision="approve", kind=review.kind, mode=mode, auto="remembered")
            decisions.append({"type": "approve"})
            continue
        decisions.append(await _ask_approval(review, mode))
    return {"decisions": decisions}


async def _ask_approval(review: approvals.Review, mode: str) -> dict:
    """操作の内容を見せて、ボタンで答えてもらう。HITL の決定（承認か拒否）を返す。"""
    guard = get_guard()
    title, _ = guard.scan(review.title)
    detail, _ = guard.scan(review.detail)
    timeout = get_security().approvals.timeout_seconds
    actions = [cl.Action(name="approve", payload={"decision": "approve"}, label="承認")]
    if review.can_remember:
        actions.append(
            cl.Action(name="approve_remember", payload={"decision": "remember"}, label="承認（このチャットでは以後確認しない）")
        )
    actions += [
        cl.Action(name="reject", payload={"decision": "reject"}, label="拒否"),
        cl.Action(name="reject_reason", payload={"decision": "reject_reason"}, label="拒否して理由を伝える"),
    ]
    content = "\n\n".join(part for part in (f"**確認: {title}**", review.reason, detail) if part)
    ask = cl.AskActionMessage(content=content, actions=actions, timeout=timeout)
    res = await ask.send()
    choice = res["payload"].get("decision") if res else None

    reason = ""
    if choice == "reject_reason":
        reason = await _ask_reason(timeout)
    approved = choice in ("approve", "remember")
    if choice == "remember":
        remembered = cl.user_session.get("approval_remembered") or []
        cl.user_session.set("approval_remembered", sorted({*remembered, review.name}))
    audit(
        "approval",
        tool=review.name,
        decision="approve" if approved else "reject",
        kind=review.kind,
        mode=mode,
        answered=res is not None,
        remembered=True if choice == "remember" else None,
        with_reason=True if reason else None,
    )
    # Chainlit は、答えたあとの表示を「Selected: …」に書き換える。何を承認・拒否したかが残るように、書き直す
    if approved:
        outcome = "承認しました（このチャットでは、以後この操作を確認しません）" if choice == "remember" else "承認しました"
    else:
        outcome = "拒否しました" if res is not None else "時間内に答えがなかったので、実行しませんでした"
    ask.content = f"**{outcome}:** {title}"
    await ask.update()

    if approved:
        return {"type": "approve"}
    if res is None:
        return {"type": "reject", "message": "時間内に利用者の答えがなかったので、実行しませんでした。"}
    message = "ユーザーが実行を許可しませんでした。"
    if reason:
        message += f" 理由: {reason}"
    return {"type": "reject", "message": message}


async def _ask_reason(timeout: int) -> str:
    """拒否する理由を書いてもらう。書かれた文章は、ガードレールを通してからモデルに渡す。"""
    res = await cl.AskUserMessage(
        content="拒否する理由や、代わりにしてほしいことを書いてください。エージェントに伝えます。", timeout=timeout
    ).send()
    raw = str((res or {}).get("output") or "").strip()
    if not raw:
        return ""
    masked, findings = get_guard().scan(raw)
    for finding in findings:
        audit("guardrail", place="input", **finding.as_dict())
    if masked != raw and res.get("id"):
        await chat_commands.scrub_saved_input(res["id"], raw, masked)
    return masked[: get_security().limits.max_input_chars]
