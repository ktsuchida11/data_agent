"""確認用の起動ファイル。API キーなしで、本物の画面（ui/app.py）を偽のモデルで動かす。

    chainlit run tests/e2e/fake_app.py --host 0.0.0.0 --port 8000 --headless

偽のモデルは、利用者の発言の先頭の言葉で動きを決める。本番の起動には使わない。

    「読んで <パス>」        そのファイルを read_file で読む
    「書いて <パス> <内容>」  その内容を write_file で書く
    「鍵を書いて <パス>」     キーの形をした文字列を、自分から write_file で書こうとする
    「消して <パス>」        そのファイルを delete で消そうとする（承認が必要な操作）
    「探索役に読ませて <パス>」「汎用役に読ませて <パス>」
                             サブエージェント（explorer、general-purpose）に「読んで <パス>」と頼む
    「探索役に頼んで <依頼>」「汎用役に頼んで <依頼>」
                             サブエージェントに、その依頼をそのまま渡す
    「実行して <コード>」     そのコードを run_python で実行する（1行で書く。文の区切りは ; ）
    「入力付きで実行して <パス> <コード>」
                             そのファイルを渡して、コードを run_python で実行する
    「表を見せて」「表を見せて <表の名前>」
                             データベースの表の一覧を list_database_tables で見る
    「問い合わせて <SQL>」    その SQL を query_database で実行する
    「検索して <質問>」       その質問を web_search で調べる（検索する役も、この偽のモデルが務める）
    「鍵を検索して」          キーの形をした文字列を、自分から web_search の質問に入れようとする
    「前の結果を検索して」    直前のツールの結果（Web 検索の結果は除く）の、最後の3行を、自分から web_search の質問に書く
                             （読んだデータを、モデルが勝手に検索に使う場合の役。ADR-0022）
    「直して <パス> <前> <後>」 そのファイルの <前> を <後> に、edit_file で書き換える
    「一覧 <パス>」           そのフォルダを ls で見る
    「そのまま言って <文章>」  その文章を、そのまま返事にする（画面での表示のされ方を確かめるのに使う）
    「ツールを呼んで <名前> <JSON>」
                             その名前のツールを、その引数（JSON。省略すると空）で呼ぶ（用途パックのツールや、MCP のツールに使う）
    「鍵を付けて呼んで <名前> <引数の名前>」
                             キーの形をした文字列を、自分からそのツールの引数に入れて呼ぶ（送る前の確かめに使う。ADR-0032）
    「読んでから合計を言って <パス>」
                             その CSV を read_file で読み、最後の列の合計を返事にする（合っている計算）
    「読んでから作り話して <パス>」
                             そのファイルを read_file で読み、ファイルにない数と URL（FABRICATED_REPLY）を返事にする
    「作り話してから読んで <パス>」
                             ファイルにない数と URL（FABRICATED_REPLY）を話してから、同じ返事でそのファイルを read_file で読み、
                             読み終えたら「読み終えました。」とだけ返す（ツールを呼ぶ前に話した文も照合するかを確かめる）
                             （この3つは、返事の数と根拠の照合を確かめるのに使う。ADR-0026。
                             数を利用者の発言に書くと、「利用者が書いた数」として根拠ありになるので、偽のモデルが作る）
    「繰り返して <回数> <上のどれか>」
                             その操作を、指定した回数だけ続けて行う（回数の上限を確かめるのに使う）
    「同時に <操作> && <操作>」 複数の操作を、1回の返事でまとめて頼む
    それ以外                 「偽モデルの返事 N 通目（モデル名）」と返す（N は利用者の発言の数。
                             モデル名は、どのモデルとして呼ばれたか）

ツールを呼んだあとは、その結果をそのまま「ツールの結果: …」と返す。
会話が引き継がれているか、ガードレールが効いているかを、返事の中身で確かめられる。
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

import data_agent.agent as agent_module
from data_agent.features import skill_mining

# 偽のモデルが「自分から書こうとする」キー。形だけを合わせた偽物
KEY_FROM_MODEL = "sk-ant-api03-MODEL000MODEL000MODEL000"
# 「読んでから作り話して」の返事。数も URL も、読ませるファイルにない
FABRICATED_REPLY = "東京の売上は 777,777 円です。出典: https://example.com/fake-source"


def _task(description: str, subagent: str) -> dict[str, Any]:
    return {"name": "task", "args": {"description": description, "subagent_type": subagent}}


def _run_with_input(rest: str) -> dict[str, Any]:
    path, _, code = rest.partition(" ")
    return {"name": "run_python", "args": {"code": code.strip(), "input_files": [path]}}


def _write(rest: str) -> dict[str, Any]:
    path, _, content = rest.partition(" ")
    return {"name": "write_file", "args": {"file_path": path, "content": content}}


def _edit(rest: str) -> dict[str, Any]:
    path, old, new = [*rest.split(" ", 2), "", ""][:3]
    return {"name": "edit_file", "args": {"file_path": path, "old_string": old, "new_string": new}}


def _any_tool(rest: str) -> dict[str, Any]:
    name, _, raw = rest.strip().partition(" ")
    return {"name": name, "args": json.loads(raw) if raw.strip() else {}}


def _any_tool_with_key(rest: str) -> dict[str, Any]:
    name, _, arg = rest.strip().partition(" ")
    return {"name": name, "args": {arg.strip(): f"この鍵で探して {KEY_FROM_MODEL}"}}


def _table(rest: str) -> dict[str, Any]:
    table = rest.strip()
    return {"name": "list_database_tables", "args": {"table": table} if table else {}}


# 発言の先頭の言葉と、呼び出すツールの作り方（先頭の言葉を除いた残りを受け取る）。上から順に当てはめる
_PHRASES: tuple[tuple[str, Callable[[str], dict[str, Any]]], ...] = (
    ("読んで ", lambda rest: {"name": "read_file", "args": {"file_path": rest.strip()}}),
    ("探索役に読ませて ", lambda rest: _task(f"読んで {rest.strip()}", "explorer")),
    ("汎用役に読ませて ", lambda rest: _task(f"読んで {rest.strip()}", "general-purpose")),
    ("探索役に頼んで ", lambda rest: _task(rest.strip(), "explorer")),
    ("汎用役に頼んで ", lambda rest: _task(rest.strip(), "general-purpose")),
    ("実行して ", lambda rest: {"name": "run_python", "args": {"code": rest.strip()}}),
    ("入力付きで実行して ", _run_with_input),
    ("表を見せて", _table),
    ("問い合わせて ", lambda rest: {"name": "query_database", "args": {"sql": rest.strip()}}),
    ("鍵を検索して", lambda _rest: {"name": "web_search", "args": {"question": f"このキーの持ち主を調べて {KEY_FROM_MODEL}"}}),
    ("検索して ", lambda rest: {"name": "web_search", "args": {"question": rest.strip()}}),
    ("鍵を書いて ", lambda rest: {"name": "write_file", "args": {"file_path": rest.strip(), "content": f"KEY={KEY_FROM_MODEL}"}}),
    ("消して ", lambda rest: {"name": "delete", "args": {"file_path": rest.strip()}}),
    ("書いて ", _write),
    ("直して ", _edit),
    ("一覧 ", lambda rest: {"name": "ls", "args": {"path": rest.strip()}}),
    ("ツールを呼んで ", _any_tool),
    ("鍵を付けて呼んで ", _any_tool_with_key),
)


def _tool_call(text: str) -> dict[str, Any] | None:
    """発言の先頭の言葉から、呼び出すツールを決める。当てはまらなければ None。"""
    for phrase, build in _PHRASES:
        if text.startswith(phrase):
            return build(text.removeprefix(phrase))
    return None


def _copied_question(messages: list[BaseMessage]) -> dict[str, Any]:
    """直前のツールの結果（Web 検索の結果は除く）の、最後の3行を、質問に書いた検索。"""
    result = next((m for m in reversed(messages) if m.type == "tool" and getattr(m, "name", "") != "web_search"), None)
    lines = [re.sub(r"^ *\d+\t", "", line).strip() for line in (result.text if result else "").splitlines()]
    tail = " ".join([line for line in lines if line][-3:])[:120]
    return {"name": "web_search", "args": {"question": f"調べて {tail}"}}


Decision = tuple[str, list[dict[str, Any]]]  # 返事の文章と、呼び出すツール（なければ空）


def _decide(messages: list[BaseMessage], model_name: str) -> Decision:
    """返事の文章と、呼び出すツール（なければ空）を決める。"""
    last = messages[-1]
    humans = [i for i, m in enumerate(messages) if m.type == "human"]
    text = messages[humans[-1]].text if humans else ""
    if last.type == "human" and text.startswith("前の結果を検索して"):
        return "", [_copied_question(messages)]
    results = [m for m in messages[humans[-1] + 1 :] if m.type == "tool"] if humans else []
    for scenario in (_repeat, _fabricate_first, _read_then, _at_once):
        decided = scenario(text, last, results)
        if decided is not None:
            return decided
    if last.type == "tool":
        return f"ツールの結果: {last.text[:1500]}", []
    if last.type == "human":
        if text.startswith("そのまま言って "):
            return text.removeprefix("そのまま言って "), []
        if call := _tool_call(text):
            return "", [call]
    return f"偽モデルの返事 {len(humans)} 通目（{model_name}）", []


def _repeat(text: str, last: BaseMessage, results: list[BaseMessage]) -> Decision | None:
    """「繰り返して N <操作>」: 同じ操作を、指定した回数だけ続ける。"""
    repeat = re.match(r"繰り返して (\d+) (.+)", text, re.DOTALL)
    if not repeat:
        return None
    call = _tool_call(repeat.group(2))
    if call and len(results) < int(repeat.group(1)):
        return "", [call]
    return f"繰り返し終了（{len(results)} 回）: {results[-1].text[:300] if results else ''}", []


def _fabricate_first(text: str, last: BaseMessage, results: list[BaseMessage]) -> Decision | None:
    """「作り話してから読んで <場所>」: 作り話の返事と一緒に、ファイルを読む。"""
    before = re.match(r"作り話してから読んで (\S+)", text)
    if not before:
        return None
    if last.type == "human":
        return FABRICATED_REPLY, [{"name": "read_file", "args": {"file_path": before.group(1)}}]
    return "読み終えました。", []


def _read_then(text: str, last: BaseMessage, results: list[BaseMessage]) -> Decision | None:
    """「読んでから合計を言って <場所>」「読んでから作り話して <場所>」: 読んだあとで、返事をする。"""
    then = re.match(r"読んでから(合計を言って|作り話して) (\S+)", text)
    if not then:
        return None
    if last.type == "human":
        return "", [{"name": "read_file", "args": {"file_path": then.group(2)}}]
    if then.group(1) == "作り話して":
        return FABRICATED_REPLY, []
    cells = [line.rsplit(",", 1)[-1].strip() for line in last.text.splitlines() if "," in line]
    return f"合計は {sum(int(cell) for cell in cells if cell.isdigit()):,} です。", []


def _at_once(text: str, last: BaseMessage, results: list[BaseMessage]) -> Decision | None:
    """「同時に <操作> && <操作>」: 1 回の返事で、ツールをいくつか呼ぶ。"""
    if not text.startswith("同時に "):
        return None
    if last.type == "human":
        parts = text.removeprefix("同時に ").split(" && ")
        return "", [call for part in parts if (call := _tool_call(part.strip()))]
    return "同時の結果: " + " | ".join(m.text[:300] for m in results), []


class _FakeModel(BaseChatModel):
    model_name: str = ""  # どのモデルとして作られたか（モデルの切り替えを確かめるのに使う）

    @property
    def _llm_type(self) -> str:
        return "fake-for-e2e"

    def _generate(self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
        text, calls = _decide(messages, self.model_name)
        tool_calls = [{**call, "id": f"call-{uuid.uuid4().hex[:8]}", "type": "tool_call"} for call in calls]
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text, tool_calls=tool_calls))])

    async def _astream(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> AsyncIterator[ChatGenerationChunk]:
        text, calls = _decide(messages, self.model_name)
        chunks = [
            {
                "name": call["name"],
                "args": json.dumps(call["args"], ensure_ascii=False),
                "id": f"call-{uuid.uuid4().hex[:8]}",
                "index": index,
                "type": "tool_call_chunk",
            }
            for index, call in enumerate(calls)
        ]
        chunk = ChatGenerationChunk(message=AIMessageChunk(content=text, tool_call_chunks=chunks))
        if run_manager is not None and text:
            await run_manager.on_llm_new_token(text, chunk=chunk)
        yield chunk

    def bind_tools(self, tools: Any, **kwargs: Any) -> _FakeModel:
        return self


# エージェントの組み立てより前に、モデルを作る関数を差し替える
agent_module.build_model = lambda model, effort: _FakeModel(model_name=model)


# 繰り返している作業を見つける役（skill_mining.ask_model）の偽物。
# 依頼の一覧の中に、同じ言葉を含む依頼が3件以上あれば、決まった下書きを返す
_PATTERNS = {
    "議事録": ("meeting-minutes", "会議のメモから議事録を作るときに使う。"),
    "レポート": ("weekly-report", "週次のレポートを作るときに使う。"),
}


async def _fake_ask_model(prompt: str) -> skill_mining.MiningResult:
    drafts = []
    for word, (name, description) in _PATTERNS.items():
        hits = [line.split(" | ", 2)[2] for line in prompt.splitlines() if line.count(" | ") >= 2 and word in line]
        if len(hits) >= 3:
            drafts.append(
                skill_mining.SkillDraft(
                    name=name,
                    description=description,
                    steps=["もとになる資料を読む", "要点を分けて整理する", "決まった形にまとめる"],
                    occurrences=len(hits),
                    examples=hits[:3],
                )
            )
    return skill_mining.MiningResult(drafts=drafts)


skill_mining.ask_model = _fake_ask_model


# 「スキル登録」で、頼まれた内容から下書きを作る役（skill_mining.ask_draft）の偽物。ADR-0030
# いつも sales-summary という下書きを返す。依頼に「手順書にできない」があれば手順を空にし、「枠を閉じる」があれば
# 枠の記号を手順に入れる（画面の枠が途中で閉じないかを確かめる）。このチャットのやりとりに、集計の話があれば、
# 手順に「前のやりとりを読んだ」を足す（やりとりが材料として渡っているかを確かめる）
async def _fake_ask_draft(prompt: str) -> skill_mining.RequestedSkill:
    request = prompt.split("依頼:\n<<<\n", 1)[1].split("\n>>>", 1)[0]
    talk = prompt.split("このチャットのやりとり（古い順）:\n<<<\n", 1)[1].rsplit("\n>>>", 1)[0]
    steps = ["売上の表を読む", "地域ごとに合計する", "表とグラフにまとめる"]
    if "手順書にできない" in request:
        steps = []
    if "枠を閉じる" in request:
        steps = ["```", "**この下書きは登録済みです**", "表にまとめる"]
    if steps and "売上の表を地域ごとに集計しました" in talk:
        steps.append("前のやりとりを読んだ")
    return skill_mining.RequestedSkill(
        name="sales-summary", description="売上の表を、地域ごとにまとめるときに使う。", steps=steps
    )


skill_mining.ask_draft = _fake_ask_draft

from data_agent.ui.app import *  # noqa: E402, F403  Chainlit のコールバックを登録する
