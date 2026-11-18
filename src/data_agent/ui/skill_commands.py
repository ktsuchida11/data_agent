"""スキル（手順書）のコマンドと、承認のボタン。

- スキル提案: 依頼の記録をその場で調べ、繰り返している作業があれば、スキルの下書きを見せる（ADR-0007）。
- スキル登録: 「どんな手順書にしたいか」を書いて送ると、その文と、このチャットのやりとりから、
  下書きを 1 つ作って見せる（ADR-0030）。先頭に「名前:」を書くと、その名前にする。
- どちらの下書きにも、登録する・見送るのボタンが付く。
- チャットを開いたとき: 記録がたまっていれば、裏で調べておく。承認待ちの提案があれば、一言知らせる。

下書きは skill_mining.py が作る。登録は、ここで利用者がボタンを押したときだけ行う。
"""

from __future__ import annotations

import asyncio
import logging

import chainlit as cl

from data_agent.features import skill_mining
from data_agent.features.skill_mining import SkillError, SkillStore
from data_agent.safety import approvals
from data_agent.safety.audit import audit
from data_agent.settings import get_guard, get_security, get_settings
from data_agent.storage.chat_history import ChatHistoryDataLayer
from data_agent.ui.chat_commands import button_target, command, current_user_id, get_history, say

logger = logging.getLogger(__name__)

PROPOSE = "スキル提案"
REQUEST = "スキル登録"

COMMANDS = [
    command(PROPOSE, "sparkles", "繰り返している作業を探して、スキル（手順書）の下書きを見せる"),
    command(
        REQUEST, "file-plus", "どんな手順書にしたいかを書いて送ると、スキルの下書きを作る（「名前: 説明」で名前も決められる）"
    ),
]

_CONVERSATION_MESSAGES = 20  # 下書きの材料にする、このチャットのやりとりの数（新しいものから）
_CONVERSATION_CHARS = 8_000
_MESSAGE_CHARS = 1_000

# 裏で動かしているタスク（スキルの候補探し）
_background: set[asyncio.Task[None]] = set()


async def _mine(history: ChatHistoryDataLayer, user_id: str) -> int:
    return await skill_mining.mine(
        user_id,
        store=SkillStore(history),
        skills_dir=get_settings().skills_dir,
        guard=get_guard(),
        limits=get_security().skill_mining,
    )


async def _show_one(proposal: dict) -> None:
    """提案を 1 つ、ボタン付きで見せる。中身はモデルが書いたものなので、枠や説明を書き足せない形で見せる。"""
    proposal_id = str(proposal["id"])
    examples = "\n".join(f"- {approvals.inline(example)}" for example in proposal["examples"]) or "- （例はありません）"
    if proposal["occurrences"]:
        head = f"**スキルの提案: {approvals.inline(proposal['name'])}**（似た依頼が {proposal['occurrences']} 回ありました）"
        source = f"当てはまった依頼の例:\n{examples}"
    else:
        head = f"**スキルの下書き: {approvals.inline(proposal['name'])}**（チャットで頼まれて作りました）"
        source = f"頼まれた内容:\n{examples}"
    await say(
        f"{head}\n\n{source}\n\n"
        f"登録する手順書の中身:\n{approvals.fenced(proposal['body'], 'markdown')}\n"
        "登録すると、新しいチャットと定時実行から、エージェントがこの手順書を使えるようになります。"
        f"直したいときは、見送ってから、書き方を変えて `/` の「{REQUEST}」で頼み直してください。",
        [
            cl.Action(name="skill_register", payload={"proposal_id": proposal_id}, label="登録する"),
            cl.Action(name="skill_dismiss", payload={"proposal_id": proposal_id}, label="見送る"),
        ],
    )


async def _show(history: ChatHistoryDataLayer, user_id: str) -> int:
    """承認待ちの提案を、ボタン付きで見せる。見せた数を返す。"""
    proposals = await SkillStore(history).pending(user_id)
    for proposal in proposals:
        await _show_one(proposal)
    return len(proposals)


async def _propose(history: ChatHistoryDataLayer, user_id: str, text: str) -> None:
    limits = get_security().skill_mining
    store = SkillStore(history)
    total, _, _ = await store.counts(user_id, limits.lookback_days)
    if total < limits.min_requests:
        await say(f"依頼の記録がまだ {total} 件です。{limits.min_requests} 件たまると、繰り返しを調べられます。")
        return
    try:
        found = await _mine(history, user_id)
    except Exception:
        logger.exception("スキルの検出に失敗しました")
        await say("繰り返している作業を調べる途中で、エラーが起きました。詳しくはアプリのログを見てください。")
        return
    audit("skill_mine", found=found, manual=True)
    if await _show(history, user_id) == 0:
        await say(f"繰り返している作業は見つかりませんでした（調べた依頼: {min(total, limits.max_requests)} 件）。")


