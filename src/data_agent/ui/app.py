"""Chainlit のチャット画面。

起動: uv run chainlit run src/data_agent/ui/app.py --host 127.0.0.1 --port 8000

- 最終回答はトークン単位でストリーミング表示する。
- ツール呼び出しは折りたたみの Step として表示する（サブエージェント内の呼び出しも名前付きで出す）。
- 確認が必要な操作（削除など。approvals.py）は、内容を見せて、ボタンで承認／拒否してから再開する。
- 確認のしかた（標準 / 毎回確認）は、設定欄でチャットごとに選べる。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
from typing import Any

import chainlit as cl
import chainlit.server
from chainlit.config import config as chainlit_config
from chainlit.data import get_data_layer
from chainlit.input_widget import Select
from chainlit.types import ThreadDict
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from langgraph.types import Command

from data_agent.agent import approval_policy, build_agent, tool_policies
from data_agent.features import schedule_runner, schedules, skill_mining
from data_agent.features.skill_mining import SkillStore
from data_agent.safety import approvals, grounding
from data_agent.safety.audit import audit, bind
from data_agent.safety.guardrails import masked_only
from data_agent.settings import PROJECT_ROOT, get_guard, get_pack, get_security, get_settings
from data_agent.storage import trace_retention, tracing
from data_agent.storage.chat_history import build_data_layer, ensure_schema
from data_agent.storage.persistence import open_checkpointer
from data_agent.tools import code_execution, mcp, registry
from data_agent.ui import chat_commands, schedule_commands, skill_commands
from data_agent.ui.connection_guard import ConnectionGuard, SecurityHeaders

# 定時実行とスキルの提案のコマンドを、アーカイブなどのコマンドと同じ入口（入力欄の `/`）に足す
chat_commands.register(schedule_commands.COMMANDS, schedule_commands.HANDLERS)
chat_commands.register(skill_commands.COMMANDS, skill_commands.HANDLERS)

_STEP_PREVIEW = 4_000  # Step に表示するツール入出力の上限（文字）
# その場に表示する画像の種類。SVG は中にスクリプトを書けるので、ダウンロードだけにする
_INLINE_IMAGES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

logger = logging.getLogger(__name__)


class _QuietRmtree:
    """shutil の代わり。rmtree だけ、消せなくてもエラーにしない。

    Chainlit は終了時に、一時フォルダ（.files）をフォルダごと消そうとする。コンテナでは、
    このフォルダは書き込み禁止の場所に重ねた一時領域なので、中身は消せてもフォルダ自体は消せず、
    止めるたびに "Application shutdown failed" と長いエラーが出ていた。
    Chainlit の中の shutil だけを、これに差し替える。
    """

    def __getattr__(self, name: str) -> Any:
        return getattr(shutil, name)

    @staticmethod
    def rmtree(path: Any, *args: Any, **kwargs: Any) -> None:
        shutil.rmtree(path, ignore_errors=True)


chainlit.server.shutil = _QuietRmtree()


def _origin_rejected(kind: str, origin: str) -> None:
    audit("origin_rejected", kind=kind, origin=origin[:200])


# 接続元の検査と、HTTPS でのクッキーの Secure（ADR-0027。connection_guard.py）。一覧は .chainlit/config.toml の allow_origins。
# SecurityHeaders より先に付けるので、その内側に入り、断った応答にもヘッダーが付く
_allowed_origins = list(chainlit_config.project.allow_origins or [])
if "*" in _allowed_origins:
    logger.warning("allow_origins に * があるので、接続元を検査しません（ADR-0027）")
chainlit.server.app.add_middleware(ConnectionGuard, allowed_origins=_allowed_origins, on_reject=_origin_rejected)
chainlit.server.app.add_middleware(SecurityHeaders)  # 画面に付ける HTTP ヘッダー（connection_guard.py）

# エージェントは起動時（_startup）以降に作る。会話の保存先（PostgreSQL）への接続は、
# サーバーのイベントループの中で開く必要があるため。
# モデルごとに1つずつ作って使い回す。会話は保存先にあるので、途中でモデルを替えても続きから話せる
_agents: dict[str, Any] = {}
_db_pool = None
_checkpointer = None
_tracing_handler = None  # Langfuse へのトレースの送信（設定したときだけ）
_scheduler_task = None  # 定時実行の見張り（会話を PostgreSQL に保存しているときだけ）
_retention_task = None  # Langfuse の古い記録を、1 日に 1 回消す（Langfuse に送っているときだけ。ADR-0031）


def _agent_for(model: str) -> Any:
    if model not in _agents:
        _agents[model] = build_agent(_checkpointer, main_model=model)
    return _agents[model]


def _current_model() -> str:
    """このチャットで使うモデル。画面の設定欄で選んでいなければ MAIN_MODEL。"""
    return cl.user_session.get("model") or get_settings().main_model


def _current_agent() -> Any:
    return _agent_for(_current_model())


def _current_mode() -> str:
    """このチャットの確認のしかた。設定欄で選んでいなければ、既定のもの。"""
    policy = approval_policy()
    if not get_security().approvals.allow_mode_change:
        return policy.default_mode
    return cl.user_session.get("approval_mode") or policy.default_mode


async def _delete_saved_conversation(thread_id: str) -> None:
    """画面でチャットを削除したときに、エージェント側に保存した会話も消す。"""
    if _checkpointer is not None:
        await _checkpointer.adelete_thread(thread_id)
    audit("chat_delete", thread_id=thread_id)


if get_settings().db_host:
    # 過去のチャットの一覧・再開・名前の変更・削除を有効にする（DB_HOST を設定したときだけ）
    @cl.data_layer
    def _data_layer():
        return build_data_layer(get_settings(), on_thread_deleted=_delete_saved_conversation, mask=_mask_text)


def _check_login_settings() -> None:
    """ログインに必要な設定を、起動時に確かめる。

    Chainlit は、CHAINLIT_AUTH_SECRET が空でも起動し、ログインしたときに初めてエラーになる。
    ひな形（example.env）をコピーしただけの状態で起きるので、足りない項目を名指しで知らせる。
    """
    names = ("CHAINLIT_AUTH_SECRET", "APP_USERNAME", "APP_PASSWORD_SHA256")
    missing = [name for name in names if not os.environ.get(name, "").strip()]
    if missing:
        raise RuntimeError(f".env の次の項目が空です: {'、'.join(missing)}。値の作り方は example.env に書いてあります。")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", os.environ["APP_PASSWORD_SHA256"].strip()):
        raise RuntimeError(
            "APP_PASSWORD_SHA256 が 64 文字の英数字ではありません。"
            "パスワードそのものではなく、example.env のコマンドで作った値を入れてください。"
        )
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        logger.warning("ANTHROPIC_API_KEY が空です。このままでは、モデルを呼び出したときにエラーになります。")


@cl.on_app_startup
async def _startup() -> None:
    global _db_pool, _checkpointer, _tracing_handler, _scheduler_task, _retention_task
    settings = get_settings()
    try:
        _check_login_settings()
        _checkpointer, _db_pool = await open_checkpointer()
        if _db_pool is not None:
            await ensure_schema(_db_pool, (*schedules.SCHEMA, *skill_mining.SCHEMA))
            await get_data_layer().connect()  # つながらなければ、ここで起動を止める
        # 用途パックの MCP サーバーにつなぎ、使うツールを読み込む（ADR-0033）。つながらなければ、ここで起動を止める
        await mcp.load(get_pack(), get_guard())
        _agent_for(settings.main_model)  # 既定のモデルで組み立てておく。設定に誤りがあれば、ここで止まる
        _tracing_handler = tracing.build_handler(get_guard())
        retention_days = get_security().tracing.retention_days
        if _tracing_handler is not None and retention_days > 0:
            _retention_task = asyncio.create_task(trace_retention.retention_loop(retention_days))
        if _db_pool is not None and settings.scheduler_enabled:
            # 定時実行。一定の間隔で、実行の時刻が来た予定がないかを見る
            _scheduler_task = asyncio.create_task(
                schedule_runner.scheduler_loop(
                    settings.scheduler_poll_seconds,
                    history=get_data_layer(),
                    agent_for=_agent_for,
                    guard=get_guard(),
                    default_model=settings.main_model,
                    timezone=settings.schedule_timezone,
                    tracing_handler=_tracing_handler,
                )
            )
    except Exception as exc:
        # Chainlit は起動時フックの例外を記録するだけで、起動を続けてしまう。
        # エージェントがないまま画面だけ動く状態にしないよう、ここでプロセスを止める
        logger.error("起動に失敗したので終了します: %s", exc)
        raise SystemExit(1) from exc


@cl.on_app_shutdown
async def _shutdown() -> None:
    for task in (_scheduler_task, _retention_task):
        if task is not None:
            task.cancel()
    tracing.shutdown()
    if _db_pool is not None:
        await _db_pool.close()


# ---------- 認証（127.0.0.1 で待ち受けていても、同じPC上の他プロセスからは開けるため） ----------
@cl.password_auth_callback
def auth(username: str, password: str) -> cl.User | None:
    expected_user = os.environ.get("APP_USERNAME", "")
    expected_hash = os.environ.get("APP_PASSWORD_SHA256", "")
    if not expected_user or not expected_hash:
        return None  # 未設定ならログインさせない
    actual_hash = hashlib.sha256(password.encode()).hexdigest()
    # 比較はバイト列で（str 同士だと非ASCIIで例外になる）。どちらも定数時間で比べる
    user_ok = hmac.compare_digest(username.encode(), expected_user.encode())
    hash_ok = hmac.compare_digest(actual_hash.encode(), expected_hash.lower().encode())
    if user_ok and hash_ok:
        audit("login", user=username, ok=True)
        return cl.User(identifier=username)
    # 失敗したときは、入力された名前をそのまま記録しない（パスワードを名前の欄に打ち間違えることがある）
    audit("login", user=username if user_ok else None, ok=False)
    return None


def _bind_audit() -> None:
    """以後の監査ログに、いまの利用者とチャットの ID を付ける。"""
    user = cl.context.session.user
    bind(user=getattr(user, "identifier", None), thread_id=cl.user_session.get("thread_id"))


@cl.on_chat_start
async def start() -> None:
    # Chainlit のチャットの ID を、そのままエージェントの会話の ID に使う。
    # 過去のチャットを開き直したとき（resume）に、同じ会話の続きになる
    cl.user_session.set("thread_id", cl.context.session.thread_id)
    _bind_audit()
    audit("chat_start")
    await _send_settings()
    await chat_commands.announce()
    greeting = get_pack().config.greeting  # 用途パック（packs/<名前>/pack.yaml）のあいさつ
    if chat_commands.enabled():
        greeting += "\n" + chat_commands.HINT
    await cl.Message(content=greeting).send()
    # 繰り返している作業の提案があれば知らせる。記録がたまっていれば、裏で調べておく
    await skill_commands.on_chat_start()


@cl.on_chat_resume
async def resume(thread: ThreadDict) -> None:
    """過去のチャットを開き直したとき。画面のメッセージは Chainlit が復元する。"""
    cl.user_session.set("thread_id", thread["id"])
    _bind_audit()
    audit("chat_resume")
    # このチャットで前に選んだモデルがあれば、それで続ける（選べるモデルから外れていたら、既定に戻す）
    history = chat_commands.get_history()
    if history is not None:
        saved = await history.get_model(thread["id"])
        if saved in get_settings().selectable_models():
            cl.user_session.set("model", saved)
            cl.user_session.set("model_saved", saved)
    # 確認のしかたは、会話の状態に入っている（前に選んだものを、そのまま使う）
    config = _config()
    state = await _current_agent().aget_state(config)
    if state.values.get(approvals.STATE_KEY) in approvals.MODES:
        cl.user_session.set("approval_mode", state.values[approvals.STATE_KEY])
    # 「以後確認しない」は、チャットを開き直したら選び直してもらう（Chainlit は、前の値を復元することがある）
    cl.user_session.set("approval_remembered", [])
    await _send_settings()
    await chat_commands.announce()
    # 確認待ちのまま閉じられていた場合は、もう一度確かめてから続ける
    await _drive(None, config)


# ---------- 画面の設定欄（モデルと、確認のしかた） ----------
async def _send_settings() -> None:
    """設定欄に、モデルと、確認のしかたの選択肢を出す。"""
    choices = get_settings().selectable_models()
    widgets: list[Any] = [
        Select(
            id="model",
            label="モデル",
            values=choices,
            initial_index=choices.index(_current_model()),
            description="このチャットで使うモデルです。途中で替えても、会話は続きから話せます。",
        )
    ]
    if get_security().approvals.allow_mode_change:
        widgets.append(
            Select(
                id="approval_mode",
                label="確認",
                items={approvals.MODE_LABELS[mode]: mode for mode in approvals.MODES},
                initial_value=_current_mode(),
                description=(
                    "エージェントが操作を実行する前に、どこまで確かめるかです。"
                    "ファイルの削除と、長期メモへの書き込みは、どちらを選んでも必ず確かめます。"
                ),
            )
        )
    await cl.ChatSettings(widgets).send()


@cl.on_settings_update
async def settings_update(settings: dict) -> None:
    _bind_audit()
    # 画面から送られてきた値は、選べるものの中にあるかを必ず確かめる
    model = settings.get("model")
    if model is not None and model != _current_model():
        if model not in get_settings().selectable_models():
            audit("model_change_refused")
            await cl.Message(content="そのモデルは選べません。").send()
        else:
            cl.user_session.set("model", model)
            audit("model_change", model=model)
            await _save_model()
            await cl.Message(content=f"モデルを `{model}` に切り替えました。次の返事から使います。").send()

    mode = settings.get("approval_mode")
    if mode is not None and mode != _current_mode():
        if mode not in approvals.MODES or not get_security().approvals.allow_mode_change:
            audit("approval_mode_refused")
            await cl.Message(content="その確認のしかたは選べません。").send()
        else:
            cl.user_session.set("approval_mode", mode)
            cl.user_session.set("approval_remembered", [])  # 「以後確認しない」は、選び直してもらう
            audit("approval_mode_change", mode=mode)
            await cl.Message(
                content=f"確認のしかたを「{approvals.MODE_LABELS[mode]}」に切り替えました。次の依頼から使います。"
            ).send()


async def _save_model() -> None:
    """選んだモデルをチャットに覚えさせる（過去のチャットを保存しているときだけ）。

    最初のメッセージを送る前は、チャットがまだ保存されていないので覚えられない。
    その場合は、メッセージを処理したあとにもう一度呼ばれたときに覚える。
    """
    model = cl.user_session.get("model")
    history, user_id = chat_commands.get_history(), chat_commands.current_user_id()
    if not model or cl.user_session.get("model_saved") == model:
        return
    if history is None or user_id is None:
        return
    if await history.set_model(cl.user_session.get("thread_id"), user_id, model) is not None:
        cl.user_session.set("model_saved", model)


@cl.on_message
async def on_message(message: cl.Message) -> None:
    _bind_audit()
    limit = get_security().limits.max_input_chars
    if len(message.content) > limit:
        audit("request_rejected", reason="too_long", chars=len(message.content))
        await cl.Message(content=f"入力が長すぎます（上限 {limit} 文字）。分けて送ってください。").send()
        return

    # ガードレール（入口）。マスクしたものを、モデルにも、画面と保存先にも使う
    raw = message.content
    masked, findings = get_guard().scan(raw)
    audit("request", chars=len(raw), command=message.command, model=_current_model())
    for finding in findings:
        audit("guardrail", place="input", **finding.as_dict())
    scrubbed = masked != raw
    if scrubbed:
        message.content = masked
        await message.update()
        # Chainlit は「処理の記録」（on_message）にも入力の文章を残す。そちらもマスクしたものにする
        if (run := cl.context.current_run) is not None:
            run.input = masked
        labels = get_guard().labels
        kinds = "、".join(f"{labels.get(f.kind, f.kind)} {f.count}件" for f in masked_only(findings))
        await cl.Message(
            content=f"入力に含まれていた情報をマスクしました（{kinds}）。モデルには、マスクしたあとの文章を渡します。"
        ).send()

    try:
        # アーカイブやグループ分けのコマンドは、ここで処理して終える（エージェントには渡さない）
        if await chat_commands.handle(message):
            return
        # 確認のしかたは、会話の状態に入れて渡す。承認を待って止まったあとも、同じ決まりで判定される
        payload = {"messages": [{"role": "user", "content": masked}], approvals.STATE_KEY: _current_mode()}
        tools = await _drive(payload, _config())
        await _log_request(masked, tools)
    finally:
        if scrubbed:
            await _scrub_saved_input(message.id, raw, masked)
        await _save_model()


def _mask_text(text: str) -> str:
    """保存する文章をマスクする（過去のチャットの保存先に、マスクする前の文章を残さない）。"""
    return get_guard().scan(text)[0]


async def _scrub_saved_input(step_id: str, raw: str, masked: str) -> None:
    """保存済みの入力を、マスクしたものに置き換える（過去のチャットを保存しているときだけ）。"""
    history = chat_commands.get_history()
    if history is None:
        return
    # 保存の手前でもマスクしている（_mask_text）。これは、念のための置き換え。
    # Chainlit は、利用者のメッセージとチャットの名前を裏で保存する。それが終わるのを待ってから置き換える
    await asyncio.sleep(1)
    await history.scrub_user_input(cl.user_session.get("thread_id"), step_id, raw, masked)


def _config() -> dict:
    thread_id = cl.user_session.get("thread_id")
    config: dict[str, Any] = {"configurable": {"thread_id": thread_id}}
    if _tracing_handler is not None:
        # Langfuse へのトレース。利用者とチャットごとにまとめて見られるようにする
        user = getattr(cl.context.session.user, "identifier", None)
        config["callbacks"] = [_tracing_handler]
        config["metadata"] = {
            "langfuse_session_id": thread_id,
            "langfuse_tags": ["data-agent", _current_model()],
            **({"langfuse_user_id": user} if user else {}),
        }
    return config


class _Turn:
    """1回の依頼で使ったツールと、その結果、出した返事。返事の照合（ADR-0026）に使う。

    このファイルでは @dataclass を使わない。Chainlit は、このファイルを実行し終えてから sys.modules に
    登録するので、実行の途中で @dataclass が自分のモジュールを探すと、見つからずに起動が止まる
    （tests/e2e/check_boot.py で確かめる）。
    """

    __slots__ = ("answer", "calls", "replies", "results", "used")

    def __init__(self) -> None:
        self.used: list[str] = []  # 使ったツールの名前（サブエージェントの分も含む）
        self.calls: list[grounding.Call] = []  # 使ったツールの名前と引数
        self.results: list[str] = []  # ツールの結果（サブエージェントの分も含む）
        self.replies: list[str] = []  # 出した返事の文（ツールを呼ぶ前に話した文も含む。マスクしたあとのもの）
        self.answer: cl.Message | None = None  # 最後に出した返事（注記は、ここに付ける）


async def _drive(payload: dict | Command | None, config: dict) -> list[str]:
    """確認待ちがなくなるまで「実行 → 確認 → 再開」を繰り返す。使ったツールの名前を返す。

    payload が None のときは実行せず、確認待ちが残っているかを見るところから始める。
    """
    agent = _current_agent()  # 途中で設定欄のモデルを替えられても、この依頼は同じエージェントで最後まで進める
    turn = _Turn()
    steps: dict[str, cl.Step] = {}  # 結果を待っているツールの表示。確認をはさんでも、同じ表示を使い続ける
    while True:
        if payload is not None:
            await _run(agent, payload, config, steps, turn)
        state = await agent.aget_state(config)
        if not state.interrupts:
            break
        mode = approval_policy().mode_of(state.values)
        answers = {it.id: await _answer(it.value, mode) for it in state.interrupts}
        # 答えを待っている間に、別の画面（開き直した画面など）で先に答えが出ていたら、こちらの答えは使わない
        # （こちらが時間切れで「拒否」を返すと、すでに承認されて終わった操作に、あとから答えることになる）
        pending = {it.id for it in (await agent.aget_state(config)).interrupts}
        if not set(answers) <= pending:
            audit("approval_stale")
            await cl.Message(content="この確認には、別の画面ですでに答えが出ていました。こちらの答えは使っていません。").send()
            break
        payload = Command(resume=answers)
    for step in steps.values():  # 結果が届かなかった表示を閉じる
        step.output = "（結果がありません）"
        await step.update()
    await _note_unverified(agent, config, turn)
    return turn.used


async def _note_unverified(agent: Any, config: dict, turn: _Turn) -> None:
    """データを読んだ回の返事の数と URL を、根拠と照合する。確かめられないものがあれば、返事の下に注記を出す（ADR-0026）。

    照合するのは、その回に出した返事のすべて（ツールを呼ぶ前に話した文も含む）。注記は、最後の返事の下に1つだけ出す。
    根拠は、このチャットの利用者の発言とツールの結果（サブエージェントの分も含む）と、長期メモ。返事は止めない。
    注記は画面と保存した会話にだけ付け、モデルに渡る会話には入れない（次の返事に持ち込まないため）。
    """
    answer = turn.answer
    if answer is None:
        return
    try:
        state = await agent.aget_state(config)
        _, text = grounding.review(
            turn.replies,
            turn.calls,
            messages=state.values.get("messages", []),
            results=turn.results,
            memory_dir=PROJECT_ROOT / "memories",
            config=registry.grounding_config(get_security(), tool_policies()),
            where="chat",
            labels=registry.grounding_labels(tool_policies()),
        )
        if not text:
            return
        text, _ = get_guard().scan(text)
        answer.content = f"{answer.content}\n\n{text}"
        await answer.update()
    except Exception:
        # 照合できなくても、会話は止めない
        logger.exception("返事の照合に失敗しました")


async def _log_request(text: str, tools: list[str]) -> None:
    """依頼を記録する。繰り返している作業を見つけて、スキルの下書きを作る材料になる。"""
    history, user_id = chat_commands.get_history(), chat_commands.current_user_id()
    if history is None or user_id is None:
        return
    try:
        await SkillStore(history).log_request(user_id, cl.user_session.get("thread_id"), text, sorted(set(tools)))
    except Exception:
        # 記録できなくても、会話は続ける
        logger.exception("依頼の記録に失敗しました")


async def _run(agent: Any, payload: dict | Command, config: dict, steps: dict[str, cl.Step], turn: _Turn) -> None:
    """エージェントを動かして、途中経過と返事を画面に出す。使ったツール・その結果・出した返事を turn に書く。

    steps は、結果を待っているツールの表示（ツール呼び出しの ID → 表示）。確認のあとで再開すると、
    止まっていた呼び出しがもう一度流れてくるので、同じ ID のものは、表示を作り直さない。
    """
    bubble = _Bubble(turn)
    async for namespace, mode, data in agent.astream(payload, config, stream_mode=["messages", "updates"], subgraphs=True):
        if mode == "messages":
            text = _answer_text(data[0], namespace)
            if text:
                await bubble.write(text)
            continue
        for msg in _updated_messages(data):
            if isinstance(msg, AIMessage) and msg.tool_calls:
                # ツール呼び出しの前に話していた内容は確定させ、Step の後に新しい吹き出しを始める
                await bubble.close()
                await _open_steps(msg.tool_calls, namespace, steps, turn)
            elif isinstance(msg, ToolMessage) and msg.tool_call_id in steps:
                await _close_step(msg, steps.pop(msg.tool_call_id), turn)

    for step in steps.values():  # 確認を待って止まった（結果は、再開したあとに入る）
        step.output = "（確認待ち）"
        await step.update()
    await bubble.close()


class _Bubble:
    """いま書いている返事の吹き出し。確定させたら、返事として turn に記録し、次の吹き出しを用意する。"""

    __slots__ = ("message", "turn")

    def __init__(self, turn: _Turn) -> None:
        self.turn = turn
        self.message = cl.Message(content="")

    async def write(self, text: str) -> None:
        await self.message.stream_token(text)

    async def close(self) -> None:
        """書いた分があれば確定させる（マスクしてから送る）。何も書いていなければ、何もしない。"""
        if not self.message.content:
            return
        await _send_answer(self.message)
        self.turn.replies.append(self.message.content)
        self.turn.answer = self.message
        self.message = cl.Message(content="")


def _answer_text(chunk: Any, namespace: tuple) -> str:
    """返事として画面に流す文字。流すのは親エージェントの発言だけ（サブエージェントの途中経過は Step で見せる）。"""
    if namespace or not isinstance(chunk, AIMessage) or not chunk.text:
        return ""
    if isinstance(chunk, AIMessageChunk):
        return chunk.text
    # モデルを呼ばずに作られた返事（回数の上限で止めたときの知らせ）。少しずつではなく、まとめて届く
    return "" if chunk.tool_calls else chunk.text


def _updated_messages(data: dict) -> list[Any]:
    """updates の流れから、ノードが足したメッセージを、届いた順に取り出す（確認の中断は除く）。"""
    found: list[Any] = []
    for node, update in data.items():
        if node == "__interrupt__" or not isinstance(update, dict):
            continue
        messages = update.get("messages") or []
        if isinstance(messages, list):
            found.extend(messages)
    return found


async def _open_steps(calls: list[dict], namespace: tuple, steps: dict[str, cl.Step], turn: _Turn) -> None:
    """ツールの呼び出しを表示し、turn に記録する。再開で同じ呼び出しがもう一度流れてきたら、作り直さない。"""
    for call in calls:
        if call["id"] in steps:
            continue
        turn.used.append(call["name"])
        turn.calls.append((call["name"], dict(call.get("args") or {})))
        steps[call["id"]] = await _open_step(call, namespace)


async def _close_step(msg: ToolMessage, step: cl.Step, turn: _Turn) -> None:
    """ツールの結果を表示に書き、turn に記録する。コードが作ったファイルは、画面に出す。"""
    turn.results.append(msg.text)
    step.output = _preview(msg.text)
    step.is_error = msg.status == "error"
    await step.update()
    if msg.name == code_execution.TOOL_NAME:
        await _send_files(code_execution.artifact_files(msg.artifact))


async def _send_files(files: list[dict]) -> None:
    """コードが作ったファイルを画面に出す。画像はその場に表示し、ほかはダウンロードできるようにする。"""
    root = get_settings().workspace_dir.resolve()
    elements: list[Any] = []
    shown: list[str] = []
    for item in files:
        path = (root / item["path"].lstrip("/")).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            continue
        kind = cl.Image if path.suffix.lower() in _INLINE_IMAGES else cl.File
        elements.append(kind(name=path.name, path=str(path), display="inline"))
        shown.append(item["path"])
    if not elements:
        return
    listing = "\n".join(f"- {approvals.inline(path)}" for path in shown)  # 名前の ` や改行で、説明を書き足させない
    try:
        await cl.Message(content=f"作成したファイル（作業フォルダの中の場所）:\n{listing}", elements=elements).send()
    except Exception:
        # 画面に出せなくても、ファイルは作業フォルダに保存済み。会話は止めない
        logger.exception("作成したファイルを画面に出せませんでした")


async def _send_answer(answer: cl.Message) -> None:
    """返事を確定させる。途中経過として流した文字はマスク前のものなので、確定するときにマスクする。"""
    answer.content, _ = get_guard().scan(answer.content)
    await answer.send()


def _masked_args(args: dict) -> str:
    """ツールの引数を、画面に出す形にする（マスクして、長すぎる分は省く）。"""
    text, _ = get_guard().scan(json.dumps(args, ensure_ascii=False, indent=2))
    return _preview(text)


async def _open_step(call: dict, namespace: tuple) -> cl.Step:
    prefix = "サブエージェント › " if namespace else ""
    step = cl.Step(name=f"{prefix}{call['name']}", type="tool", show_input="json")
    step.input = _masked_args(call["args"])
    await step.send()
    return step


async def _answer(request: dict, mode: str) -> dict:
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
        await _scrub_saved_input(res["id"], raw, masked)
    return masked[: get_security().limits.max_input_chars]


def _preview(text: str) -> str:
    return text if len(text) <= _STEP_PREVIEW else text[:_STEP_PREVIEW] + "\n…（省略）"
