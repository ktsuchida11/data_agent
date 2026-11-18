"""定時実行の予定の解釈（schedules）。データベースには接続しない。"""

from datetime import datetime

import pytest

from data_agent.features import schedules
from data_agent.features.schedules import ScheduleError

TOKYO = "Asia/Tokyo"


def _parse(text: str, min_interval: int = 5, max_chars: int = 200):
    return schedules.parse(text, timezone=TOKYO, min_interval_minutes=min_interval, max_prompt_chars=max_chars)


def test_cron_and_prompt_are_split() -> None:
    parsed = _parse("0 9 * * 1-5  README を読んで、  今日やることをまとめて")
    assert parsed.cron == "0 9 * * 1-5"
    # 依頼文の中の空白は、そのまま残す
    assert parsed.prompt == "README を読んで、  今日やることをまとめて"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("こんにちは", "5項目"),  # cron 式がない
        ("0 9 * * 1-5", "5項目"),  # 依頼文がない
        ("99 9 * * * だめな時刻", "読み取れませんでした"),
        ("* * * * * 毎分やって", "間隔が短すぎます"),
        ("*/2 * * * * 2分おき", "間隔が短すぎます"),
        ("0 9 * * * " + "あ" * 201, "長すぎます"),
    ],
)
def test_bad_input_is_refused_with_a_reason(text: str, message: str) -> None:
    with pytest.raises(ScheduleError, match=message):
        _parse(text)


def test_five_minute_interval_is_allowed() -> None:
    assert _parse("*/5 * * * * 5分おき").cron == "*/5 * * * *"


def test_next_run_is_computed_in_the_configured_timezone() -> None:
    # 2026-11-13（金）12:30 UTC は、日本時間で同じ日の 21:30。次の「平日の朝9時」は月曜の朝
    after = datetime(2026, 11, 13, 12, 30)
    # 日本時間の 11/16（月）09:00 は、UTC では 11/16 00:00
    assert schedules.next_run("0 9 * * 1-5", TOKYO, after) == datetime(2026, 11, 16, 0, 0)
    # 同じ式を UTC で解釈すると、UTC の 11/16 09:00 になる
    assert schedules.next_run("0 9 * * 1-5", "UTC", after) == datetime(2026, 11, 16, 9, 0)


def test_upcoming_runs_are_shown_in_local_time() -> None:
    after = datetime(2026, 11, 13, 12, 30)
    shown = [schedules.format_time(t) for t in schedules.upcoming("0 9 * * 1-5", TOKYO, after=after)]
    assert shown == ["11/16 09:00", "11/17 09:00", "11/18 09:00"]


def test_stored_time_is_shown_in_local_time() -> None:
    assert schedules.format_time(schedules.to_local(datetime(2026, 11, 16, 0, 0), TOKYO)) == "11/16 09:00"
