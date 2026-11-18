"""ガードレールの検出ルールとマスク。

形が決まっている情報（API キー、秘密鍵、カード番号、マイナンバー、メールアドレス、電話番号、口座番号）を
正規表現で見つける。見つけたものをどう扱うかは、種類ごとに config/security.yaml で決める。

    mask: 「[マスク済み:種類]」に置き換える
    log : 置き換えずに、見つけたことだけを記録する
    off : 何もしない

形が決まっていないもの（独自形式のパスワード、氏名、住所など）は見つけられない。
データベースの個人情報は、列ごとに読めなくして守る（scripts/setup_data_db.sh）。

用途ごとの、形が決まった情報（社員番号、案件のコードなど）は、config/security.yaml の
guardrails.custom_rules に正規表現で書き足せる（custom_rule()。ADR-0032）。

日本語の文章で見逃さないための決まり:
- 前後の区切りに \b をそのまま使わない。\b は、ひらがなや漢字も「単語の文字」として扱うので、
  「鍵はsk-…です」のように日本語に続けて書かれると、区切りにならない。
  秘密情報のルールは re.ASCII で作り、番号のルールは「前後が数字でない」ことで区切る。
- 数字とハイフンは、全角でも受け付ける。
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

Action = Literal["mask", "log", "off"]

MASK_PREFIX = "[マスク済み:"
SECRET = "secret"  # noqa: S105  検出の種類の名前（鍵ではない）

DEFAULT_ACTIONS: dict[str, Action] = {
    SECRET: "mask",
    "credit_card": "mask",
    "my_number": "mask",
    "email": "mask",
    "phone": "mask",
    "bank_account": "mask",
}


def _digits(text: str) -> str:
    """数字だけを取り出す（全角の数字は、半角にそろえる）。"""
    return re.sub(r"\D", "", unicodedata.normalize("NFKC", text))


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


# カード会社ごとの先頭の数字と桁数。時刻（ミリ秒）などの長い数字を、カード番号と間違えないために使う
_CARD_PREFIXES: tuple[tuple[re.Pattern[str], tuple[int, ...]], ...] = (
    (re.compile(r"4"), (13, 16, 19)),  # Visa
    (re.compile(r"5[1-5]|2(?:2[2-9]|[3-6]\d|7[01]|720)"), (16,)),  # Mastercard
    (re.compile(r"3[47]"), (15,)),  # American Express
    (re.compile(r"35"), (16,)),  # JCB
    (re.compile(r"3[0689]"), (14,)),  # Diners Club
    (re.compile(r"6011|65"), (16,)),  # Discover
    (re.compile(r"62"), (16, 17, 18, 19)),  # UnionPay
)


def _is_card_number(text: str) -> bool:
    digits = _digits(text)
    if not any(prefix.match(digits) and len(digits) in lengths for prefix, lengths in _CARD_PREFIXES):
        return False
    return _luhn_ok(digits)


def _is_my_number(text: str) -> bool:
    """マイナンバー（12桁）の検査用数字を確かめる。"""
    digits = _digits(text)
    if len(digits) != 12:
        return False
    body, check = digits[:11], int(digits[11])
    total = 0
    for n, ch in enumerate(reversed(body), start=1):  # n は右から数えた位置
        weight = n + 1 if n <= 6 else n - 5
        total += int(ch) * weight
    remainder = total % 11
    return check == (0 if remainder <= 1 else 11 - remainder)


def _is_phone(text: str) -> bool:
    """電話番号の桁数と、先頭の番号を確かめる。注文番号（0001-0002-0003）などを、電話番号と間違えないために使う。"""
    digits = _digits(text)
    if text.startswith(("+", "＋")):
        return digits.startswith("81") and len(digits) in (11, 12)  # +81 と、先頭の 0 を除いた 9〜10 桁
    if len(digits) < 2 or digits[0] != "0" or digits[1] == "0":
        return False
    if len(digits) == 10:  # 固定電話、0120、0570 など
        return True
    # 11 桁は、携帯電話・IP 電話など（020・050・060・070・080・090）と、0800
    return len(digits) == 11 and digits[1] in "256789" and digits[2] == "0"


@dataclass(frozen=True)
class Rule:
    kind: str  # 設定ファイルと監査ログで使う名前
    label: str  # マスクしたあとに表示する名前
    pattern: re.Pattern[str]
    validate: Callable[[str], bool] | None = None  # 形が合ったものを、さらに確かめる
    group: int = 0  # マスクする部分（0 は一致した全体。前に付く言葉を残すときは、かっこの番号）


def _secret(pattern: str) -> Rule:
    # re.ASCII を付けると、\b が英数字だけを「単語の文字」として扱う（日本語に続けて書かれても見つかる）
    return Rule(SECRET, "秘密情報", re.compile(pattern, re.ASCII))


_D = "[0-9０-９]"  # 数字（全角を含む）
_ZERO = "[0０]"
_HYPHEN = r"[\-‐‑‒–—―−ー－]"  # ハイフンの類（全角、マイナス記号、長音記号を含む）
_GAP = rf"(?:{_HYPHEN}|[ \u3000])"  # 番号の途中の区切り（ハイフンの類か、空白1つ）
_PHONE_GAP = rf"(?:{_HYPHEN}|[ \u3000.．])"  # 電話番号は、ピリオドで区切ることもある
# 数字の並びの途中からは拾わない。小数（0.0312345678）の小数点より後ろも拾わない
_NOT_AFTER_NUMBER = r"(?<![0-9０-９\-‐‑‒–—―−－])(?<![0-9０-９][.．])"
_NOT_BEFORE_NUMBER = r"(?![0-9０-９\-‐‑‒–—―−－])"

_PHONE = re.compile(
    _NOT_AFTER_NUMBER
    + "(?:"
    # 国際表記（+81 90 1234 5678）
    + rf"[+＋]81{_PHONE_GAP}?{_D}{{1,4}}{_PHONE_GAP}?{_D}{{1,4}}{_PHONE_GAP}?{_D}{{3,4}}"
    # かっこ付き（(03)1234-5678、03(1234)5678）
    + rf"|[(（]{_ZERO}{_D}{{1,4}}[)）]{_PHONE_GAP}?{_D}{{1,4}}{_PHONE_GAP}?{_D}{{3,4}}"
    + rf"|{_ZERO}{_D}{{1,4}}[(（]{_D}{{1,4}}[)）]{_D}{{3,4}}"
    # 区切りあり（090-1234-5678、03 1234 5678、090.1234.5678）
    + rf"|{_ZERO}{_D}{{1,4}}{_PHONE_GAP}{_D}{{1,4}}{_PHONE_GAP}{_D}{{3,4}}"
    # 区切りなし（09012345678、0312345678）
    + rf"|{_ZERO}{_D}{{9,10}}"
    + ")"
    + _NOT_BEFORE_NUMBER
)


RULES: tuple[Rule, ...] = (
    # 秘密鍵のブロック（終わりの行がなければ、文章の最後まで）
    _secret(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)"),
    # Anthropic・OpenAI・Langfuse などの「sk-」で始まるキー。CSS のクラス名などと間違えないよう、数字を含むものに限る
    _secret(r"\bsk-(?=[A-Za-z0-9_\-]*\d)[A-Za-z0-9_\-]{20,}"),
    _secret(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),  # AWS のアクセスキー ID
    _secret(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),  # GitHub のトークン
    _secret(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b"),
    _secret(r"\bAIza[0-9A-Za-z_\-]{35}\b"),  # Google の API キー
    _secret(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b"),  # Slack のトークン
    Rule(
        "credit_card",
        "カード番号",
        re.compile(rf"{_NOT_AFTER_NUMBER}{_D}(?:{_GAP}?{_D}){{12,18}}{_NOT_BEFORE_NUMBER}"),
        _is_card_number,
    ),
    Rule(
        "my_number",
        "マイナンバー",
        re.compile(rf"{_NOT_AFTER_NUMBER}{_D}{{4}}{_GAP}?{_D}{{4}}{_GAP}?{_D}{{4}}{_NOT_BEFORE_NUMBER}"),
        _is_my_number,
    ),
    Rule("email", "メールアドレス", re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")),
    Rule("phone", "電話番号", _PHONE, _is_phone),
    # 口座番号は、7 桁の数字というだけでは見分けられないので、前に「口座」「普通」などの言葉があるものに限る。
    # 言葉は残して、番号だけをマスクする
    Rule(
        "bank_account",
        "口座番号",
        re.compile(rf"(?:口座|普通|当座|預金)[^0-9０-９\n]{{0,8}}?(?<![0-9０-９])({_D}{{7}})(?![0-9０-９])"),
        group=1,
    ),
)


# 種類ごとの表示名（利用者への通知に使う）。独自のルールの分は、Guard.labels にある
LABELS: dict[str, str] = {rule.kind: rule.label for rule in RULES}

KIND_PATTERN = re.compile(r"[a-z][a-z0-9_]{1,30}")  # 独自のルールの名前（設定ファイルと監査ログで使う）


def custom_rule(kind: str, label: str, pattern: str, *, ignore_case: bool = False) -> Rule:
    """設定ファイルに書いた、独自の検出ルールを作る。誤りがあれば ValueError（理由は日本語）。"""
    if not KIND_PATTERN.fullmatch(kind):
        raise ValueError(f"kind は、英小文字で始まる、英小文字・数字・_ の 2〜31 文字にしてください: {kind!r}")
    if kind in LABELS:
        raise ValueError(f"kind の {kind!r} は、元からあるルールの名前です。別の名前にしてください")
    if not label.strip() or "]" in label or len(label) > 20:
        raise ValueError(f"label は、] を含まない 20 文字以内にしてください: {label!r}")
    try:
        compiled = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        raise ValueError(f"pattern の正規表現が読めません（{kind}）: {exc}") from exc
    if compiled.search("") is not None:
        # 空の文字列に一致するものは、どこにでも一致して、文章を壊す
        raise ValueError(f"pattern が、空の文字列にも一致します（{kind}）。1 文字以上に一致する形にしてください")
    return Rule(kind, label.strip(), compiled)


@dataclass(frozen=True)
class Finding:
    """見つけたものの集計。値そのものは持たない。"""

    kind: str
    count: int
    action: Action

    def as_dict(self) -> dict[str, str | int]:
        return {"kind": self.kind, "count": self.count, "action": self.action}


class Guard:
    def __init__(
        self,
        actions: Mapping[str, Action] | None = None,
        *,
        enabled: bool = True,
        custom: Iterable[tuple[Rule, Action]] = (),
    ) -> None:
        """actions は元からあるルールの扱い。custom は独自のルールと、その扱い（custom_rule() で作る）。"""
        custom = list(custom)
        self._actions: dict[str, Action] = {**DEFAULT_ACTIONS, **(actions or {}), **{r.kind: a for r, a in custom}}
        self._rules: tuple[Rule, ...] = (*RULES, *(rule for rule, _ in custom))
        self._enabled = enabled
        self.labels: dict[str, str] = {**LABELS, **{rule.kind: rule.label for rule, _ in custom}}

    def scan(self, text: str) -> tuple[str, list[Finding]]:
        """マスクしたあとの文章と、見つけたものの集計を返す。"""
        if not self._enabled or not text:
            return text, []

        matches: list[tuple[int, int, Rule, Action]] = []
        for rule in self._rules:
            action = self._actions.get(rule.kind, "off")
            if action == "off":
                continue
            for m in rule.pattern.finditer(text):
                if rule.validate is None or rule.validate(m.group(rule.group)):
                    matches.append((m.start(rule.group), m.end(rule.group), rule, action))
        # 重なったときは、先に始まるものを優先する（同じ位置なら長いもの）
        matches.sort(key=lambda item: (item[0], item[0] - item[1]))

        parts: list[str] = []
        counts: Counter[tuple[str, Action]] = Counter()
        written = 0  # ここまでを parts に書き出した
        covered = 0  # ここまでは、すでに何かに一致した
        for start, end, rule, action in matches:
            if start < covered:
                continue
            covered = end
            counts[(rule.kind, action)] += 1
            if action == "mask":
                parts.append(text[written:start])
                parts.append(f"{MASK_PREFIX}{rule.label}]")
                written = end
        parts.append(text[written:])
        findings = [Finding(kind, count, action) for (kind, action), count in counts.items()]
        return "".join(parts), findings

    def write_denial_reason(self, text: str) -> str | None:
        """ファイルに書き込んではいけない内容なら、その理由を返す。問題なければ None。"""
        if not self._enabled or not text:
            return None
        if MASK_PREFIX in text:
            return "masked_placeholder"
        _, findings = self.scan(text)
        if any(f.kind == SECRET and f.action == "mask" for f in findings):
            return SECRET
        return None


def masked_only(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.action == "mask"]


def _main(argv: list[str] | None = None) -> int:
    """文章を、いまの設定（config/security.yaml）のガードレールに通して、どうマスクされるかを見る。"""
    import argparse

    from data_agent.settings import get_guard

    parser = argparse.ArgumentParser(
        prog="python -m data_agent.safety.guardrails",
        description="文章を、いまの設定のガードレールに通して、どうマスクされるかを見る（独自のルールを試すときに使う）",
    )
    parser.add_argument("text", help="試す文章")
    args = parser.parse_args(argv)
    try:
        guard = get_guard()
    except ValueError as exc:  # 設定の誤り（独自のルールの書き間違いなど）
        print(f"config/security.yaml に誤りがあります:\n{exc}")
        return 1
    masked, findings = guard.scan(args.text)
    print(f"結果: {masked}")
    for finding in findings:
        print(f"  {guard.labels.get(finding.kind, finding.kind)}（{finding.kind}）: {finding.count} 件、扱い: {finding.action}")
    if not findings:
        print("  見つかったものはありません。")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
