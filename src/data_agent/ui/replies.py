"""1 回の依頼の、返事とツールの表示（ui/app.py の _drive から使う）。

- 返事は、少しずつ流して表示する（親エージェントの発言だけ）。確定するときにマスクする。
- ツールの呼び出しは、折りたたみの Step として表示する（サブエージェントの分も、名前を付けて出す）。
- コードが作ったファイルは、画像ならその場に、ほかはダウンロードできる形で出す。
- 最後に、返事の数と URL を根拠と照合し、確かめられないものに注記を付ける（ADR-0026）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import chainlit as cl
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from langgraph.types import Command

from data_agent.agent import tool_policies
from data_agent.safety import approvals, grounding
from data_agent.settings import PROJECT_ROOT, get_guard, get_security, get_settings
from data_agent.tools import code_execution, registry

_STEP_PREVIEW = 4_000  # Step に表示するツール入出力の上限（文字）
# その場に表示する画像の種類。SVG は中にスクリプトを書けるので、ダウンロードだけにする
_INLINE_IMAGES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

logger = logging.getLogger(__name__)


@dataclass
class Turn:
    """1回の依頼で使ったツールと、その結果、出した返事。返事の照合（ADR-0026）に使う。"""

    used: list[str] = field(default_factory=list)  # 使ったツールの名前（サブエージェントの分も含む）
    calls: list[grounding.Call] = field(default_factory=list)  # 使ったツールの名前と引数
    results: list[str] = field(default_factory=list)  # ツールの結果（サブエージェントの分も含む）
    replies: list[str] = field(default_factory=list)  # 出した返事の文（ツールを呼ぶ前に話した文も含む。マスクしたあと）
    answer: cl.Message | None = None  # 最後に出した返事（注記は、ここに付ける）
    steps: dict[str, cl.Step] = field(default_factory=dict)  # 結果を待っているツールの表示（呼び出しの ID → 表示）


async def stream(agent: Any, payload: dict | Command, config: dict, turn: Turn) -> None:
    """エージェントを動かして、途中経過と返事を画面に出す。使ったツール・その結果・出した返事を turn に書く。

    確認のあとで再開すると、止まっていた呼び出しがもう一度流れてくるので、turn.steps にある ID のものは、
    表示を作り直さない。
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
                await _open_steps(msg.tool_calls, namespace, turn)
            elif isinstance(msg, ToolMessage) and msg.tool_call_id in turn.steps:
                await _close_step(msg, turn.steps.pop(msg.tool_call_id), turn)

    await _mark_steps(turn, "（確認待ち）")  # 確認を待って止まった（結果は、再開したあとに入る）
    await bubble.close()


async def finish(agent: Any, config: dict, turn: Turn) -> None:
    """依頼が終わったとき。結果が届かなかった表示を閉じ、返事を照合する。"""
    await _mark_steps(turn, "（結果がありません）")
    turn.steps.clear()
    await _note_unverified(agent, config, turn)


async def _mark_steps(turn: Turn, text: str) -> None:
    for step in turn.steps.values():
        step.output = text
        await step.update()


async def _note_unverified(agent: Any, config: dict, turn: Turn) -> None:
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


class _Bubble:
    """いま書いている返事の吹き出し。確定させたら、返事として turn に記録し、次の吹き出しを用意する。"""

    def __init__(self, turn: Turn) -> None:
        self.turn = turn
        self.message = cl.Message(content="")

    async def write(self, text: str) -> None:
        await self.message.stream_token(text)

    async def close(self) -> None:
        """書いた分があれば確定させる。途中で流した文字はマスク前のものなので、確定するときにマスクする。"""
        if not self.message.content:
            return
        self.message.content, _ = get_guard().scan(self.message.content)
        await self.message.send()
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


async def _open_steps(calls: list[dict], namespace: tuple, turn: Turn) -> None:
    """ツールの呼び出しを表示し、turn に記録する。再開で同じ呼び出しがもう一度流れてきたら、作り直さない。"""
    prefix = "サブエージェント › " if namespace else ""
    for call in calls:
        if call["id"] in turn.steps:
            continue
        turn.used.append(call["name"])
        turn.calls.append((call["name"], dict(call.get("args") or {})))
        step = cl.Step(name=f"{prefix}{call['name']}", type="tool", show_input="json")
        args, _ = get_guard().scan(json.dumps(call["args"], ensure_ascii=False, indent=2))  # マスクしてから出す
        step.input = _preview(args)
        await step.send()
        turn.steps[call["id"]] = step


async def _close_step(msg: ToolMessage, step: cl.Step, turn: Turn) -> None:
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


def _preview(text: str) -> str:
    return text if len(text) <= _STEP_PREVIEW else text[:_STEP_PREVIEW] + "\n…（省略）"
