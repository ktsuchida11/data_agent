"""チャットのアーカイブとグループ分け。入力欄で `/` を打つと選べるコマンドで操作する。

Chainlit の画面には、アーカイブもグループ分けもないので、チャットの中のコマンドで補う。
- アーカイブ: 左の一覧から隠す。「アーカイブ一覧」で確認し、ボタンで一覧に戻せる。
- グループ: チャットにグループ名を付ける。一覧では名前の先頭に [グループ名] が出て、検索で絞り込める。

過去のチャットを保存しているとき（DB_HOST を設定したとき）だけ使える。
コマンドのメッセージは、エージェント（モデル）には渡さない。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import chainlit as cl
from chainlit.data import get_data_layer

from data_agent.safety.audit import audit
from data_agent.storage.chat_history import ChatHistoryDataLayer, display_name

ARCHIVE = "アーカイブ"
ARCHIVED_LIST = "アーカイブ一覧"
GROUP = "グループ"
UNGROUP = "グループ解除"

HINT = "`/` を入力すると、このチャットのアーカイブやグループ分け、定時実行の登録ができます。"

_GROUP_MAX = 40  # グループ名の上限（文字）
_LIST_MAX = 20  # 「アーカイブ一覧」に出す件数の上限
_UNTITLED = "無題の会話"
_NOT_FOUND = "このチャットはまだ保存されていないか、見つかりませんでした。"


def command(command_id: str, icon: str, description: str) -> dict[str, Any]:
    return {
        "id": command_id,
        "icon": icon,
        "description": description,
        "button": False,
        "persistent": False,
        "selected": False,
    }


COMMANDS = [
    command(ARCHIVE, "archive", "このチャットを左の一覧から隠す"),
    command(ARCHIVED_LIST, "archive-restore", "隠したチャットを表示して、一覧に戻す"),
    command(GROUP, "folder", "このチャットにグループ名を付ける（グループ名を書いて送る）"),
    command(UNGROUP, "folder-minus", "このチャットをグループから外す"),
]


# ---------- ほかの画面のファイル（app.py、schedule_commands.py、skill_commands.py）からも使う部品 ----------
def get_history() -> ChatHistoryDataLayer | None:
    """過去のチャットの保存先。保存していないとき（DB_HOST を設定していないとき）は None。"""
    layer = get_data_layer()
    return layer if isinstance(layer, ChatHistoryDataLayer) else None


def current_user_id() -> str | None:
    """ログインした利用者の ID（チャットや予定の持ち主を確かめるのに使う）。"""
    return getattr(cl.context.session.user, "id", None)


async def scrub_saved_input(step_id: str, raw: str, masked: str) -> None:
    """保存済みの入力を、マスクしたものに置き換える（過去のチャットを保存しているときだけ）。"""
    history = get_history()
    if history is None:
        return
    # 保存の手前でもマスクしている（app.py の _mask_text）。これは、念のための置き換え。
    # Chainlit は、利用者のメッセージとチャットの名前を裏で保存する。それが終わるのを待ってから置き換える
    await asyncio.sleep(1)
    await history.scrub_user_input(cl.user_session.get("thread_id"), step_id, raw, masked)


def button_target(action: cl.Action, key: str) -> tuple[ChatHistoryDataLayer, str, str] | None:
    """ボタンの対象（保存先、利用者の ID、対象の ID）。ボタンの中身は利用者の側から送られてくるので、形式を確かめる。"""
    history, user_id = get_history(), current_user_id()
    target_id = str(action.payload.get(key, ""))
    if history is None or user_id is None or not is_uuid(target_id):
        return None
    return history, user_id, target_id


def enabled() -> bool:
    return get_history() is not None


async def announce() -> None:
    """使えるコマンドを画面に知らせる。チャットを開いたときに呼ぶ。"""
    if enabled():
        await cl.context.emitter.set_commands(COMMANDS)


async def handle(message: cl.Message) -> bool:
    """コマンドのメッセージなら処理して True を返す。それ以外は False（エージェントに渡す）。"""
    handler = _HANDLERS.get(message.command or "")
    history = get_history()
    if handler is None or history is None:
        return False
    user_id = current_user_id()
    if user_id is None:
        await say("ログインした利用者を確認できなかったので、何もしませんでした。")
        return True
    await handler(history, user_id, message.content.strip())
    return True


async def say(content: str, actions: list[cl.Action] | None = None) -> None:
    await cl.Message(content=content, actions=actions or []).send()


async def _on_current_thread(update: Callable[[str], Awaitable[dict | None]]) -> dict | None:
    """いま開いているチャットを更新する。

    チャットの最初のメッセージがコマンドのときは、Chainlit がチャットを登録し終える前に
    ここへ来ることがある。少し待って、もう一度試す。
    """
    thread_id = cl.context.session.thread_id
    for _ in range(15):
        row = await update(thread_id)
        if row is not None:
            return row
        await asyncio.sleep(0.2)
    return None


async def _archive(history: ChatHistoryDataLayer, user_id: str, text: str) -> None:
    row = await _on_current_thread(lambda thread_id: history.set_archived(thread_id, user_id, True))
    if row is None:
        await say(_NOT_FOUND)
        return
    audit("chat_archive")
    await say(
        "このチャットをアーカイブしました。左の一覧には出なくなります"
        "（一覧の表示は、ページを読み込み直すと更新されます）。\n"
        f"戻すときは、`/` から「{ARCHIVED_LIST}」を選んでください。"
    )


async def _archived_list(history: ChatHistoryDataLayer, user_id: str, text: str) -> None:
    threads = await history.list_archived_threads(user_id, limit=_LIST_MAX)
    if not threads:
        await say("アーカイブしたチャットはありません。")
        return
    lines, actions = [], []
    for number, thread in enumerate(threads, start=1):
        name = thread["name"] or _UNTITLED
        lines.append(f"{number}. {name} — [開く](/thread/{thread['id']})")
        actions.append(
            cl.Action(
                name="unarchive",
                payload={"thread_id": thread["id"]},
                label=f"{number} を一覧に戻す",
                tooltip=name,
                icon="archive-restore",
            )
        )
    note = f"\n\n（新しい順に {_LIST_MAX} 件まで表示しています）" if len(threads) == _LIST_MAX else ""
    await say(
        "アーカイブしたチャットです。ボタンを押すと、左の一覧に戻します。\n\n" + "\n".join(lines) + note,
        actions,
    )


async def _group(history: ChatHistoryDataLayer, user_id: str, text: str) -> None:
    name = " ".join(text.split())  # 改行や連続した空白を、空白1つにまとめる
    if not name:
        await say(f"グループ名を書いて送ってください。例: 「{GROUP}」を選んで `案件A` と入力する。")
        return
    if len(name) > _GROUP_MAX:
        await say(f"グループ名は {_GROUP_MAX} 文字までにしてください。")
        return
    row = await _on_current_thread(lambda thread_id: history.set_group(thread_id, user_id, name))
    if row is None:
        await say(_NOT_FOUND)
        return
    audit("chat_group")
    await say(
        f"このチャットをグループ「{name}」に入れました。"
        f"左の一覧では名前の先頭に `[{name}]` が付き、検索欄にグループ名を入れると絞り込めます。"
    )


async def _ungroup(history: ChatHistoryDataLayer, user_id: str, text: str) -> None:
    row = await _on_current_thread(lambda thread_id: history.set_group(thread_id, user_id, None))
    if row is None:
        await say(_NOT_FOUND)
        return
    audit("chat_ungroup")
    await say("このチャットをグループから外しました。")


_HANDLERS: dict[str, Callable[[ChatHistoryDataLayer, str, str], Awaitable[None]]] = {
    ARCHIVE: _archive,
    ARCHIVED_LIST: _archived_list,
    GROUP: _group,
    UNGROUP: _ungroup,
}


def is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def register(commands: list[dict[str, Any]], handlers: dict[str, Any]) -> None:
    """ほかの機能のコマンド（定時実行など）を足す。アプリの読み込み時に呼ぶ。"""
    COMMANDS.extend(commands)
    _HANDLERS.update(handlers)


@cl.action_callback("unarchive")
async def _unarchive(action: cl.Action) -> None:
    """「アーカイブ一覧」のボタン。チャットを左の一覧に戻す。"""
    target = button_target(action, "thread_id")  # 本人のチャットだけを対象にする
    row = await target[0].set_archived(target[2], target[1], False) if target is not None else None
    user = getattr(cl.context.session.user, "identifier", None)
    if row is None:
        audit("chat_unarchive_refused", user=user)
        await say("そのチャットは見つかりませんでした。")
        return
    audit("chat_unarchive", user=user, thread_id=target[2])
    name = display_name(row["name"], row["groupName"]) or _UNTITLED
    await say(f"「{name}」を左の一覧に戻しました。")
    await action.remove()
