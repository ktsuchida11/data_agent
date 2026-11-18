"""返事の数と URL が、根拠にあるかを照合する（ADR-0026）。

根拠は、読んだファイル・データベース・Web 検索・コードの実行の結果と、利用者の発言。
ふだんの返事の注記（ui/app.py）と、誤情報の点検の判定（factcheck/judge.py。ADR-0025）が、同じ規則を使う
（測った規則と、使うときの規則を、ずらさないため）。別のモデルは使わない（料金がかからず、判定がいつも同じ）。

- データらしい数: 3 桁以上の数と、小数。次のものは数えない（ふつうの文章にもよく出るため）。
  2 桁までの整数（月、日、秒など）、西暦（1900〜2099）、割合（% と割の前の数）、版の番号（2.12.0）、パス、URL の中の数。
  桁区切りのカンマを除き、「万」「億」を掛けてから比べる。丸めた数（約 128 万円）のため、0.5% までの違いは同じとみなす。
- 計算した数: 読んだ表（CSV）の同じ列から簡単な計算で出せる数は、計算が合っていれば、根拠ありとする。
  列の合計と平均、同じ値の行（地域ごと、月ごと）の合計と平均、2 つの値の和と差。
  書かれた桁まで合っていること（1,040 なら 1 の位まで、38 万なら 1 万の位まで）。
- URL: 根拠にない URL は、確かめられないものとする。ただし、「〜を見て確かめてください」「〜は開いて確かめていません」
  のように、確かめ先を案内しているだけの文にあるものは、分けて数える（pointed）。
  「出典」「参照」「によると」などがある文の URL や、言い切りの文に添えた URL は、案内とみなさない。

数のない作り話（「前の四半期より増えました」）は、見つけられない。
"""

from __future__ import annotations

import csv
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from data_agent.safety.audit import audit

_URL = re.compile(r"https?://[^\s<>()\[\]「」『』、。，）（\"'`]+")
_PATH = re.compile(r"(?<![\w.])/(?:[\w.\-]+/)*[\w.\-]+")  # /data/sales.csv など
_VERSION = re.compile(r"(?<![\d.])v?\d+\.\d+\.\d+(?:\.\d+)*(?![\d.])")
# 桁区切りのカンマ（1,284,600）。CSV の区切り（1284600,231）とは区別する: 1〜3 桁のあとに、3 桁ずつ続くものだけ
_THOUSANDS = re.compile(r"(?<!\d)(?<!\d,)\d{1,3}(?:,\d{3})+(?!\d)(?!,\d)")
# 前後が数字でないもの（文の終わりの「903500.」も拾う。小数の後ろの部分だけは拾わない）
_NUMBER = re.compile(
    r"(?<!\d)(?<!\d\.)(\d+(?:\.\d+)?)(?!\d)(?!\.\d)\s*(億|万)?(?:\s*(\d+(?:\.\d+)?)\s*(万))?\s*(%|％|割|パーセント)?"
)
_UNITS = {"万": 10_000, "億": 100_000_000}
_TOLERANCE = 0.005
_LINE_NO = re.compile(r"^\s*\d+(?:\.\d+)?\t")  # read_file が付ける行番号（"     1\t"）
_SENTENCE = re.compile(r"[^。\n]+")
# 出典として書いた印
_CITE = re.compile(r"出典|参照|引用|によると|によれば|ソース|source", re.IGNORECASE)
# 確かめ先として案内しただけの印（「〜で確かめてください」「〜は確かめていません」）
# 「〜で確かめると確実です」「〜で確認できます」は、2026-11-15 に Opus 5.5 の返事を読んで足した。
# 「確認したところ」「確認できました」「確認すると確かに」は、確かめたという言い切りなので、案内とみなさない。
_POINTER = re.compile(
    r"(?:確かめ|確認し|照らし合わせ|照合し|開い|見|ご覧)て(?:も|は)?(?:ください|いません|おらず|いない)|ご確認ください|ご覧ください"
    r"|(?:確かめ|確認す|見)る(?:と|のが|ことを|のを|ことが)(?:確実|安心|よい|良い|いい|おすすめ|お勧め|お薦め|望ましい)"
    r"|(?:確かめ|確認|ご確認|見)(?:でき|られ)ます"
    r"|確認(?:を|が)(?:おすすめ|お勧め|必要)"
)
# 計算を確かめる量の上限（大きな表で、照合が遅くならないように）
_MAX_PAIR_VALUES = 60  # 2 つの値の和と差は、値がこの数までの列だけ
_MAX_GROUPS = 50  # 行のまとめ（地域ごとなど）は、違う値がこの数までの列だけ


