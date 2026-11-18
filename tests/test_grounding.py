"""返事の数と URL の、根拠との照合（grounding.py。ADR-0026）。

画面での注記の出方は、tests/e2e/check_grounding.py で確かめる。照合の規則そのものは、
誤情報の点検（tests/test_factcheck.py）でも確かめている（同じ規則を使う）。
"""

from __future__ import annotations

import time
from pathlib import Path

from langchain_core.messages import HumanMessage, ToolMessage

from data_agent.safety import grounding
from data_agent.settings import GroundingConfig

SALES = "\n".join(
    f"{n:6d}\t{line}"
    for n, line in enumerate(["月,地域,売上（円）", "2026-10,東京,1284600", "2026-10,大阪,903500", "2026-10,福岡,468100"], 1)
)


def test_numbers_in_the_grounds_are_verified() -> None:
    report = grounding.check("東京は 1,284,600 円、大阪は約 90 万円です。", [SALES])
    assert report.numbers == [] and not report.unverified


def test_numbers_not_in_the_grounds_are_unverified() -> None:
    report = grounding.check("東京は 1,350,000 円です。", [SALES])
    assert report.numbers == [1_350_000] and report.unverified


def test_numbers_the_user_wrote_are_verified() -> None:
    report = grounding.check("目標の 2,000,000 円には届いていません。東京は 1,284,600 円です。", [SALES, "目標は 200 万円です"])
    assert not report.unverified


def test_correct_calculations_are_verified_and_wrong_ones_are_not() -> None:
    ok = grounding.check("3 地域の合計は 2,656,200 円、東京と大阪の差は 381,100 円です。", [SALES])
    assert not ok.unverified and len(ok.calculated) == 2
    wrong = grounding.check("3 地域の合計は 2,665,200 円です。", [SALES])
    assert wrong.numbers == [2_665_200]


def test_common_numbers_are_not_counted() -> None:
    report = grounding.check("2026 年 10 月 15 日、30 秒ごとに、全体の 48% を、2.12.0 で、/data/2026.csv に書いた", [])
    assert not report.unverified


def test_urls_cited_and_urls_only_pointed_to() -> None:
    grounds = ["出典: https://github.com/Chainlit/chainlit/releases"]
    ok = grounding.check("出典: https://github.com/Chainlit/chainlit/releases/", grounds)
    assert not ok.unverified
    cited = grounding.check("最新は 2.12.0 です（https://chainlit.io/blog/x）。", grounds)
    assert cited.urls == ["https://chainlit.io/blog/x"] and cited.unverified
    pointed = grounding.check("PyPI（https://pypi.org/project/chainlit/ ）は開いて確かめていません。", grounds)
    assert pointed.pointed == ["https://pypi.org/project/chainlit"] and not pointed.unverified


def test_more_ways_of_pointing_to_a_url() -> None:
    """確かめ先の案内の、ほかの言い方（2026-11-15、Opus 5.5 の返事）。確かめたという言い切りは、出典として数える。"""
    grounds = ["出典: https://github.com/Chainlit/chainlit/releases"]
    for answer in [
        "リリースはよく出るので、使う前に PyPI（https://pypi.org/project/chainlit/ ）か `pip index versions chainlit` で確かめると確実です。",
        "最新の版は、https://pypi.org/project/chainlit/ で確認できます。",
        "インストールの前に https://pypi.org/project/chainlit/ を見るのがおすすめです。",
        "PyPI（https://pypi.org/project/chainlit/ ）での確認をおすすめします。",
    ]:
        report = grounding.check(answer, grounds)
        assert report.pointed == ["https://pypi.org/project/chainlit"] and not report.unverified, answer
    for answer in [
        "https://pypi.org/project/chainlit/ で確認したところ、最新は 2.12.0 です。",
        "https://pypi.org/project/chainlit/ で確認できました。",
        "https://pypi.org/project/chainlit/ を確認すると確かに 2.12.0 でした。",
        "PyPI（https://pypi.org/project/chainlit/ ）を参照してください。",
    ]:
        report = grounding.check(answer, grounds)
        assert report.urls == ["https://pypi.org/project/chainlit"] and not report.pointed, answer


def test_large_tables_are_checked_quickly() -> None:
    rows = "\n".join(f"{n},地域{n % 100},{n * 37}" for n in range(20_000))
    started = time.monotonic()
    report = grounding.check("合計は 7,399,630,000 円で、最後の行は 739,963 円です。", [f"番号,地域,売上\n{rows}"])
    assert time.monotonic() - started < 3
    assert not report.unverified  # 列の合計（20,000 行）と、表にある値


def test_only_turns_that_read_data_are_checked() -> None:
    limits = GroundingConfig()
    check = lambda calls: grounding.used_data(calls, limits.tools, limits.file_suffixes)  # noqa: E731
    assert check([("read_file", {"file_path": "/data/Sales.CSV"})])
    assert check([("query_database", {"sql": "select 1"})])
    assert check([("web_search", {"question": "chainlit"})])
    assert check([("run_python", {"code": "print(1)"})])
    assert not check([("read_file", {"file_path": "/src/app.py"}), ("edit_file", {"file_path": "/src/app.py"})])
    assert not check([])


