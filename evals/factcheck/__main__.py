"""`python -m evals.factcheck` の入口。使い方は、パッケージの説明（__init__.py）を参照。"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from evals import common
from evals.factcheck import catalog, judge, runner

_RESULT_LIMIT = 4_000


def _parse(argv: list[str] | None) -> argparse.Namespace:
    return common.parse_args(
        argv,
        prog="python -m evals.factcheck",
        description="誤情報（根拠のない数や出典、答えがないのに答えること）を、本物のモデルで測る。API の料金がかかる。",
        noun="質問",
        budget=5.0,
        selftest_help="判定の仕組みを、偽のモデルで確かめる（料金はかからない）",
    )


def _select(only: str | None) -> tuple[catalog.Task, ...]:
    if not only:
        return catalog.TASKS
    names = [name.strip() for name in only.split(",") if name.strip()]
    unknown = [name for name in names if name not in catalog.TASK_IDS]
    if unknown:
        raise ValueError(f"知らない質問の名前です: {'、'.join(unknown)}（--list で一覧を出せます）")
    return tuple(task for task in catalog.TASKS if task.id in names)


def _list(tasks: tuple[catalog.Task, ...]) -> None:
    for task in tasks:
        print(f"{task.id:<16} {catalog.KIND_LABELS[task.kind]:<6}　{task.note}　— {task.request.splitlines()[0]}")
    print(f"\n{len(tasks)} 問")


def _status(result: runner.Result) -> str:
    outcome = result.outcome
    if outcome.error:
        status = f"実行できなかった（{outcome.error}）"
    elif outcome.kind == catalog.ANSWER:
        status = f"正答: {common.yes(outcome.correct)}　作り話: {common.yes(outcome.fabricated)}"
    else:
        status = f"「ない」と言えた: {common.yes(outcome.admitted)}　作り話: {common.yes(outcome.fabricated)}"
    return f"{result.task.id:<16} {status}"


def _record(result: runner.Result) -> dict[str, Any]:
    outcome = result.outcome
    return {
        "task": result.task.id,
        "kind": result.task.kind,
        "trial": result.trial,
        "correct": outcome.correct,
        "admitted": outcome.admitted,
        "fabricated": outcome.fabricated,
        "evidence": list(outcome.evidence),
        "notes": list(outcome.notes),
        "error": outcome.error,
        **common.trace_record(result.trace),
        "tool_results": [common.clip(text, _RESULT_LIMIT) for text in result.trace.tool_results],  # 判定の根拠にしたもの
    }


def _render(summary: judge.Summary, results: list[runner.Result], model: str, stopped: bool) -> str:
    t = judge.THRESHOLDS
    lines = [f"===== 誤情報の点検の結果（{model}） =====", "", f"{'質問':<16} {'種類':<8} 結果"]
    for result in sorted(results, key=lambda r: (catalog.TASK_IDS.index(r.task.id), r.trial)):
        outcome = result.outcome
        if outcome.error:
            verdict = "実行できなかった"
        else:
            verdict = ("正しい" if outcome.correct else "正しくない") + ("・作り話あり" if outcome.fabricated else "")
        lines.append(f"{result.task.id:<16} {catalog.KIND_LABELS[result.task.kind]:<8} {verdict}")
    lines += [
        "",
        f"正答率（答えられる質問）       : {summary.answer_correct}/{summary.answer_total}（{summary.accuracy:.0%}）　基準（案）: {t['accuracy']:.0%} 以上",
        f"「ない」と言えた（作り話なし）    : {summary.absent_admitted}/{summary.absent_total}（{summary.admit_rate:.0%}）　基準（案）: {t['admit_rate']:.0%} 以上",
        f"作り話の割合（すべての質問）   : {summary.fabricated}/{summary.total}（{summary.fabrication_rate:.0%}）　基準（案）: {t['fabrication_rate']:.0%} 以下",
    ]
    if summary.errors:
        lines.append(f"実行できなかった回（集計から外した）: {summary.errors}")
    if stopped:
        lines.append("判定: 保留（料金の上限で、途中でやめました）")
    else:
        lines.append(f"判定: {'合格' if judge.passed(summary) else '不合格'}（基準は案です。ADR-0025）")
    return "\n".join(lines)


async def _measure_model(
    args: argparse.Namespace, tasks: tuple[catalog.Task, ...], model: str, mode: str, budget: float
) -> tuple[bool | None, float]:
    """モデルを1つ測って、結果を出す。返すのは、合否（判定しないときは None）と、使った料金の目安。"""
    results, stopped, meter = await common.run_model(args, tasks, runner.run_all, _status, model=model, mode=mode, budget=budget)
    summary = judge.summarize(result.outcome for result in results)
    text = _render(summary, results, model, stopped)
    passed = None if stopped else judge.passed(summary)
    details = []
    wrong = [f"  {r.task.id}: {line}" for r in results if not r.outcome.correct for line in r.outcome.evidence]
    if wrong:
        details += ["", "正しくなかった実行の根拠:", *wrong]
    notes = [f"  {r.task.id}: {line}" for r in results for line in r.outcome.notes]
    if notes:
        details += ["", "作り話に数えなかったもの（記録の返事を読んで確かめてください）:", *notes]
    common.report(
        args,
        "factcheck",
        "質問",
        model=model,
        mode=mode,
        meter=meter,
        stopped=stopped,
        text=text,
        details=details,
        results=results,
        record=_record,
        accuracy=summary.accuracy,
        admit_rate=summary.admit_rate,
        fabrication_rate=summary.fabrication_rate,
        errors=summary.errors,
        thresholds=judge.THRESHOLDS,
        passed=passed,
    )
    return passed, meter.cost


async def _measure(args: argparse.Namespace, tasks: tuple[catalog.Task, ...]) -> int:
    def describe(mode: str, runs: int) -> list[str]:
        return [
            f"  質問        : {len(tasks)} 問 × {args.trials} 回 = モデル1つにつき {runs} 回の実行（同時に {args.parallel} つ）"
        ]

    return await common.measure(
        args,
        tasks,
        title="誤情報（作り話）の点検",
        describe=describe,
        measure_model=lambda model, mode, budget: _measure_model(args, tasks, model, mode, budget),
    )


async def _selftest(tasks: tuple[catalog.Task, ...]) -> int:
    print(f"判定の仕組みを、偽のモデルで確かめます（{len(tasks)} 問。料金はかかりません）…", flush=True)
    problems = await runner.selftest(tasks)
    if problems:
        print(f"食い違いが {len(problems)} 件ありました:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("問題ありません。")
    print(f"  正直に答えるモデル   : 正答・「ない」と言えた {len(tasks)}/{len(tasks)}、作り話 0")
    print(
        f"  作り話をするモデル   : 正答 0/{len(tasks)}、作り話として数えた {sum(t.kind != catalog.NOT_RUN for t in tasks)}/{len(tasks)}"
        "（実行できない質問は、「実行したふり」で数える）"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    try:
        tasks = _select(args.only)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.list:
        _list(tasks)
        return 0
    common.quiet_logs()
    return asyncio.run(_selftest(tasks) if args.selftest else _measure(args, tasks))


if __name__ == "__main__":
    sys.exit(main())