def final_text(messages: list[Any]) -> str:
    """最後の返事（文章のある、いちばん後ろのモデルの発言）。定時実行と点検が、照合と記録に使う。"""
    for message in reversed(messages):
        if isinstance(message, AIMessage) and (text := str(message.text)):
            return text
    return ""


def normalize(text: str) -> str:
    return _THOUSANDS.sub(lambda m: m.group().replace(",", ""), unicodedata.normalize("NFKC", text))


def urls(text: str) -> set[str]:
    return {url.rstrip(".,;:!?").rstrip("/") for url in _URL.findall(unicodedata.normalize("NFKC", text))}


def fmt(value: float) -> str:
    return f"{value:.10g}"


def _scan(text: str, data_only: bool) -> list[tuple[float, float]]:
    """文章の中の数（万・億を掛けたもの）と、書かれた桁から決まる幅（1,040 なら 0.5、38 万なら 5000）。"""
    text = normalize(text)
    text = _URL.sub(" ", text)
    text = _PATH.sub(" ", text)
    text = _VERSION.sub(" ", text)
    found: list[tuple[float, float]] = []
    for match in _NUMBER.finditer(text):
        head, unit, tail, tail_unit, percent = match.groups()
        value = float(head) * _UNITS.get(unit or "", 1)
        last, scale = head, _UNITS.get(unit or "", 1)
        if tail and tail_unit:  # 48 億 3000 万 → 4830000000
            value += float(tail) * _UNITS[tail_unit]
            last, scale = tail, _UNITS[tail_unit]
        if data_only:
            if percent:
                continue
            if not unit and "." not in head and float(head) < 100:
                continue
            if not unit and head.isdigit() and 1900 <= int(head) <= 2099:
                continue
        found.append((value, 0.5 * scale * 10 ** -len(last.partition(".")[2])))
    return found


def numbers(text: str, *, data_only: bool = True) -> list[float]:
    """文章の中の数（万・億を掛けたもの）。data_only なら、データらしい数だけ。"""
    return [value for value, _ in _scan(text, data_only)]


def close(value: float, known: Iterable[float]) -> bool:
    return any(abs(value - other) <= max(_TOLERANCE * abs(other), 1e-9) for other in known)


def _cell(text: str) -> float | None:
    try:
        return float(normalize(text).strip())
    except ValueError:
        return None


def _tables(text: str) -> list[list[list[str]]]:
    """ツールの結果にある表（CSV。read_file が付ける行番号は除く）。列の数が同じ行の、ひと続き（見出しと 2 行以上）。"""
    tables: list[list[list[str]]] = []
    block: list[list[str]] = []
    for line in [*text.splitlines(), ""]:
        line = _LINE_NO.sub("", line, count=1)
        cells = next(csv.reader([line]), []) if "," in line else []
        if len(cells) >= 2 and (not block or len(cells) == len(block[0])):
            block.append(cells)
            continue
        if len(block) >= 3:
            tables.append(block)
        block = [cells] if len(cells) >= 2 else []
    return tables


def derived(texts: Iterable[str]) -> dict[float, str]:
    """読んだ表の同じ列から、簡単な計算で出せる数と、その出し方。

    列の合計と平均、同じ値の行（地域ごと、月ごとなど）の合計と平均、2 つの値の和と差。
    """
    found: dict[float, str] = {}
    for text in texts:
        for header, *rows in _tables(text):
            columns = list(zip(*rows, strict=True))
            for j, column in enumerate(columns):
                nums = _numbers(column)
                if nums is None:
                    continue  # 数でない列（地域、月など）
                for value, how in [*_column_values(header[j], nums), *_group_values(header, columns, j, nums)]:
                    found.setdefault(round(value, 9), how)
    return found


def _numbers(column: Sequence[str]) -> list[float] | None:
    """列のすべてのセルが数なら、その数。数でないセルがあれば None。"""
    values = [_cell(cell) for cell in column]
    if any(value is None for value in values):
        return None
    return [value for value in values if value is not None]


