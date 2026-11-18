"""2つの点検（redteam: 指示の乗っ取り、factcheck: 誤情報）が共有する部分。

どちらの点検も、エージェントを agent.build_agent で作り、外につながない代わりの役（Web 検索・コードの実行・
データベース）に差し替えて、本物のモデルで1回ずつ実行する。ここには、そのための共通の部品を置く。
- 料金: 料金表、実行の前の目安（estimate）、使った分の記録と上限（Meter、BudgetExceeded）
- 記録: モデルの出力を集める記録係（Recorder）と、ツールの呼び出し（ToolCall）
- 代わりの役: Web 検索（SearchStub）、コードの実行（SandboxStub）、自己点検用の偽のモデル（Scripted、Call）
- 実行: 使い捨てのフォルダ（prepare）、ツール（stub_tools）、組み立て（build）、確認への答え（answer_actions）、
  1回の実行（drive）、すべての実行（run_all）
- 入口（__main__.py）: 引数（parse_args）、測る前の案内と確認（measure）、記録の保存（save）など

点検ごとに違うところ（仕込む文章、判定、結果の表）は、それぞれのパッケージに置く。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import sys
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult, LLMResult
from langchain_core.tools import BaseTool
from langgraph.types import Command
from pydantic import Field

from data_agent.safety import approvals
from data_agent.settings import PROJECT_ROOT, get_guard, get_security, get_settings
from data_agent.tools import code_execution, database, web_search

# ---------- 料金 ----------
# 100 万トークンあたりの料金（ドル）: 入力、出力、キャッシュからの読み取り。2026-11 時点の値で、目安として使う
PRICES: dict[str, tuple[float, float, float]] = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}
_UNKNOWN_PRICE = (8.00, 40.00, 0.20)  # 表にないモデルは、高いほうに見積もる
_CACHE_WRITE = 1.25  # キャッシュへの書き込みは、入力の料金の 1.25 倍
# 1回の実行で使うトークンの見当（実行の前に、料金の目安を出すためだけに使う）。
# 2026-11-15 に Opus 5.5 で測った値（21 回の合計で、キャッシュからの読み取り 50 万、キャッシュへの書き込み 4.3 万、
# 出力 1.5 万）を1回あたりに直し、余裕を見て 1.5 倍にしてある。入力のほとんどは、キャッシュからの読み取りになる
_TYPICAL_READ, _TYPICAL_WRITTEN, _TYPICAL_OUTPUT = 36_000, 3_000, 1_100

_AUTO_REJECT = "点検では利用者が確認できないので、この操作は実行しませんでした。"
_MAX_ROUNDS = 10  # 確認で止まった回数の上限
_STUB = "redteam_stub"  # 代わりの役が返した文章の印（モデルの出力として数えないため）。どちらの点検も、この印を使う
NO_RESULT = "該当する情報は見つかりませんでした。"
_NOT_RUN = "（点検用の環境です。コードは実行していません）"


class BudgetExceeded(Exception):
    """料金の上限に達した。以後の実行をやめる。"""


def price_of(model: str) -> tuple[float, float, float]:
    """モデルの料金。名前の後ろに日付などが付いていても、同じモデルとして扱う。"""
    return next((price for name, price in PRICES.items() if model.startswith(name)), _UNKNOWN_PRICE)


def estimate(model: str, runs: int) -> float:
    """実行の前の、料金の目安（ドル）。"""
    price_in, price_out, price_read = price_of(model)
    per_run = _TYPICAL_READ * price_read + _TYPICAL_WRITTEN * price_in * _CACHE_WRITE + _TYPICAL_OUTPUT * price_out
    return runs * per_run / 1_000_000


@dataclass
class Meter:
    """使ったトークンと、料金の目安。上限を超えたら、実行を止める。"""

    budget: float
    # モデル → [入力（キャッシュを使わなかった分）, キャッシュからの読み取り, キャッシュへの書き込み, 出力]
    tokens: dict[str, list[int]] = field(default_factory=dict)

    def add(self, model: str, usage: Mapping[str, Any] | None) -> None:
        if not usage:
            return
        details = usage.get("input_token_details") or {}
        read, written = int(details.get("cache_read") or 0), int(details.get("cache_creation") or 0)
        fresh = max(0, int(usage.get("input_tokens") or 0) - read - written)
        row = self.tokens.setdefault(model, [0, 0, 0, 0])
        for index, value in enumerate((fresh, read, written, int(usage.get("output_tokens") or 0))):
            row[index] += value

    @property
    def cost(self) -> float:
        total = 0.0
        for model, (fresh, read, written, output) in self.tokens.items():
            price_in, price_out, price_read = price_of(model)
            total += fresh * price_in + read * price_read + written * price_in * _CACHE_WRITE + output * price_out
        return total / 1_000_000

    def check(self) -> None:
        if self.cost >= self.budget:
            raise BudgetExceeded


# ---------- 記録 ----------
@dataclass(frozen=True)
class ToolCall:
    """モデルが出したツールの呼び出し（記録に残すもの）。"""

    name: str
    args: Mapping[str, Any]

    def text(self) -> str:
        """引数をつなげた文字列（中に、ある文字列があるかを調べるため）。"""
        return "\n".join(str(value) for value in self.args.values())


class Recorder(BaseCallbackHandler):
    """モデルの出力を、すべて記録する（サブエージェントの分も、あとで止められるツール呼び出しも）。

    trace は、点検ごとの記録（redteam は judge.Trace、factcheck は runner.Trace）。
    model_calls・texts・tool_calls・models の欄に書く。
    """

    run_inline = True
    raise_error = True  # 料金の上限に達したら、その場で実行を止める

    def __init__(self, trace: Any, meter: Meter) -> None:
        self._trace = trace
        self._meter = meter

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        for generations in response.generations:
            for generation in generations:
                message = getattr(generation, "message", None)
                if not isinstance(message, AIMessage) or message.response_metadata.get(_STUB):
                    continue
                self._trace.model_calls += 1
                if text := str(message.text):
                    self._trace.texts.append(text)
                self._trace.tool_calls += [ToolCall(c["name"], dict(c.get("args") or {})) for c in message.tool_calls]
                metadata = message.response_metadata
                model = str(metadata.get("model_name") or metadata.get("model") or "?")
                self._trace.models[model] = self._trace.models.get(model, 0) + 1
                self._meter.add(model, message.usage_metadata)
        self._meter.check()


# ---------- 外につながない、代わりの役 ----------
class SearchStub(BaseChatModel):
    """Web 検索の役の代わり（共通の部分）。依頼文を記録して、決まった返事を返す。外には何も送らない。

    返事は、本物（Claude API の Web 検索）と同じ形にする: 検索を1回行い、見つかったページが続き、まとめの文章が続く。
    点検ごとに決めるもの: まとめの文章（_summary）、見つかったページ（_pages。(題名, URL) の並び）、
    検索の id（TOOL_USE_ID）、名前（_llm_type）。
    """

    TOOL_USE_ID: ClassVar[str]
    trace: Any = None

    def _summary(self) -> str:
        raise NotImplementedError

    def _pages(self) -> list[tuple[str, str]]:
        raise NotImplementedError

    def _generate(self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
        self.trace.searches.append(str(messages[-1].text))
        text = self._summary()
        pages = [{"type": "web_search_result", "url": url, "title": title} for title, url in self._pages()]
        content = [
            {"type": "server_tool_use", "id": self.TOOL_USE_ID, "name": "web_search", "input": {"query": "…"}},
            {"type": "web_search_tool_result", "tool_use_id": self.TOOL_USE_ID, "content": pages},
            {"type": "text", "text": text},
        ]
        message = AIMessage(content=content, response_metadata={_STUB: True})
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(self, tools: Any, **kwargs: Any) -> SearchStub:
        return self


class SandboxStub:
    """コードを実行する環境の代わり。コードを記録するだけで、実行しない。"""

    def __init__(self, trace: Any) -> None:
        self._trace = trace

    async def run(self, code: str, files: list[tuple[str, bytes]], timeout: int) -> code_execution.SandboxResult:
        self._trace.code_runs.append(code)
        return code_execution.SandboxResult(exit_code=0, timed_out=False, output=_NOT_RUN, truncated=False, duration_ms=1)


@dataclass(frozen=True)
class Call:
    """ツールの呼び出し（自己点検で、偽のモデルに出させる）。"""

    name: str
    args: Mapping[str, Any]


class Scripted(BaseChatModel):
    """自己点検用の偽のモデル。決まった順にツールを呼び、最後に決まった返事を書く。"""

    script: list[Any] = Field(default_factory=list)  # Call の並び
    answer: str = ""

    @property
    def _llm_type(self) -> str:
        return "redteam-scripted"

    def _generate(self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
        humans = [index for index, message in enumerate(messages) if message.type == "human"]
        since = messages[humans[-1] + 1 :] if humans else []
        step = sum(1 for message in since if message.type == "ai")
        if step < len(self.script):
            call = self.script[step]
            tool_call = {"name": call.name, "args": dict(call.args), "id": f"call-{uuid.uuid4().hex[:8]}", "type": "tool_call"}
            message = AIMessage(content="", tool_calls=[tool_call])
        else:
            results = [str(m.text) for m in since if m.type == "tool"]
            message = AIMessage(content=self.answer.replace("{last}", results[-1] if results else ""))
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(self, tools: Any, **kwargs: Any) -> Scripted:
        return self


# ---------- 準備 ----------
def prepare(root: Path, files: Mapping[str, str]) -> dict[str, Path]:
    """使い捨てのフォルダを作る。返すのは、エージェントから見たパスの先頭 → 実際のフォルダ。

    長期メモとスキルは、本物の写しを置く。作業フォルダには、files（エージェントから見たパス → 中身）を置く。
    """
    roots = {"": root / "workspace", "/memories": root / "memories", "/skills": root / "skills"}
    for prefix, source in (("/memories", PROJECT_ROOT / "memories"), ("/skills", get_settings().skills_dir)):
        if source.is_dir():
            shutil.copytree(source, roots[prefix])
    for folder in roots.values():
        folder.mkdir(parents=True, exist_ok=True)
    for path, content in files.items():
        target = roots[""] / path.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return roots


def stub_tools(
    trace: Any,
    backend: Any,
    permissions: list[Any],
    database_stub: Callable[..., database.Database],
    search_stub: Callable[[], BaseChatModel],
    search_name: str,
) -> list[BaseTool]:
    """代わりの役を、本物と同じ名前・説明・引数のツールにする。設定で止めてあるツールは、付けない。

    database_stub は、データベースの代わりの役を作る関数（本物の Database の、つなぐ先のあとの引数を受け取る）。
    search_stub は、Web 検索の役の代わりを作る関数。search_name は、検索に使うモデルの名前の代わり。
    """
    settings, security, guard = get_settings(), get_security(), get_guard()
    timezone = settings.schedule_timezone
    tools: list[BaseTool] = []
    if security.code_execution.enabled:
        runner = code_execution.CodeRunner(
            SandboxStub(trace),  # type: ignore[arg-type]
            backend,
            permissions,
            security.code_execution,
            guard,
            timezone,
        )
        tools.append(code_execution.as_tool(runner, security.code_execution))
    if security.database.enabled:
        stub = database_stub(backend, permissions, security.database, guard, timezone)
        tools += database.as_tools(stub, security.database)
    if security.web_search.enabled:
        searcher = web_search.WebSearcher(search_stub(), search_name, security.web_search, guard, timezone)
        tools.append(web_search.as_tool(searcher, security.web_search))
    return tools


def build(
    roots: Mapping[str, Path],
    model: str,
    make_tools: Callable[[Any, list[Any]], list[BaseTool]],
    scripted: tuple[list[Any], str] | None = None,
) -> Any:
    """エージェントを組み立てる。make_tools は、ファイルの置き場所（backend）と権限の決まりから、ツールを作る関数。

    scripted は、自己点検のときの、偽のモデルに呼ばせるツール（Call の並び）と、最後の返事。None なら、本物のモデルを使う。
    """
    from data_agent import agent as agent_module  # ここで読み込む（判定と一覧だけを使うときは、要らない）

    backend = agent_module.backend_for(roots[""], roots["/memories"], roots["/skills"])
    tools = make_tools(backend, agent_module.PERMISSIONS)
    if scripted is None:
        return agent_module.build_agent(main_model=model, backend=backend, tools=tools)
    # 自己点検: モデルを作る関数を、偽のモデルを返すものに差し替えて組み立てる
    script, answer = scripted
    original = agent_module.build_model
    agent_module.build_model = lambda name, effort: Scripted(script=script, answer=answer)  # type: ignore[assignment]
    try:
        return agent_module.build_agent(main_model=model, backend=backend, tools=tools)
    finally:
        agent_module.build_model = original


# ---------- 実行 ----------
def answer_actions(
    actions: Sequence[Mapping[str, Any]],
    entry: str,
    trace: Any,
    withhold: Callable[[ToolCall], bool] = lambda call: False,
) -> list[dict[str, str]]:
    """確認で止まった操作への、人の役の答え。入口の操作（entry は、そのツールの名前）だけを1回承認し、ほかは拒否する。

    withhold に当てはまる操作は、入口の操作でも承認しない（redteam: 社外秘の値が入った操作）。
    答えは trace の approved と rejected にも残す。
    """
    decisions: list[dict[str, str]] = []
    for action in actions:
        call = ToolCall(str(action.get("name")), dict(action.get("args") or {}))
        held = withhold(call)
        if call.name == entry and not trace.approved and not held:
            trace.approved.append(call)
            decisions.append({"type": "approve"})
        else:
            trace.rejected.append(call)
            decisions.append({"type": "reject", "message": _AUTO_REJECT})
    return decisions


async def drive(
    graph: Any,
    request: str,
    mode: str,
    recorder: Recorder,
    answer: Callable[[Sequence[Mapping[str, Any]]], list[dict[str, str]]],
    trace: Any,
    timeout: int,
) -> list[Any]:
    """依頼を送り、確認で止まったら、人の役が答えて（answer）、最後の返事まで進める。返すのは、最後の会話。

    時間切れと、API のエラーなどは、trace.error に書いて、空の会話を返す（この回だけを集計から外して、次へ進む）。
    料金の上限に達したときは、そのまま止める（BudgetExceeded）。
    """
    try:
        async with asyncio.timeout(timeout):
            return await _until_done(graph, request, mode, recorder, answer)
    except BudgetExceeded:
        raise
    except TimeoutError:
        trace.error = f"時間切れ（{timeout} 秒）"
    except Exception as exc:  # API のエラーなど。この回だけを集計から外して、次へ進む
        trace.error = f"{type(exc).__name__}: {' '.join(str(exc).split())[:200]}"
    return []


async def _until_done(
    graph: Any,
    request: str,
    mode: str,
    recorder: Recorder,
    answer: Callable[[Sequence[Mapping[str, Any]]], list[dict[str, str]]],
) -> list[Any]:
    config: dict[str, Any] = {"configurable": {"thread_id": str(uuid.uuid4())}, "callbacks": [recorder]}
    payload: Any = {"messages": [{"role": "user", "content": request}], approvals.STATE_KEY: mode}
    for _ in range(_MAX_ROUNDS):
        await graph.ainvoke(payload, config)
        state = await graph.aget_state(config)
        if not state.interrupts:
            return state.values.get("messages", [])
        answers = {}
        for interrupt in state.interrupts:
            actions = interrupt.value.get("action_requests", [])
            answers[interrupt.id] = {"decisions": answer(actions)}
        payload = Command(resume=answers)
    raise RuntimeError("確認待ちが繰り返されたので、打ち切りました。")


async def run_all[Item, Done](
    items: Sequence[Item],
    run: Callable[[Item], Awaitable[Any]],
    to_result: Callable[[Item, int, Any], Done],
    *,
    trials: int,
    parallel: int,
    on_done: Callable[[Done], None] | None,
) -> tuple[list[Done], bool]:
    """すべての項目を、trials 回ずつ実行する。返すのは、結果と、料金の上限で途中でやめたかどうか。

    run は、1回実行して、記録を返す。to_result は、項目・何回目か・記録から、判定した結果を作る。
    """
    gate = asyncio.Semaphore(max(1, parallel))
    stopped = False

    async def one(item: Item, trial: int) -> Done | None:
        nonlocal stopped
        async with gate:
            if stopped:
                return None
            try:
                trace = await run(item)
            except BudgetExceeded:
                stopped = True
                return None
            result = to_result(item, trial, trace)
            if on_done:
                on_done(result)
            return result

    done = await asyncio.gather(*(one(item, trial) for trial in range(1, trials + 1) for item in items))
    return [result for result in done if result is not None], stopped


# ---------- 入口（__main__.py） ----------
def parse_args(
    argv: list[str] | None,
    *,
    prog: str,
    description: str,
    noun: str,
    budget: float,
    selftest_help: str,
    with_mode: bool = False,
) -> argparse.Namespace:
    """入口の引数を読む。noun は、実行する単位の呼び名（攻撃、質問）。budget は、料金の上限の既定（ドル）。

    with_mode なら、確認のしかた（--mode）も受け付ける（redteam だけ）。
    """
    parser = argparse.ArgumentParser(prog=prog, description=description)
    parser.add_argument("--model", help="測るモデル（省略すると、設定の MAIN_MODEL）。all で、画面で選べるモデルをすべて")
    if with_mode:
        parser.add_argument("--mode", choices=approvals.MODES, help="確認のしかた（省略すると、設定の既定）")
    parser.add_argument("--trials", type=int, default=1, help=f"{noun}1つあたりの実行の回数（既定は 1）")
    parser.add_argument("--only", help=f"実行する{noun}の名前（カンマ区切り）。省略すると、すべて")
    parser.add_argument(
        "--budget", type=float, default=budget, help=f"料金の上限（ドル。既定は {budget:g}）。達したら途中でやめる"
    )
    parser.add_argument("--parallel", type=int, default=3, help="同時に実行する数（既定は 3）")
    parser.add_argument("--timeout", type=int, default=300, help="1回の実行の時間の上限（秒。既定は 300）")
    parser.add_argument("--yes", action="store_true", help="実行の前の確認を省く")
    parser.add_argument("--list", action="store_true", help=f"{noun}の一覧を出して終わる（料金はかからない）")
    parser.add_argument("--selftest", action="store_true", help=selftest_help)
    args = parser.parse_args(argv)
    if args.trials < 1 or args.parallel < 1 or args.budget <= 0:
        parser.error("--trials と --parallel は 1 以上、--budget は 0 より大きい値にしてください")
    return args


# 記録に残す長さ。返事は、あとから全文を読んで確かめられるように、ほぼ切らない
# （800 文字で切っていたときは、長い返事の末尾にある注意書きを、記録から確かめられなかった）
ANSWER_LIMIT = 20_000
ARGS_LIMIT = 2_000


def clip(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


def yes(value: bool) -> str:
    return "はい" if value else "いいえ"


def trace_record(trace: Any) -> dict[str, Any]:
    """記録（Trace）のうち、2 つの点検で同じ欄。結果の JSON に入れる。"""
    return {
        "model_calls": trace.model_calls,
        "models": dict(trace.models),
        "tool_calls": [{"name": call.name, "args": clip(dict(call.args), ARGS_LIMIT)} for call in trace.tool_calls],
        "approved": [call.name for call in trace.approved],
        "rejected": [call.name for call in trace.rejected],
        "searches": [clip(question, 300) for question in trace.searches],
        "answer": clip(trace.answer, ANSWER_LIMIT),
    }


async def run_model(
    args: argparse.Namespace,
    items: Sequence[Any],
    run_all: Callable[..., Awaitable[tuple[list[Any], bool]]],
    status: Callable[[Any], str],
    *,
    model: str,
    mode: str,
    budget: float,
) -> tuple[list[Any], bool, Meter]:
    """モデルを 1 つ、すべての項目で測る。1 件終わるたびに「[ 3/12] <status>」と出す。結果・途中でやめたか・料金の記録を返す。"""
    meter = Meter(budget=budget)
    total = len(items) * args.trials
    done = 0

    def show(result: Any) -> None:
        nonlocal done
        done += 1
        print(f"[{done:>3}/{total}] {status(result)}", flush=True)

    print(f"\n----- {model} -----", flush=True)
    results, stopped = await run_all(
        items,
        model=model,
        mode=mode,
        meter=meter,
        trials=args.trials,
        parallel=args.parallel,
        timeout=args.timeout,
        on_done=show,
    )
    return results, stopped, meter


def report(
    args: argparse.Namespace,
    name: str,
    noun: str,
    *,
    model: str,
    mode: str,
    meter: Meter,
    stopped: bool,
    text: str,
    details: list[str],
    results: list[Any],
    record: Callable[[Any], dict[str, Any]],
    **summary: Any,
) -> None:
    """結果の表と、根拠・ほかのモデル・使ったトークンの行を出し、logs/<name>/ に保存する。summary は、点検ごとの集計の欄。"""
    lines = ["", text, *details]
    if args.only:
        lines += ["", f"（一部の{noun}だけを実行しました。全体の判定には、--only を付けずに実行してください）"]
    lines += other_models(results, model)
    lines += ["", "使ったトークン:", *usage_lines(meter)]
    print("\n".join(lines))
    save(
        {
            "date": datetime.now().isoformat(timespec="seconds"),
            "model": model,
            "fast_model": get_settings().fast_model,
            "mode": mode,
            "trials": args.trials,
            "stopped_by_budget": stopped,
            **summary,
            "cost_usd": round(meter.cost, 4),
            "tokens": meter.tokens,
            "report": text,
            "results": [record(result) for result in results],
        },
        name,
    )


def usage_lines(meter: Meter) -> list[str]:
    """使ったトークン（モデルごと）と、料金の目安の行。"""
    lines = []
    for model, (fresh, read, written, output) in sorted(meter.tokens.items()):
        lines.append(f"  {model}: 入力 {fresh + read + written:,}（うちキャッシュ {read:,}）、出力 {output:,} トークン")
    lines.append(
        f"料金の目安: 約 {meter.cost:.2f} ドル（料金表は 2026-11 時点。正確な額は、Anthropic のコンソールで確かめてください）"
    )
    return lines


def other_models(results: Iterable[Any], model: str) -> list[str]:
    """測ったモデルとは別のモデルが答えた呼び出しがあれば、それを知らせる行（前の空行を含む）。なければ空。"""
    others = sorted({name for result in results for name in result.trace.models if not name.startswith(model)})
    if not others:
        return []
    return ["", f"一部の呼び出しには、別のモデルが答えました: {'、'.join(others)}（断られたときの切り替えなど）"]


def save(report: dict[str, Any], name: str) -> str | None:
    """結果を logs/<name>/ に保存して、保存先を出す。書き込めない環境（コンテナの中など）では、保存しない。"""
    folder = get_settings().log_dir / name
    stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path = next(p for n in range(100) if not (p := folder / f"{stamp}{f'-{n}' if n else ''}.json").exists())
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        saved = None
    else:
        saved = str(path)
    print(f"\n詳しい記録: {saved}" if saved else "\n（詳しい記録は、保存できませんでした。上の結果を控えてください）")
    return saved


async def measure(
    args: argparse.Namespace,
    items: Sequence[Any],
    *,
    title: str,
    describe: Callable[[str, int], list[str]],
    measure_model: Callable[[str, str, float], Awaitable[tuple[bool | None, float]]],
) -> int:
    """測る前に案内を出して確かめ、モデルごとに測る。返すのは、終了コード（0: 合格、1: 不合格、2: 保留・中止）。

    describe は、案内のうち、点検ごとに違う行を返す（引数は、確認のしかたと、モデル1つあたりの実行の回数）。
    measure_model は、モデル・確認のしかた・残りの料金の上限を受け取り、合否（判定しないときは None）と、
    使った料金の目安を返す。--mode のない点検（factcheck）では、設定の既定の確認のしかたを使う。
    """
    settings = get_settings()
    # all: 画面の設定欄で選べるモデルを、すべて測る
    models = settings.selectable_models() if args.model == "all" else [args.model or settings.main_model]
    mode = getattr(args, "mode", None) or get_security().approvals.default_mode
    runs = len(items) * args.trials
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY が設定されていません（.env を確かめてください）。", file=sys.stderr)
        return 2
    print(title)
    print(f"  モデル      : {'、'.join(models)}（サブエージェントは {settings.fast_model}）")
    for line in describe(mode, runs):
        print(line)
    guess = sum(estimate(model, runs) for model in models)
    if not _confirmed(args, guess):
        print("実行しませんでした。")
        return 2

    verdicts, spent = await _measure_models(models, mode, args.budget, measure_model)
    if len(models) > 1:
        _print_summary(models, verdicts, spent)
    if any(passed is None for passed in verdicts):
        return 2
    return 0 if all(verdicts) else 1


def _confirmed(args: argparse.Namespace, guess: float) -> bool:
    """料金の目安を出して、実行してよいかを聞く（--yes なら聞かない）。"""
    print(
        f"  料金の目安  : {f'約 {guess:.0f} ドル' if guess >= 1 else '1 ドル未満'}。上限は {args.budget:g} ドルで、達したら途中でやめます"
    )
    print("  本物の作業フォルダ・長期メモ・Web・データベースには、触れません。")
    if args.yes:
        return True
    try:
        answer = input("実行しますか？ [y/N]: ")
    except EOFError:
        answer = ""
    return answer.strip().lower() in ("y", "yes")


async def _measure_models(
    models: Sequence[str],
    mode: str,
    budget: float,
    measure_model: Callable[[str, str, float], Awaitable[tuple[bool | None, float]]],
) -> tuple[list[bool | None], float]:
    """モデルを順に測る。料金の上限に達したら、残りのモデルは測らない（保留）。合否の一覧と、使った料金の目安を返す。"""
    verdicts: list[bool | None] = []
    spent = 0.0
    for model in models:
        if spent >= budget:
            print(f"\n料金の上限（{budget:g} ドル）に達したので、{model} は測りませんでした。")
            verdicts.append(None)
            continue
        passed, cost = await measure_model(model, mode, budget - spent)
        verdicts.append(passed)
        spent += cost
    return verdicts, spent


def _print_summary(models: Sequence[str], verdicts: Sequence[bool | None], spent: float) -> None:
    labels = {True: "合格", False: "不合格", None: "保留"}
    print("\n===== まとめ =====")
    for model, passed in zip(models, verdicts, strict=True):
        print(f"  {model}: {labels[passed]}")
    print(f"料金の目安の合計: 約 {spent:.2f} ドル")


def quiet_logs() -> None:
    """測っているあいだ、結果の表に混ざるログを止める。"""
    # 監査ログ（1件1行の JSON）は、結果の表に混ざるので出さない
    logging.getLogger("data_agent.audit").disabled = True
    # 料金の上限で止めたときに、LangChain が出す警告（上限に達したことは、結果の表に書く）
    logging.getLogger("langchain_core.callbacks.manager").setLevel(logging.ERROR)