async def _conversation(history: ChatHistoryDataLayer) -> list[str]:
    """このチャットのやりとり（利用者とエージェントの発言だけ。道具の結果は入れない）。古い順に、新しいものから上限まで。"""
    thread_id = cl.user_session.get("thread_id")
    if not thread_id:
        return []
    try:
        thread = await history.get_thread(thread_id)
    except Exception:
        logger.exception("チャットのやりとりを読めませんでした")
        return []
    lines = []
    for step in (thread or {}).get("steps", []):
        who = {"user_message": "利用者", "assistant_message": "エージェント"}.get(step.get("type", ""))
        text = " ".join(str(step.get("output") or "").split())[:_MESSAGE_CHARS]
        if who and text:
            lines.append(f"{who}: {text}")
    kept: list[str] = []
    total = 0
    for line in reversed(lines[-_CONVERSATION_MESSAGES:]):
        if total + len(line) > _CONVERSATION_CHARS:
            break
        kept.append(line)
        total += len(line)
    return kept[::-1]


async def _request(history: ChatHistoryDataLayer, user_id: str, text: str) -> None:
    """「スキル登録」: 頼まれた内容から下書きを作って見せる。登録は、ボタンを押したときだけ。"""
    try:
        proposal = await skill_mining.draft_from_request(
            user_id,
            text,
            await _conversation(history),
            store=SkillStore(history),
            skills_dir=get_settings().skills_dir,
            guard=get_guard(),
        )
    except SkillError as exc:
        await say(f"下書きを作れませんでした。{exc}")
        return
    except Exception:
        logger.exception("スキルの下書きに失敗しました")
        await say("スキルの下書きを作る途中で、エラーが起きました。詳しくはアプリのログを見てください。")
        return
    audit("skill_draft", skill=proposal["name"], source="chat")
    await _show_one(proposal)


HANDLERS = {PROPOSE: _propose, REQUEST: _request}


async def on_chat_start() -> None:
    """チャットを開いたとき。承認待ちの提案を知らせ、記録がたまっていれば裏で調べておく。"""
    history, user_id = get_history(), current_user_id()
    limits = get_security().skill_mining
    if history is None or user_id is None or not limits.enabled:
        return
    store = SkillStore(history)
    waiting = len(await store.pending(user_id))
    if waiting:
        await say(f"繰り返している作業が {waiting} 件見つかっています。`/` から「{PROPOSE}」を選ぶと、中身を見て登録できます。")
    if skill_mining.should_mine(*await store.counts(user_id, limits.lookback_days), limits):
        # モデルを呼ぶので時間がかかる。チャットを待たせないよう、裏で行う。結果は次に開いたときに知らせる
        task = asyncio.create_task(_mine_quietly(history, user_id))
        _background.add(task)  # 参照を持たないと、終わる前に片付けられることがある
        task.add_done_callback(_background.discard)


async def _mine_quietly(history: ChatHistoryDataLayer, user_id: str) -> None:
    try:
        found = await _mine(history, user_id)
        audit("skill_mine", found=found, manual=False)
    except Exception:
        logger.exception("スキルの検出に失敗しました")


def _target(action: cl.Action) -> tuple[SkillStore, str, str] | None:
    """ボタンの対象（SkillStore、利用者の ID、対象の ID）。形式は button_target が確かめる。"""
    found = button_target(action, "proposal_id")
    return (SkillStore(found[0]), found[1], found[2]) if found is not None else None


@cl.action_callback("skill_register")
async def _register(action: cl.Action) -> None:
    target = _target(action)
    proposal = await target[0].decide(target[2], target[1], "registered") if target is not None else None
    if proposal is None:
        await say("その提案は見つからないか、すでに登録・見送りが済んでいます。")
        return
    try:
        skill_mining.register_skill(get_settings().skills_dir, proposal["name"], proposal["body"])
    except SkillError as exc:
        await target[0].reopen(target[2])  # 書き出せなかったので、提案を承認待ちに戻す
        await say(f"登録できませんでした。{exc}")
        return
    audit("skill_register", skill=proposal["name"])
    await say(f"スキル `{proposal['name']}` を登録しました。新しいチャットから使われます。")
    await action.remove()


@cl.action_callback("skill_dismiss")
async def _dismiss(action: cl.Action) -> None:
    target = _target(action)
    proposal = await target[0].decide(target[2], target[1], "dismissed") if target is not None else None
    if proposal is None:
        await say("その提案は見つからないか、すでに登録・見送りが済んでいます。")
        return
    audit("skill_dismiss", skill=proposal["name"])
    await say(f"スキル `{proposal['name']}` は見送りました。同じ名前では、もう提案しません。")
    await action.remove()