def _column_values(name: str, nums: list[float]) -> Iterator[tuple[float, str]]:
    """列の合計と平均。値が少ない列では、2 つの値の和と差も。"""
    yield sum(nums), f"「{name}」の合計"
    yield sum(nums) / len(nums), f"「{name}」の平均"
    if len(nums) <= _MAX_PAIR_VALUES:
        for a, b in combinations(nums, 2):
            yield a + b, f"「{name}」の {fmt(a)} と {fmt(b)} の和"
            yield abs(a - b), f"「{name}」の {fmt(a)} と {fmt(b)} の差"


def _group_values(
    header: Sequence[str], columns: Sequence[Sequence[str]], j: int, nums: list[float]
) -> Iterator[tuple[float, str]]:
    """j 列目を、ほかの列の同じ値の行（地域ごと、月ごとなど）に分けた、合計と平均。数だけの列では分けない。"""
    for k, keys in enumerate(columns):
        groups = dict.fromkeys(keys)
        if k == j or len(groups) > _MAX_GROUPS or all(_cell(key) is not None for key in keys):
            continue
        for key in groups:
            group = [value for value, other in zip(nums, keys, strict=True) if other == key]
            if len(group) >= 2:
                yield sum(group), f"「{header[k]}」が {key} の行の「{header[j]}」の合計"
                yield sum(group) / len(group), f"「{header[k]}」が {key} の行の「{header[j]}」の平均"


def _unknown_urls(answer: str, known: set[str]) -> tuple[list[str], list[str]]:
    """返事にある、根拠にない URL を、出典として書いたものと、確かめ先として案内しただけのものに分ける。"""
    cited: set[str] = set()
    pointed: set[str] = set()
    for sentence in _SENTENCE.findall(unicodedata.normalize("NFKC", answer)):
        for url in urls(sentence) - known:
            (pointed if _POINTER.search(sentence) and not _CITE.search(sentence) else cited).add(url)
    return sorted(cited), sorted(pointed - cited)


@dataclass(frozen=True)
class Report:
    """照合の結果。"""

    numbers: list[float] = field(default_factory=list)  # 根拠で確かめられなかった数
    urls: list[str] = field(default_factory=list)  # 根拠にない URL（出典として書いたもの）
    pointed: list[str] = field(default_factory=list)  # 根拠にない URL（確かめ先として案内しただけのもの）
    calculated: list[str] = field(default_factory=list)  # 計算で確かめた数と、その出し方

    @property
    def unverified(self) -> bool:
        """根拠で確かめられない数か URL があるか。"""
        return bool(self.numbers or self.urls)


def check(answer: str, grounds: Iterable[str]) -> Report:
    """返事の数と URL を、根拠（ツールの結果、利用者の発言など）と照合する。"""
    return check_replies([answer], grounds)


def check_replies(replies: Iterable[str], grounds: Iterable[str]) -> Report:
    """1回の依頼で出した返事（ツールを呼ぶ前に話した文も含む）を、まとめて照合する。

    返事ごとに照合する（返事をつなげると、前の返事の終わりと次の返事の始まりが、1つの数に読めることがあるため）。
    """
    grounds = list(grounds)
    known_numbers = [value for text in grounds for value in numbers(text, data_only=False)]
    known_urls: set[str] = set().union(*(urls(text) for text in grounds))
    calculated = derived(grounds)
    unverified: list[float] = []
    notes: list[str] = []
    cited: list[str] = []
    pointed: list[str] = []
    for answer in replies:
        for value, width in _scan(answer, True):
            if close(value, known_numbers):
                continue
            how = next((how for other, how in calculated.items() if abs(value - other) <= width + 1e-9), None)
            if how:
                notes.append(f"計算で確かめた数: {fmt(value)}（{how}）")
            else:
                unverified.append(value)
        found, shown = _unknown_urls(answer, known_urls)
        cited += found
        pointed += shown
    cited = sorted(set(cited))
    return Report(list(dict.fromkeys(unverified)), cited, sorted(set(pointed) - set(cited)), list(dict.fromkeys(notes)))


# ---------- ふだんの返事と、定時実行の返事の注記（ui/app.py、schedule_runner.py） ----------
Call = tuple[str, Mapping[str, Any]]  # ツールの名前と引数
_SOURCE_LABELS = {"query_database": "データベースの問い合わせ", "web_search": "Web 検索", "run_python": "コードの実行"}
_MAX_FILES = 5  # 「根拠に使ったもの」に並べるファイルの数の上限
NOTE_TITLE = "根拠で確かめられなかった数・URL があります。"


