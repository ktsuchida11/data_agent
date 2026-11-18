"""Web 検索の質問文に、外に出したくない情報が入っていないかを調べる（ADR-0022）。

Web 検索の質問文は、検索のために外部に送られる。調べて、次のどれかが見つかったら、検索の前に利用者に確かめる。
見つからなければ、確かめずに検索する（config/security.yaml の web_search.require_approval: auto）。

- 手元のデータの写し（LOCAL）: このチャットで読んだもの（ファイル、データベースの結果、コードの出力、
  サブエージェントの返事、会話の要約）にあって、利用者が自分では書いていない文字列。
  ファイルやデータベースの値に仕込まれた指示に、モデルが従って、読んだ内容を質問文に書く経路を止めるため。
  見るのは、次の3つ（言い換えたものは、見つけられない）。
    番号やコード: 数字を含む、3文字以上の英数字（AOBA-7Q4KX、8,200、318 など。版の番号と西暦は除く）と、
            大文字を - や _ でつないだ、6文字以上のもの（AOBA-BNWQX、PRJ_ALPHA など）
    表の値: 区切り（, | 、 改行など）で区切った、4〜40 文字のかたまり（取引先の名前など。日本語を含むもの）
    長い文: 日本語を含む、10 文字以上続いた部分
- 見張る言葉（WATCH）: 顧客の名前など、一覧のファイル（web_search.watch_terms_file）に書いた言葉と、
  データベースの決めた列（顧客の名前の列など）の値。利用者が自分で書いた場合も、確かめる。
  一覧を作る（ファイルとデータベースから読み、間隔ごとに読み直す）のは watch_terms.py。
  一覧を読めなかったとき（読み終える前も）は、すべての検索を確かめる。

形の決まった秘密情報と個人情報（キー、カード番号、メールアドレス、電話番号など）は、ここでは見ない。
web_search.py が、ガードレールで調べて、検索そのものを断る。

判定は、モデルが検索を出した時点で行い、結果をその返事（AIMessage の response_metadata）に記録する
（SearchScreenMiddleware）。確認の仕組み（approvals.py）は、この記録を読む。承認を待って止まったあとで
再開したときに、判定が変わらないようにするため（変わると、利用者が拒否した検索が実行される。
LangChain の HumanInTheLoopMiddleware の when の注意書き）。見張る言葉の一覧を書き換えても、
すでに出た検索の判定は変わらない。

サブエージェントでは、親から渡された依頼文を、手元のデータとして扱う（親のモデルが書いたもので、
読んだ内容の写しが入りうるため）。利用者が書いた文章は、親の会話の状態（screen_user_texts）から引き継ぐ。

Web 検索のほかに、用途パックのツール（MCP のツールなど）も、扱いで require_approval: auto とすれば、
同じ判定で調べる（ADR-0032）。調べるのは、そのツールの引数（扱いで決めたもの。決めていなければ、すべての引数）。
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NotRequired

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage

from data_agent.safety.audit import audit

if TYPE_CHECKING:  # 型の注記だけに使う（watch_terms.py は、このファイルの normalize を使うため）
    from data_agent.safety.watch_terms import WatchTerms

TOOL_NAME = "web_search"
RECORD_KEY = "web_search_screening"  # 判定の記録（AIMessage の response_metadata の中。{tool_call_id: [Hit]}）
USER_TEXTS_KEY = "screen_user_texts"  # 利用者が書いた文章（会話の状態。サブエージェントに引き継ぐ）

LOCAL = "local"
WATCH = "watch"

MAX_HITS = 5  # 1回の検索で、記録して画面に出す数
MAX_HIT_CHARS = 60
_MIN_TOKEN = 3  # 番号やコードの長さの下限
_MIN_PIECE = 4  # 表の値の長さの下限
_MAX_PIECE = 40
_WINDOW = 10  # 長い文の写しとみなす、続いた文字の数
_MAX_LOCAL_CHARS = 400_000  # 調べる手元のデータの量（新しいものから）

_SUMMARY_SOURCE = "summarization"  # Deep Agents の要約の印（HumanMessage の additional_kwargs の lc_source）
# 桁区切りのカンマ（1,284,600）。CSV の区切り（1284600,231）とは区別する: 1〜3 桁のあとに、3 桁ずつ続くものだけ
_THOUSANDS = re.compile(r"(?<!\d)(?<!\d,)\d{1,3}(?:,\d{3})+(?!\d)(?!,\d)")
_TOKEN = re.compile(r"[0-9a-z][0-9a-z_\-.:/#@+]*")
_TOKEN_EDGE = "._-:/#@+"  # noqa: S105  番号の区切りに使う記号（鍵ではない）
_CODE = re.compile(r"(?<![0-9A-Za-z])[A-Z0-9]{2,}(?:[-_][A-Z0-9]{2,})+(?![0-9A-Za-z])")  # 大文字をつないだコード
_MIN_CODE = 6
_VERSION = re.compile(r"v?\d+(?:\.\d+){2,}")
_YEAR = re.compile(r"(?:19|20)\d{2}")
_SPLIT = re.compile(r"[\n\r\t,|、。;:；「」『』【】()\[\]{}<>\"'“”‘’=・/\\*#]+")
_PIECE_EDGE = " -+.!?"
_WIDE = re.compile(r"[^\x00-\x7f]")
_KATAKANA_ONLY = re.compile(r"[゠-ヿ ]+")
_LINE_NUMBERS = re.compile(r"^ *\d+\t", re.MULTILINE)  # read_file が付ける行番号


@dataclass(frozen=True)
class Hit:
    """質問文の中で見つかった、外に出したくない情報らしいもの。"""

    kind: str  # LOCAL / WATCH
    text: str  # 見つかった文字列（質問文の中のもの。全角・半角や大文字・小文字をそろえたあと）
    source: str  # どこにあったか（「read_file /data/customers.csv」「見張る言葉の一覧」など）

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "text": self.text, "source": self.source}

    @classmethod
    def from_dict(cls, value: Any) -> Hit | None:
        if not isinstance(value, Mapping):
            return None
        kind, text, source = value.get("kind"), value.get("text"), value.get("source")
        if kind not in (LOCAL, WATCH) or not isinstance(text, str) or not isinstance(source, str):
            return None
        return cls(kind, text[:MAX_HIT_CHARS], source[:200])


# ---------- 文字列をそろえる ----------
def normalize(text: str) -> str:
    """全角・半角、大文字・小文字、桁区切りのカンマ、空白の違いをそろえる。"""
    folded = unicodedata.normalize("NFKC", text).lower()
    folded = _THOUSANDS.sub(lambda m: m.group().replace(",", ""), folded)
    return " ".join(folded.split())


def normalize_lines(text: str) -> str:
    """normalize と同じだが、改行は残す（表の行の区切りとして使うため）。"""
    return "\n".join(normalize(line) for line in text.splitlines())


def _compact(text: str) -> str:
    return "".join(text.split())


def tokens(text: str) -> set[str]:
    """番号やコード（数字を含む、3文字以上の英数字）。text は normalize したもの。"""
    found: set[str] = set()
    for raw in _TOKEN.findall(text):
        token = raw.strip(_TOKEN_EDGE)
        if (
            len(token) >= _MIN_TOKEN
            and any(char.isdigit() for char in token)
            and not _VERSION.fullmatch(token)
            and not _YEAR.fullmatch(token)
        ):
            found.add(token)
    return found


def codes(text: str) -> set[str]:
    """大文字を - や _ でつないだコード（数字がなくてもよい）。text は normalize する前のもの。返すのは小文字。"""
    folded = unicodedata.normalize("NFKC", text)
    return {code.lower() for code in _CODE.findall(folded) if len(code) >= _MIN_CODE and not _VERSION.fullmatch(code)}


def has_token(token: str, text: str) -> bool:
    """text（normalize したもの）に、token が、英数字の切れ目で区切られて入っているか。長いものは、途中でもよい。"""
    if len(token) >= 5 and token in text:
        return True
    return re.search(rf"(?<![0-9a-z]){re.escape(token)}(?![0-9a-z])", text) is not None


def pieces(text: str) -> set[str]:
    """表の値など、区切りで区切ったかたまり（日本語を含む、4〜40 文字）。text は normalize_lines したもの。

    カタカナだけのもの（インストール、タイムアウトなど）は、よくある言葉が多いので除く。
    """
    found: set[str] = set()
    for chunk in _SPLIT.split(text):
        chunk = chunk.strip(_PIECE_EDGE)
        for piece in (chunk, *chunk.split(" ")) if " " in chunk else (chunk,):
            piece = piece.strip(_PIECE_EDGE)
            if _MIN_PIECE <= len(piece) <= _MAX_PIECE and _WIDE.search(piece) and not _KATAKANA_ONLY.fullmatch(piece):
                found.add(piece)
    return found


# ---------- 会話から、利用者の文章と手元のデータを集める ----------
def _is_summary(message: AnyMessage) -> bool:
    return isinstance(message, HumanMessage) and message.additional_kwargs.get("lc_source") == _SUMMARY_SOURCE


def user_texts(messages: Iterable[AnyMessage]) -> list[str]:
    """利用者が書いた文章（親のエージェントの会話の、要約ではない HumanMessage）。"""
    return [message.text for message in messages if isinstance(message, HumanMessage) and not _is_summary(message)]


def _label(name: str | None, args: Any) -> str:
    label = name or "ツール"
    if isinstance(args, Mapping):
        path = args.get("file_path") or args.get("path")
        if isinstance(path, str) and path:
            label += f" {' '.join(path.split())[:120]}"
    return label


def local_texts(messages: Sequence[AnyMessage], *, root: bool) -> list[tuple[str, str]]:
    """手元のデータ（どこにあったか, 文章）。Web 検索の結果は、外から来たものなので入れない。新しいものから。"""
    calls = _calls_by_id(messages)
    found = [item for message in messages if (item := _local_text(message, calls, root=root)) is not None]
    return _newest_first(found, _MAX_LOCAL_CHARS)


def _calls_by_id(messages: Sequence[AnyMessage]) -> dict[str, Mapping[str, Any]]:
    """ツールの呼び出し（ID → 呼び出し）。ツールの結果が、どの呼び出しのものかを知るため。"""
    return {str(call.get("id")): call for message in messages if isinstance(message, AIMessage) for call in message.tool_calls}


def _local_text(message: AnyMessage, calls: Mapping[str, Mapping[str, Any]], *, root: bool) -> tuple[str, str] | None:
    """1 つのメッセージのうち、手元のデータに当たるもの（どこにあったか, 文章）。当たらなければ None。"""
    if isinstance(message, HumanMessage):
        if _is_summary(message):
            return ("会話の要約", message.text)
        return None if root else ("サブエージェントへの依頼文", message.text)
    if isinstance(message, ToolMessage):
        call = calls.get(str(message.tool_call_id), {})
        name = message.name or call.get("name")
        if name == TOOL_NAME:
            return None
        text = _LINE_NUMBERS.sub("", message.text) if name == "read_file" else message.text
        return (_label(name, call.get("args")), text)
    return None


def _newest_first(found: Sequence[tuple[str, str]], limit: int) -> list[tuple[str, str]]:
    """新しいものから、合わせて limit 文字まで。"""
    kept: list[tuple[str, str]] = []
    total = 0
    for source, text in reversed(found):
        if total >= limit:
            break
        text = text[: limit - total]
        total += len(text)
        kept.append((source, text))
    return kept


# ---------- 判定 ----------
def screen(
    question: str,
    *,
    users: Sequence[str],
    local: Sequence[tuple[str, str]],
    terms: WatchTerms | None = None,
) -> list[Hit]:
    """質問文の中の、外に出したくない情報らしいもの。空なら、確かめずに検索してよい。

    調べるのは 4 つ: 見張る言葉、番号やコード、表の値、長い文。利用者が自分で書いたものは除く。
    """
    q = normalize(question)
    u = normalize("\n".join(users))
    sources = [(source, normalize(text)) for source, text in local]
    hits = [
        *_watched(q, terms),
        *_numbers_and_codes(question, q, u, sources),
        *_table_values(q, u, local),
        *_long_passages(q, u, sources),
    ]
    return _distinct(hits)[:MAX_HITS]


def _hit(kind: str, text: str, source: str) -> Hit:
    return Hit(kind, text[:MAX_HIT_CHARS], source)


def _watched(q: str, terms: WatchTerms | None) -> list[Hit]:
    """見張る言葉（顧客の名前など）。一覧を読めなかったときも、確かめる側に倒す。"""
    if terms is None:
        return []
    hits = [_hit(WATCH, "（一覧を読めませんでした）", f"見張る言葉の一覧（{terms.error}）")] if terms.error else []
    return hits + [_hit(WATCH, term, "見張る言葉の一覧") for term in terms.find(q)]


def _numbers_and_codes(question: str, q: str, u: str, sources: Sequence[tuple[str, str]]) -> list[Hit]:
    """番号やコード: 利用者が書いていないのに、手元のデータにあるもの。長いものから。"""
    hits: list[Hit] = []
    for token in sorted(tokens(q) | codes(question), key=len, reverse=True):
        if has_token(token, u):
            continue
        source = next((source for source, text in sources if has_token(token, text)), None)
        if source is not None:
            hits.append(_hit(LOCAL, token, source))
    return hits


def _table_values(q: str, u: str, local: Sequence[tuple[str, str]]) -> list[Hit]:
    """表の値: 手元の表の値が、そのまま質問文にあるもの（利用者が書いたものは除く）。"""
    uc = _compact(u)
    return [
        _hit(LOCAL, piece, source)
        for source, text in local
        for piece in sorted(pieces(normalize_lines(text)), key=len, reverse=True)
        if piece in q and piece not in u and _compact(piece) not in uc
    ]


def _long_passages(q: str, u: str, sources: Sequence[tuple[str, str]]) -> list[Hit]:
    """長い文: 手元のデータの文が、続けて質問文にあるもの（空白を除いて比べる。利用者が書いたものは除く）。"""
    qc, uc = _compact(q), _compact(u)
    compacts = [(source, _compact(text)) for source, text in sources]
    joined = "\x00".join(text for _, text in compacts)
    places = [i for i, char in enumerate(q) if not char.isspace()]  # 空白を除いた位置 → 質問文の中の位置
    hits: list[Hit] = []
    start = 0
    while start + _WINDOW <= len(qc):
        window = qc[start : start + _WINDOW]
        if not (_WIDE.search(window) and window not in uc and window in joined):
            start += 1
            continue
        end = start + _WINDOW
        while end < len(qc) and qc[start : end + 1] in joined:
            end += 1
        run = qc[start:end]
        shown = q[places[start] : places[end - 1] + 1]  # 画面には、質問文のとおりの空白で出す
        hits.append(_hit(LOCAL, shown, next((source for source, text in compacts if run in text), "手元のデータ")))
        start = end
    return hits


def _distinct(hits: list[Hit]) -> list[Hit]:
    """同じものを2回出さない。ほかの見つかったものの一部でしかないものも、出さない（長いほうを残す）。"""
    kept: list[Hit] = []
    for hit in hits:
        key = _compact(hit.text)
        if any(key == _compact(other.text) for other in kept):
            continue
        if any(key != _compact(other.text) and key in _compact(other.text) for other in hits):
            continue
        kept.append(hit)
    return kept


# ---------- エージェントへの組み込み ----------
class ScreenState(AgentState):
    """会話の状態に、利用者が書いた文章を持てるようにする（サブエージェントに引き継ぐため）。"""

    screen_user_texts: NotRequired[list[str]]


class SearchScreenMiddleware(AgentMiddleware):
    """モデルが Web 検索を出したら、その場で質問文を調べて、結果を返事に記録する。

    root は親のエージェントかどうか。親では、利用者が書いた文章を会話の状態に入れ、サブエージェントに引き継ぐ。
    サブエージェントでは、引き継いだ文章だけを、利用者が書いたものとして扱う。
    """

    state_schema = ScreenState

    def __init__(
        self,
        terms: Callable[[], WatchTerms],
        *,
        root: bool,
        screened: Mapping[str, Sequence[str]] | None = None,
    ) -> None:
        """screened は、調べるツールと、調べる引数（空なら、すべての引数）。省略すると、Web 検索の質問文だけ。"""
        super().__init__()
        self._terms = terms
        self._root = root
        self._screened = {TOOL_NAME: ("question",)} if screened is None else dict(screened)

    def before_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        if not self._root:
            return None
        texts = user_texts(state.get("messages", []))
        return None if state.get(USER_TEXTS_KEY) == texts else {USER_TEXTS_KEY: texts}

    async def abefore_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        return self.before_model(state, runtime)

    def wrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]
    ) -> ModelResponse | AIMessage:
        return self._record(request, handler(request))

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelResponse | AIMessage:
        return self._record(request, await handler(request))

    def _record(self, request: ModelRequest, response: Any) -> Any:
        if isinstance(response, AIMessage):
            return self._annotate(request, response)
        if isinstance(response, ModelResponse):
            result = [self._annotate(request, m) if isinstance(m, AIMessage) else m for m in response.result]
            if any(new is not old for new, old in zip(result, response.result, strict=True)):
                return ModelResponse(result=result, structured_response=response.structured_response)
            return response
        inner = getattr(response, "model_response", None)  # ExtendedModelResponse（状態の更新を伴う返事）
        if isinstance(inner, ModelResponse):
            updated = self._record(request, inner)
            if updated is not inner:
                return type(response)(model_response=updated, command=getattr(response, "command", None))
        return response

    def _annotate(self, request: ModelRequest, message: AIMessage) -> AIMessage:
        calls = [call for call in message.tool_calls if call.get("name") in self._screened]
        if not calls:
            return message
        users = user_texts(request.messages) if self._root else list((request.state or {}).get(USER_TEXTS_KEY) or [])
        local = local_texts(request.messages, root=self._root)
        terms = self._terms()
        record: dict[str, list[dict[str, str]]] = {}
        for call in calls:
            name = str(call.get("name"))
            text = sent_text(call.get("args") or {}, self._screened[name])
            hits = screen(text, users=users, local=local, terms=terms)
            record[str(call.get("id"))] = [hit.as_dict() for hit in hits]
            # 見つけた文字列は、記録に残さない（顧客の情報そのものなので）。数と種類だけ
            audit(
                "web_search_screen",
                flagged=bool(hits),
                local=sum(hit.kind == LOCAL for hit in hits),
                watch=sum(hit.kind == WATCH for hit in hits),
                subagent=not self._root or None,
                tool=None if name == TOOL_NAME else name,
            )
        metadata = {**message.response_metadata, RECORD_KEY: record}
        return message.model_copy(update={"response_metadata": metadata})


def sent_text(args: Mapping[str, Any], names: Sequence[str]) -> str:
    """ツールの引数のうち、調べるもの（names。空なら、すべて）を、1 つの文章にする。文字列でない値は JSON にする。"""
    keys = list(names) or list(args)
    parts = []
    for key in keys:
        value = args.get(key)
        if value is None:
            continue
        parts.append(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
    return "\n".join(parts)


def recorded(messages: Sequence[Any], tool_call_id: str | None) -> list[Hit] | None:
    """会話の中の、この検索の判定の記録。見つからない・読めないときは None（確かめる側に倒す）。"""
    for message in reversed(messages):
        if not isinstance(message, AIMessage) or not any(call.get("id") == tool_call_id for call in message.tool_calls):
            continue
        record = (message.response_metadata or {}).get(RECORD_KEY)
        entries = record.get(str(tool_call_id)) if isinstance(record, Mapping) else None
        if not isinstance(entries, list):
            return None
        hits = [Hit.from_dict(entry) for entry in entries]
        return None if any(hit is None for hit in hits) else [hit for hit in hits if hit is not None]
    return None