def test_note_lists_numbers_urls_and_sources() -> None:
    report = grounding.Report(numbers=[float(n) for n in range(1_000, 1_013)], urls=["https://x.example/a"])
    calls = [
        ("read_file", {"file_path": "/data/sales.csv"}),
        ("read_file", {"file_path": "/data/sales.csv"}),
        ("query_database", {}),
        ("query_database", {}),
        ("web_search", {}),
    ]
    text = grounding.note(report, grounding.sources(calls), max_items=10)
    assert grounding.NOTE_TITLE in text and text.startswith("---")
    assert "- 数: 1,000、1,001" in text and "1,009 ほか 3 個" in text
    assert "- URL: `https://x.example/a`" in text  # 押して開かないように、コードの書式にする
    assert "- 根拠に使ったもの: `/data/sales.csv`、データベースの問い合わせ 2 回、Web 検索 1 回" in text


# ---------- 1回の依頼の返事を、まとめて照合する（画面と定時実行。review） ----------
_READ = [("read_file", {"file_path": "/data/sales.csv"})]


def test_all_replies_of_a_request_are_checked() -> None:
    """ツールを呼ぶ前に話した文の数も、照合する（数を言ってから、ファイルに保存する、など）。"""
    report = grounding.check_replies(["東京は 1,350,000 円です。ファイルに保存します。", "保存しました。"], [SALES])
    assert report.numbers == [1_350_000]
    report = grounding.check_replies(["東京は 1,284,600 円です。", "合計は 2,656,200 円です。"], [SALES])
    assert not report.unverified and report.calculated


def test_replies_are_checked_one_by_one() -> None:
    """返事をつなげて読まない（前の返事の終わりの数と、次の返事の始まりの「万」を、1つの数にしない）。"""
    report = grounding.check_replies(["東京は 1284600", "万が一に備えて、保存しました。"], [SALES])
    assert not report.unverified


def _review(replies: list[str], calls: list, config: GroundingConfig | None = None, **kwargs):
    kwargs.setdefault("messages", [HumanMessage("売上をまとめて")])
    kwargs.setdefault("results", [SALES])
    kwargs.setdefault("memory_dir", Path("/nonexistent"))
    return grounding.review(replies, calls, config=config or GroundingConfig(), where="chat", **kwargs)


def test_review_notes_unverified_numbers_in_any_reply() -> None:
    report, text = _review(["東京は 1,350,000 円です。保存します。", "保存しました。"], _READ)
    assert report is not None and report.numbers == [1_350_000]
    assert grounding.NOTE_TITLE in text and "- 数: 1,350,000" in text and "`/data/sales.csv`" in text


def test_review_returns_no_note_when_everything_is_verified() -> None:
    report, text = _review(["東京は 1,284,600 円です。"], _READ)
    assert report is not None and not report.unverified and text == ""


def test_review_uses_the_conversation_and_long_term_memory_as_grounds(tmp_path) -> None:
    """根拠は、ツールの結果のほか、会話の利用者の発言とツールの結果、長期メモ。"""
    (tmp_path / "goals.md").write_text("目標は 3,000,000 円", encoding="utf-8")
    messages = [HumanMessage("予算は 2,000,000 円です"), ToolMessage(SALES, tool_call_id="call-1")]
    _, text = _review(
        ["予算 2,000,000 円、目標 3,000,000 円、東京 1,284,600 円"], _READ, messages=messages, results=[], memory_dir=tmp_path
    )
    assert text == ""
    _, text = _review(["予算 2,000,000 円、目標 3,000,000 円"], _READ, messages=[], results=[SALES])
    assert "2,000,000" in text and "3,000,000" in text


def test_review_skips_requests_that_read_no_data(monkeypatch) -> None:
    def memory_texts(folder) -> list[str]:
        raise AssertionError("照合しない回は、根拠を集めない")

    monkeypatch.setattr(grounding, "memory_texts", memory_texts)
    for replies, calls, config in [
        (["ポートは 8443 です。"], [("ls", {"path": "/"})], GroundingConfig()),  # データを読んでいない
        (["", ""], _READ, GroundingConfig()),  # 返事がない
        (["東京は 1,350,000 円です。"], _READ, GroundingConfig(enabled=False)),  # 照合を切ってある
    ]:
        assert _review(replies, calls, config) == (None, "")


def test_memory_texts_reads_markdown_files(tmp_path) -> None:
    (tmp_path / "team").mkdir()
    (tmp_path / "team" / "goals.md").write_text("目標は 200 万円", encoding="utf-8")
    (tmp_path / "skip.txt").write_text("読まない", encoding="utf-8")
    assert grounding.memory_texts(tmp_path) == ["目標は 200 万円"]
    assert grounding.memory_texts(tmp_path / "none") == []