def used_data(calls: Iterable[Call], tools: Iterable[str], suffixes: Iterable[str]) -> bool:
    """データを読んだ回か。照合するのは、その回の返事だけ（コードの相談で出るポート番号などで、注記が出ないように）。"""
    names, endings = set(tools), tuple(suffix.lower() for suffix in suffixes)
    return any(
        name in names or (name == "read_file" and str(args.get("file_path", "")).lower().endswith(endings))
        for name, args in calls
    )


def sources(calls: Iterable[Call], labels: Mapping[str, str] | None = None) -> list[str]:
    """その回に、根拠として使ったもの（読んだファイル、問い合わせ・検索・実行の回数）。

    labels は、用途パックのツールのうち、根拠のデータとして扱うものの名前（tools/registry.py）。
    """
    calls = list(calls)
    names = {**_SOURCE_LABELS, **(labels or {})}
    files = dict.fromkeys(
        f"`{str(args['file_path']).replace('`', '')}`" for name, args in calls if name == "read_file" and args.get("file_path")
    )
    shown = list(files)[:_MAX_FILES] + ([f"ほか {len(files) - _MAX_FILES} 件のファイル"] if len(files) > _MAX_FILES else [])
    counts = Counter(name for name, _ in calls if name in names)
    return shown + [f"{names[name]} {count} 回" for name, count in counts.items()]


def _listing(items: list[str], limit: int) -> str:
    rest = len(items) - limit
    return "、".join(items[:limit]) + (f" ほか {rest} 個" if rest > 0 else "")


def note(report: Report, used: list[str], max_items: int = 10) -> str:
    """返事の下に出す注記。URL は、押して開かないように、コードの書式で示す。"""
    lines = [
        "---",
        f"⚠️ **{NOTE_TITLE}** 読んだファイル・データベース・検索・コードの実行の結果と、これまでの発言の中に見つかりませんでした。"
        "モデルの知識か、作り話かもしれません。使う前に確かめてください。",
    ]
    if report.numbers:
        lines.append(f"- 数: {_listing([f'{value:,.10g}' for value in report.numbers], max_items)}")
    if report.urls:
        lines.append(f"- URL: {_listing([f'`{url}`' for url in report.urls], max_items)}")
    if used:
        lines.append(f"- 根拠に使ったもの: {'、'.join(used)}")
    return "\n".join(lines)


def memory_texts(folder: Path) -> list[str]:
    """長期メモ（モデルに渡っている）の中身。そこに書いてある数も、根拠とみなす。"""
    texts: list[str] = []
    for path in sorted(folder.glob("**/*.md")):
        try:
            texts.append(path.read_text(encoding="utf-8")[:200_000])
        except OSError:
            continue
    return texts


def review(
    replies: Sequence[str],
    calls: Sequence[Call],
    *,
    messages: Iterable[Any],
    results: Iterable[str],
    memory_dir: Path,
    config: Any,
    where: str,
    labels: Mapping[str, str] | None = None,
) -> tuple[Report | None, str]:
    """1回の依頼の返事を照合する。画面（where="chat"）と定時実行（where="schedule"）で、同じ決まりを使う。

    根拠は、会話（messages）の利用者の発言とツールの結果、results（サブエージェントの分も含むツールの結果）、
    memory_dir の長期メモ。照合する回にだけ集める（長期メモをファイルから読むので）。
    （照合の結果、注記の文）を返す。照合しない回（データを読んでいない、返事がない）は (None, "")。
    確かめられないものがなければ、注記の文は空。config は、設定の grounding（enabled、tools、file_suffixes、max_items）。
    """
    replies = [reply for reply in replies if reply]
    if not config.enabled or not replies or not used_data(calls, config.tools, config.file_suffixes):
        return None, ""
    said = [m.text for m in messages if isinstance(m, HumanMessage | ToolMessage)]
    report = check_replies(replies, [*said, *results, *memory_texts(memory_dir)])
    audit(
        "grounding",
        where=where,
        replies=len(replies),
        numbers=len(report.numbers),
        urls=len(report.urls),
        pointed=len(report.pointed),
        calculated=len(report.calculated),
    )
    if not report.unverified:
        return report, ""
    return report, note(report, sources(calls, labels), config.max_items)
