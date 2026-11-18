"""決めたことの記録（docs/adr/ の ADR）の書き方と、一覧の漏れを確かめる（AGENTS.md の「決めたことを残す」）。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ADR_DIR = ROOT / "docs" / "adr"
ADRS = sorted(ADR_DIR.glob("[0-9][0-9][0-9][0-9]-*.md"))
FIELDS = ("状態", "日付", "決定者")


def test_numbers_are_sequential() -> None:
    numbers = [int(path.name[:4]) for path in ADRS]
    assert numbers == list(range(1, len(ADRS) + 1)), "番号は 0001 からの連番にする（欠番・重なりを作らない）"


@pytest.mark.parametrize("path", ADRS, ids=lambda path: path.name[:4])
def test_each_record_has_the_required_parts(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert re.fullmatch(r"\d{4}-[a-z0-9-]+\.md", path.name), "ファイル名は「NNNN-短い英語の名前.md」"
    assert text.startswith(f"# ADR-{path.name[:4]}: "), "1 行目は「# ADR-NNNN: 決めたこと」"
    for field in FIELDS:
        assert re.search(rf"^- \*\*{field}:\*\* \S", text, re.M), f"「- **{field}:**」がない"
    assert re.search(r"^- \*\*日付:\*\* \d{4}-\d{2}-\d{2}", text, re.M), "日付は YYYY-MM-DD"
    headings = re.findall(r"^## (.+)$", text, re.M)
    assert "状況" in headings, "「## 状況」がない"
    assert any(heading.startswith("決定") for heading in headings), "「## 決定」がない"


@pytest.mark.parametrize(("index", "prefix"), [("docs/adr/README.md", ""), ("AGENTS.md", "docs/adr/")])
def test_every_record_is_listed(index: str, prefix: str) -> None:
    text = (ROOT / index).read_text(encoding="utf-8")
    missing = [path.name for path in ADRS if f"[{path.name[:4]}]({prefix}{path.name})" not in text]
    assert missing == [], f"{index} の一覧に足りない: {missing}"


def test_the_template_has_the_required_parts() -> None:
    text = (ADR_DIR / "template.md").read_text(encoding="utf-8")
    for field in FIELDS:
        assert f"- **{field}:**" in text
    for heading in ("## 状況", "## 決定", "## 検討した案", "## 確かめたこと", "## 分かっている制約", "## 次にやること"):
        assert heading in text
