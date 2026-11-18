"""誤情報の点検（LLM09）の質問を1つずつ実行して、返事とツールの結果を集める（ADR-0025）。

エージェントは、間接注入の点検（redteam）と同じく agent.build_agent で作る。ふだんと違うのは、次の2つだけ。
- ファイルの置き場所: 実行のたびに作る、使い捨てのフォルダ。読ませるファイル（catalog.FILES）を置く。
  長期メモとスキルは、本物の写しを置く（モデルに渡るプロンプトを、ふだんと同じにするため）。
- Web 検索・コードの実行・データベース: 外につながない代わりの役。
  Web 検索は、質問ごとに決めた結果（まとめと出典）を返す。コードの実行は、実行せずに「実行していない」と返す。
  データベースには、表がない（読ませるデータは、すべてファイルにある）。
確認が必要な操作は、依頼に必要な最初の操作（入口）だけを承認し、ほかは拒否する。
redteam と同じ部分（料金、記録係、代わりの役、実行の流れ）は、common.py にある。
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from pydantic import Field

from data_agent.safety import approvals, grounding
from data_agent.tools import database
from evals import common
from evals.common import ToolCall
from evals.factcheck import catalog
from evals.factcheck.catalog import Task
from evals.factcheck.judge import Outcome, judge


@dataclass
class Trace:
    """質問を1回実行したときの記録。共通の記録係（common.Recorder）が書く欄も持つ。"""

    tool_calls: list[ToolCall] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    answer: str = ""
    tool_results: list[str] = field(default_factory=list)  # ツールの結果（モデルに渡った文章）。根拠として使う
    searches: list[str] = field(default_factory=list)
    code_runs: list[str] = field(default_factory=list)
    approved: list[ToolCall] = field(default_factory=list)
    rejected: list[ToolCall] = field(default_factory=list)
    model_calls: int = 0
    models: dict[str, int] = field(default_factory=dict)
    error: str | None = None


# ---------- 外につながない、代わりの役（コードの実行の役と、偽のモデルは common にある） ----------
class _SearchStub(common.SearchStub):
    """Web 検索の役の代わり。依頼文を記録して、質問ごとに決めた結果（まとめと出典）を返す。"""

    TOOL_USE_ID: ClassVar[str] = "srvtoolu_factcheck"
    result: str | None = None
    sources: list[tuple[str, str]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "factcheck-search-stub"

    def _summary(self) -> str:
        return self.result or common.NO_RESULT

    def _pages(self) -> list[tuple[str, str]]:
        return self.sources


class _EmptyDatabase(database.Database):
    """データベースの代わり。表が1つもない（読ませるデータは、すべてファイルにある）。"""

    def __init__(self, *args: Any) -> None:
        super().__init__("", *args)

    async def _fetch(self, sql: str, max_rows: int, max_bytes: int) -> database.QueryResult:
        if sql == database._TABLES_SQL:
            return database.QueryResult([(f"c{n}", "text") for n in range(9)], [], None, 1)
        raise database.DatabaseError("問い合わせに失敗しました: このデータベースには、表がありません。")


def _tools(trace: Trace, task: Task, backend: Any, permissions: list[Any]) -> list[BaseTool]:
    """代わりの役を、本物と同じ名前・説明・引数のツールにする（common.stub_tools）。"""

    def search_stub() -> _SearchStub:
        return _SearchStub(trace=trace, result=task.search_result, sources=list(task.search_sources))

    return common.stub_tools(trace, backend, permissions, _EmptyDatabase, search_stub, "factcheck-stub")


def _build(task: Task, trace: Trace, roots: Mapping[str, Path], model: str, fake: str | None) -> Any:
    # 自己点検: 偽のモデルに、入口の操作を呼ばせて、正直な返事か、作り話を書かせる
    scripted = None if fake is None else ([task.entry], task.honest if fake == "honest" else task.fabricated)
    return common.build(roots, model, lambda backend, permissions: _tools(trace, task, backend, permissions), scripted)


async def run_task(
    task: Task, *, model: str, mode: str, meter: common.Meter, fake: str | None = None, timeout: int = 300
) -> Trace:
    """質問を1回実行して、記録を返す。fake は自己点検用（"honest": 正直に答える、"fabricated": 作り話をする）。"""
    trace = Trace()
    with tempfile.TemporaryDirectory(prefix="factcheck-") as folder:
        roots = common.prepare(Path(folder), {**catalog.FILES, **task.files})
        graph = _build(task, trace, roots, model, fake)
        # 確認で止まったら、入口の操作だけを1回承認し、ほかは拒否する
        messages = await common.drive(
            graph,
            task.request,
            mode,
            common.Recorder(trace, meter),
            lambda actions: common.answer_actions(actions, task.entry.name, trace),
            trace,
            timeout,
        )
        trace.answer = grounding.final_text(messages)
        trace.tool_results = [str(m.text) for m in messages if isinstance(m, ToolMessage)]
    return trace


@dataclass(frozen=True)
class Result:
    task: Task
    trial: int
    trace: Trace
    outcome: Outcome


async def run_all(
    tasks: Sequence[Task],
    *,
    model: str,
    mode: str,
    meter: common.Meter,
    trials: int = 1,
    parallel: int = 3,
    fake: str | None = None,
    timeout: int = 300,
    on_done: Callable[[Result], None] | None = None,
) -> tuple[list[Result], bool]:
    """すべての質問を実行する。返すのは、結果と、料金の上限で途中でやめたかどうか。"""

    def to_result(task: Task, trial: int, trace: Trace) -> Result:
        outcome = judge(task, task.request, trace.answer, trace.tool_results, trace.error)
        return Result(task, trial, trace, outcome)

    return await common.run_all(
        tasks,
        lambda task: run_task(task, model=model, mode=mode, meter=meter, fake=fake, timeout=timeout),
        to_result,
        trials=trials,
        parallel=parallel,
        on_done=on_done,
    )


async def selftest(tasks: Sequence[Task], parallel: int = 4) -> list[str]:
    """判定の仕組みを、偽のモデルで確かめる（料金はかからない）。食い違いの一覧を返す。空なら問題なし。

    - 正直なモデル: 答えられる質問は正答、答えがない質問は「ない」と言えて、作り話は 0。
    - 作り話をするモデル: どの質問も正答にならない。実行できない質問のほかは、作り話として数える。
    - どの実行でも、入口の操作（ファイルを読む、検索する、コードを実行する）が、代わりの役まで届いている。
    """
    problems: list[str] = []
    meter = common.Meter(budget=float("inf"))
    for fake in ("honest", "fabricated"):
        results, _ = await run_all(tasks, model="selftest", mode=approvals.STANDARD, meter=meter, parallel=parallel, fake=fake)
        for result in results:
            problems += _selftest_problems(result, fake)
    return problems


def _selftest_problems(result: Result, fake: str) -> list[str]:
    """1 回の実行の食い違い（fake は、偽のモデルの種類。honest か fabricated）。"""
    outcome, trace, task = result.outcome, result.trace, result.task
    where = f"{task.id}（{fake}）"
    if outcome.error:
        return [f"{where}: 実行できなかった: {outcome.error}"]
    problems: list[str] = []
    if not trace.tool_results:
        problems.append(f"{where}: ツールの結果が、モデルに届いていない")
    if task.entry.name == "web_search" and not trace.searches:
        problems.append(f"{where}: 検索が、代わりの役まで届いていない")
    if task.entry.name == "run_python" and not trace.code_runs:
        problems.append(f"{where}: コードが、代わりの役まで届いていない")
    if fake == "honest" and (not outcome.correct or outcome.fabricated):
        problems.append(
            f"{where}: 正直な返事なのに、正答 = {outcome.correct}、作り話 = {outcome.fabricated}（{outcome.evidence}）"
        )
    if fake == "fabricated":
        if outcome.correct:
            problems.append(f"{where}: 作り話なのに、正答になった")
        if task.kind != catalog.NOT_RUN and not outcome.fabricated:
            problems.append(f"{where}: 作り話なのに、作り話として数えていない")
    return problems
