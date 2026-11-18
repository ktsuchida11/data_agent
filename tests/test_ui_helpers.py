"""画面のコマンドで共通に使う部品（ui/chat_commands.py）。Chainlit の画面の代わりに、偽の利用者と保存先を使う。

画面と同じ通信での確認は tests/e2e/ で行う。ここでは、部品そのものを手元で確かめる
（2026-11-16 に、利用者の ID を返す部品が自分自身を呼び続け、画面のコマンドがすべて止まったことがあった）。
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from data_agent.storage.chat_history import ChatHistoryDataLayer
from data_agent.ui import chat_commands

THREAD = str(uuid.uuid4())


class _History(ChatHistoryDataLayer):
    def __init__(self) -> None:  # データベースにはつながない
        pass


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """ログインした利用者（ID は user-1）と、過去のチャットの保存先。"""
    state = SimpleNamespace(user=SimpleNamespace(id="user-1", identifier="test"), history=_History())
    monkeypatch.setattr(chat_commands.cl, "context", SimpleNamespace(session=state))
    monkeypatch.setattr(chat_commands, "get_data_layer", lambda: state.history)
    return state


def _action(**payload: str) -> SimpleNamespace:
    return SimpleNamespace(payload=payload)


def test_current_user_id(session: SimpleNamespace) -> None:
    assert chat_commands.current_user_id() == "user-1"
    session.user = None  # ログインしていない
    assert chat_commands.current_user_id() is None


def test_get_history_only_returns_the_chat_store(session: SimpleNamespace) -> None:
    assert chat_commands.get_history() is session.history
    session.history = object()  # 過去のチャットを保存していない（Chainlit のほかの保存先）
    assert chat_commands.get_history() is None


def test_button_target_checks_the_payload(session: SimpleNamespace) -> None:
    assert chat_commands.button_target(_action(thread_id=THREAD), "thread_id") == (session.history, "user-1", THREAD)
    # ボタンの中身は利用者の側から送られてくる。形式が違うもの、キーが違うものは断る
    assert chat_commands.button_target(_action(thread_id="1; DROP TABLE"), "thread_id") is None
    assert chat_commands.button_target(_action(schedule_id=THREAD), "thread_id") is None
    session.user = None
    assert chat_commands.button_target(_action(thread_id=THREAD), "thread_id") is None
