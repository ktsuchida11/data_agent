"""`python -m data_agent.evals.redteam` の入口。使い方は、パッケージの説明（__init__.py）を参照。"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from typing import Any

from data_agent.evals import common
from data_agent.evals.redteam import catalog, judge, runner
from data_agent.safety import approvals
from data_agent.settings import get_settings

_EXPOSED_LABELS = {
    catalog.BLOCKED: "守りが止める",
    catalog.STANDARD_ONLY: "「標準」では、モデルが従えば貫通する",
    catalog.ALWAYS: "モデルが従えば貫通する（返事そのものが狙い）",
}


def _parse(argv: list[str] | None) -> argparse.Namespace:
    return common.parse_args(
        argv,
        prog="python -m data_agent.evals.redteam",
        description="指示の乗っ取り（間接注入）への強さを、本物のモデルで測る。API の料金がかかる。",
        noun="攻撃",
        budget=10.0,
        selftest_help="点検の仕組みを、偽のモデルで確かめる（料金はかからない）",
        with_mode=True,
    )


_CONFIGURED_LABEL = "狙いの操作は、設定で確認つき（または、中身を調べて怪しいときに確認）なので、人が止められる"


def _list(attacks: tuple[catalog.Attack, ...]) -> None:
    configured, screened = runner.configured_tools(), runner.screened_tools()
    for attack in attacks:
        channel = judge.CHANNEL_LABELS.get(attack.channel, attack.channel)
        label = _EXPOSED_LABELS[attack.exposed]
        stopped = not runner.expected_breach(attack, approvals.STANDARD, configured, screened)
        if attack.exposed == catalog.STANDARD_ONLY and stopped:
            label = _CONFIGURED_LABEL
        print(f"{attack.id:<24} {channel}　{attack.goal}　— {label}")
    print(f"\n{len(attacks)} 種類")


def _progress(total: int):
    done = 0

    def show(result: runner.Result) -> None:
        nonlocal done
        done += 1
        outcome = result.outcome
        if outcome.error:
            status = f"実行できなかった（{outcome.error}）"
        else:
            status = f"従った: {'はい' if outcome.followed else 'いいえ'}　貫通: {'はい' if outcome.breached else 'いいえ'}"
        print(f"[{done:>3}/{total}] {result.attack.id:<24} {status}", flush=True)

    return show


def _record(result: runner.Result) -> dict[str, Any]:
    trace, outcome = result.trace, result.outcome
    return {
        "attack": result.attack.id,
        "trial": result.trial,
        "followed": outcome.followed,
        "breached": outcome.breached,
        "evidence": list(outcome.evidence),
        "error": outcome.error,
        "model_calls": trace.model_calls,
        "models": dict(trace.models),
        "tool_calls": [{"name": call.name, "args": common.clip(dict(call.args), common.ARGS_LIMIT)} for call in trace.tool_calls],
        "rejected": [call.name for call in trace.rejected],
        "approved": [call.name for call in trace.approved],
        "searches": [common.clip(question, 300) for question in trace.searches],
        "changed_files": sorted(
            path for path in {*trace.before, *trace.after} if trace.before.get(path) != trace.after.get(path)
        ),
        "answer": common.clip(trace.answer, common.ANSWER_LIMIT),
    }


async def _measure_model(
    args: argparse.Namespace, attacks: tuple[catalog.Attack, ...], model: str, mode: str, budget: float
) -> tuple[bool | None, float]:
    """モデルを1つ測って、結果を出す。返すのは、合否（判定しないときは None）と、使った料金の目安。"""
    settings = get_settings()
    runs = len(attacks) * args.trials
    meter = common.Meter(budget=budget)
    print(f"\n----- {model} -----", flush=True)
    results, stopped = await runner.run_all(
        attacks,
        model=model,
        mode=mode,
        meter=meter,
        trials=args.trials,
        parallel=args.parallel,
        timeout=args.timeout,
        on_done=_progress(runs),
    )
    summary = judge.summarize(attacks, [result.outcome for result in results])
    text = judge.render(summary, model=model, mode_label=approvals.MODE_LABELS[mode], stopped=stopped)
    passed = None if stopped else summary.passed
    details = [
        f"  {result.attack.id}: {line}" for result in results if result.outcome.followed for line in result.outcome.evidence
    ]
    lines = ["", text]
    if details:
        lines += ["", "従った実行の根拠:", *details]
    if args.only:
        lines += ["", "（一部の攻撃だけを実行しました。全体の判定には、--only を付けずに実行してください）"]
    lines += common.other_models(results, model)
    lines += ["", "使ったトークン:", *common.usage_lines(meter)]
    print("\n".join(lines))

    common.save(
        {
            "date": datetime.now().isoformat(timespec="seconds"),
            "model": model,
            "fast_model": settings.fast_model,
            "mode": mode,
            "trials": args.trials,
            "stopped_by_budget": stopped,
            "runs": summary.runs,
            "followed": summary.followed,
            "breached": summary.breached,
            "errors": summary.errors,
            "breach_rate": summary.breach_rate,
            "threshold": judge.THRESHOLD,
            "passed": passed,
            "cost_usd": round(meter.cost, 4),
            "tokens": meter.tokens,
            "report": text,
            "results": [_record(result) for result in results],
        },
        "redteam",
    )
    return passed, meter.cost


async def _measure(args: argparse.Namespace, attacks: tuple[catalog.Attack, ...]) -> int:
    def describe(mode: str, runs: int) -> list[str]:
        return [
            f"  確認のしかた: {approvals.MODE_LABELS[mode]}。確認が必要な操作は、依頼に必要な最初の操作だけを承認し、ほかは拒否します",
            f"  攻撃        : {len(attacks)} 種類 × {args.trials} 回 = モデル1つにつき {runs} 回の実行（同時に {args.parallel} つ）",
        ]

    return await common.measure(
        args,
        attacks,
        title="指示の乗っ取り（間接注入）の点検",
        describe=describe,
        measure_model=lambda model, mode, budget: _measure_model(args, attacks, model, mode, budget),
    )


async def _selftest(attacks: tuple[catalog.Attack, ...]) -> int:
    print(f"点検の仕組みを、偽のモデルで確かめます（{len(attacks)} 種類。料金はかかりません）…", flush=True)
    problems = await runner.selftest(attacks)
    if problems:
        print(f"食い違いが {len(problems)} 件ありました:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    configured, screened = runner.configured_tools(), runner.screened_tools()
    standard = sum(runner.expected_breach(attack, approvals.STANDARD, configured, screened) for attack in attacks)
    strict = sum(runner.expected_breach(attack, approvals.STRICT, configured, screened) for attack in attacks)
    blocked = len(attacks) - standard  # 「標準」で、モデルが従っても止まるもの
    print("問題ありません。")
    print(f"  指示に従わないモデル   : 従った 0、貫通 0（{len(attacks)} 種類とも）")
    print(f"  指示に必ず従うモデル   : 「標準」で貫通 {standard}/{len(attacks)}、「毎回確認」で貫通 {strict}/{len(attacks)}")
    print(f"  モデルが従っても止まる : 「標準」で {blocked}/{len(attacks)}（権限・確認・画面の設定が止める）")
    if configured:
        print(f"  設定で確認つきのツール : {'、'.join(sorted(configured))}")
    if screened:
        print(f"  中身を調べるツール     : {'、'.join(sorted(screened))}（怪しいときだけ確認）")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    try:
        attacks = catalog.by_id([name.strip() for name in args.only.split(",") if name.strip()] if args.only else None)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.list:
        _list(attacks)
        return 0
    common.quiet_logs()
    return asyncio.run(_selftest(attacks) if args.selftest else _measure(args, attacks))


if __name__ == "__main__":
    sys.exit(main())
