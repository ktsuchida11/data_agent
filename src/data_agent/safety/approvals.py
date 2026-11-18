"""確認（承認）の決まり。エージェントが操作を実行する前に、利用者に確かめるかどうかを決める。

確かめる操作は、次の5種類。
- protected : 権限の決まり（agent.PERMISSIONS）で「承認が必要」とした場所への書き込み（/memories/）。必ず確かめる。
- always    : 取り消せない操作（ファイルの削除）。必ず確かめる。
- configured: config/security.yaml で、ツールごとに require_approval: true としたもの。
- suspicious: require_approval: auto としたツール（Web 検索など）で、送る内容に、外に出したくない情報らしいものが
              見つかったもの（search_screen.py）。判定は、モデルがツールを呼んだ時点で記録したものを読む。
              記録が見つからないときは、確かめる。
- strict    : 確認のしかたが「毎回確認」のチャットでの、ファイルの作成と書き換えと、ツールの扱いで strict と
              したもの（コードの実行、データベースの問い合わせ、Web 検索、パックのツール。tools/registry.py）。

どのツールをどう確かめるかは、ツールの扱い（tools/registry.py の ToolPolicy）から、agent.py が渡す。

確認のしかた（標準 / 毎回確認）は、チャットごとに選べる。選んだものは、会話の状態（approval_mode）に入れて
エージェントに渡す。状態は会話と一緒に保存されるので、承認を待って止まったあとで再開したときにも、
同じ判定になる。判定が途中で変わると、利用者が拒否した操作が実行されてしまうことがある
（LangChain の HumanInTheLoopMiddleware の when の注意書き）。設定の値をその場で読む作りにはしない。

サブエージェントにも、同じ決まりと状態が引き継がれる。

「このチャットでは以後確認しない」は、ここでは扱わない。エージェントは毎回止まり、画面の側
（ui/app.py）が、利用者に聞かずに承認を返す。protected と always には使えない。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, NotRequired

from deepagents import FilesystemPermission
from deepagents.backends.protocol import BackendProtocol
from deepagents.backends.utils import validate_path

# 権限の決まりから「承認が必要な呼び出しか」を判定する関数は、deepagents の内部のものを使う
# （リンクの先や、フォルダごとの操作も見てくれる）。deepagents を上げたときは tests/test_approvals.py で確かめる
from deepagents.middleware._fs_interrupt import _build_interrupt_on_from_permissions
from deepagents.middleware.filesystem import _check_fs_permission
from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware, InterruptOnConfig, ToolCallRequest

from data_agent.safety import search_screen
from data_agent.safety.search_screen import Hit

STANDARD = "standard"
STRICT = "strict"
MODES: tuple[str, ...] = (STANDARD, STRICT)
MODE_LABELS: dict[str, str] = {
    STANDARD: "標準（削除などだけ確認）",
    STRICT: "毎回確認（書き込み・実行・検索も確認）",
}
STATE_KEY = "approval_mode"  # 会話の状態に入れる名前

PROTECTED = "protected"
ALWAYS = "always"
CONFIGURED = "configured"
SUSPICIOUS = "suspicious"

# 必ず確かめるツール（取り消せない操作）
ALWAYS_TOOLS = frozenset({"delete"})
# 「毎回確認」のときに確かめる、ファイルのツール。独自のツール（コードの実行など）は、agent.py が足す
STRICT_FILE_TOOLS = frozenset({"write_file", "edit_file"})

_REASONS: dict[str, str] = {
    PROTECTED: "長期メモは、以後のすべてのチャットで、エージェントへの指示として読み込まれます。内容を確かめてください。",
    ALWAYS: "削除は取り消せません。",
    CONFIGURED: "設定（config/security.yaml）で、この操作は毎回確かめることになっています。",
    SUSPICIOUS: (
        "質問文に、外に出したくない情報らしいものがあります（下の「見つかったもの」）。"
        "質問文は、検索のために外部に送られます。送ってよいかを確かめてください。"
    ),
    STRICT: "このチャットは「毎回確認」になっています。",
}
# Web 検索のほかのツールで、送る内容に外に出したくない情報らしいものがあったとき
_SUSPICIOUS_OTHER = (
    "送る内容に、外に出したくない情報らしいものがあります（下の「見つかったもの」）。"
    "この内容は、ツールの送り先に送られます。送ってよいかを確かめてください。"
)
_DETAIL_HEAD = 6_000  # 画面に出す内容の長さ（先頭）
_DETAIL_TAIL = 2_000  # 長いときに、末尾から出す長さ
_PATH_ARGS: dict[str, str] = {"write_file": "file_path", "edit_file": "file_path", "delete": "file_path"}
# 止まったときの説明（HITL の description）に、判定の記録を書き添える印。画面は、ここから読み戻す
# （画面が見られる会話の状態は、親のものだけで、サブエージェントの返事は入っていないため）
_HITS_MARK = "\n<!-- web_search_screening: "
_HITS_END = " -->"


class ApprovalState(AgentState):
    """会話の状態に、確認のしかたを足す。"""

    approval_mode: NotRequired[str]


class ApprovalStateMiddleware(AgentMiddleware):
    """会話の状態に approval_mode を持てるようにする。親にも、サブエージェントにも付ける。"""

    state_schema = ApprovalState


@dataclass(frozen=True)
class Review:
    """利用者に確かめる操作。"""

    name: str  # ツールの名前
    kind: str  # protected / always / configured / suspicious / strict
    title: str  # 何をするか（1行）
    reason: str  # なぜ確かめるか（1行）
    detail: str  # 中身（Markdown。コード、SQL、書き込む内容など）
    can_remember: bool  # 「このチャットでは以後確認しない」を選べるか


class ApprovalPolicy:
    """どの操作を確かめるか。エージェント（止まるかどうか）と、画面（どう見せるか）の両方で使う。"""

    def __init__(
        self,
        *,
        permissions: list[FilesystemPermission],
        backend: BackendProtocol | None,
        configured: Iterable[str] = (),
        screened: Iterable[str] = (),
        strict: Iterable[str] = STRICT_FILE_TOOLS,
        labels: Mapping[str, str] | None = None,
        default_mode: str = STANDARD,
        allow_remember: bool = True,
    ) -> None:
        if default_mode not in MODES:
            raise ValueError(f"approvals.default_mode は {' / '.join(MODES)} のどれかにしてください: {default_mode}")
        self._permissions = permissions
        self._protected: dict[str, Callable[[ToolCallRequest], bool]] = {
            name: config["when"]
            for name, config in _build_interrupt_on_from_permissions(permissions, backend).items()
            if "when" in config
        }
        self._configured = frozenset(configured)
        self._screened = frozenset(screened) - self._configured
        self._strict = frozenset(strict)
        self._labels = {**_TOOL_LABELS, **(labels or {})}  # 確認の画面に出す、ツールの名前
        self.default_mode = default_mode
        self.allow_remember = allow_remember

    @property
    def configured(self) -> frozenset[str]:
        """設定（config/security.yaml の require_approval: true）で、どのチャットでも確かめるツール。"""
        return self._configured

    @property
    def screened(self) -> frozenset[str]:
        """設定（require_approval: auto）で、中身を調べて、怪しいときだけ確かめるツール。"""
        return self._screened

    @property
    def tools(self) -> frozenset[str]:
        """確かめることがあるツール。"""
        return frozenset(self._protected) | ALWAYS_TOOLS | self._configured | self._screened | self._strict

    def mode_of(self, state: Any) -> str:
        """会話の状態から、確認のしかたを取り出す。入っていなければ既定のもの。"""
        value = state.get(STATE_KEY) if isinstance(state, Mapping) else None
        return value if value in MODES else self.default_mode

    def kind(self, name: str, args: Mapping[str, Any], mode: str, hits: Sequence[Hit] | None = ()) -> str | None:
        """確かめる理由の種類。確かめなくてよければ None。

        hits は、中身を調べるツールの判定の記録（search_screen）。None は、記録が見つからないこと（確かめる）。
        """
        protected = self._protected.get(name)
        if protected is not None and protected(_request(name, args)):
            return PROTECTED
        if self._denied(name, args):
            return None  # 権限の決まりで、ツールが自分で断る。先に確かめても意味がない
        if name in ALWAYS_TOOLS:
            return ALWAYS
        if name in self._configured:
            return CONFIGURED
        if name in self._screened and (hits is None or hits):
            return SUSPICIOUS
        if mode == STRICT and name in self._strict:
            return STRICT
        return None

    def review(self, name: str, args: Mapping[str, Any], mode: str, hits: Sequence[Hit] | None = ()) -> Review | None:
        """画面に出す内容を作る。確かめなくてよい操作なら None。"""
        kind = self.kind(name, args, mode, hits)
        if kind is None:
            return None
        title, detail = describe(name, args, protected=kind == PROTECTED, label=self._labels.get(name))
        reason = _REASONS[kind]
        if kind == SUSPICIOUS:
            detail += "\n\n" + describe_hits(hits)
            if name != search_screen.TOOL_NAME:
                reason = _SUSPICIOUS_OTHER
        # 「以後確認しない」は、怪しいものには出さない（押すと、以後は中身を見ずに外へ送ることになるため）
        can_remember = self.allow_remember and kind in (CONFIGURED, STRICT)
        return Review(name, kind, title, reason, detail, can_remember)

    def review_action(self, action: Mapping[str, Any], mode: str) -> Review | None:
        """止まった操作（HITL の action_requests の1つ）から、画面に出す内容を作る。"""
        name, args = str(action.get("name")), action.get("args") or {}
        hits = hits_in(str(action.get("description") or "")) if name in self._screened else ()
        return self.review(name, args, mode, hits)

    def interrupt_on(self) -> dict[str, InterruptOnConfig]:
        """create_deep_agent の interrupt_on に渡す形。

        権限の決まりから自動で作られる分（/memories/ への書き込み）も、ここに含めてある
        （同じツールの名前で渡すと、自動で作られる分は使われなくなるため）。
        画面が返すのは「承認」と「拒否」だけなので、それ以外の答え（引数の書き換えなど）は受け付けない。
        """
        return {
            name: InterruptOnConfig(
                allowed_decisions=["approve", "reject"],
                when=self._when,
                description=self._description,
            )
            for name in sorted(self.tools)
        }

    def unattended_note(self) -> str:
        """無人の実行（定時実行）で、自動で拒否される操作の説明。"""
        blocked = ["ファイルの削除", "メモリへの書き込み"]
        names = self._configured | (self._strict if self.default_mode == STRICT else frozenset())
        blocked += [label for name, label in _TOOL_LABELS.items() if name in names]
        blocked += [self._labels.get(name, name) for name in sorted(names - set(_TOOL_LABELS))]
        blocked += [
            f"{self._labels.get(name, name)}（外に出したくない情報らしいものを含むとき）"
            for name in sorted(self._screened - names)
        ]
        return "、".join(dict.fromkeys(blocked))

    def _description(self, tool_call: Mapping[str, Any], state: Any, runtime: Any) -> str:
        """止まったときに、操作に付ける説明（1行）。画面は、これを使わずに review() で作り直す。"""
        name, args = tool_call["name"], tool_call.get("args") or {}
        hits = self._hits(state, tool_call)
        review = self.review(name, args, self.mode_of(state), hits)
        title = review.title if review else describe(name, args)[0]
        if name in self._screened:
            title += encode_hits(hits)
        return title

    def _when(self, request: ToolCallRequest) -> bool:
        call = request.tool_call
        hits = self._hits(request.state, call)
        return self.kind(call["name"], call.get("args") or {}, self.mode_of(request.state), hits) is not None

    def _hits(self, state: Any, tool_call: Mapping[str, Any]) -> list[Hit] | None:
        """中身を調べるツールなら、会話の中の判定の記録を読む。それ以外のツールは、空。"""
        if tool_call.get("name") not in self._screened:
            return []
        messages = state.get("messages", []) if isinstance(state, Mapping) else []
        return search_screen.recorded(messages, tool_call.get("id"))

    def _denied(self, name: str, args: Mapping[str, Any]) -> bool:
        path = args.get(_PATH_ARGS.get(name, ""))
        if not isinstance(path, str):
            return False
        try:
            return _check_fs_permission(self._permissions, "write", validate_path(path)) == "deny"
        except ValueError:
            return False


_TOOL_LABELS: dict[str, str] = {
    "write_file": "ファイルの作成",
    "edit_file": "ファイルの書き換え",
    "run_python": "コードの実行",
    "query_database": "データベースの問い合わせ",
    "web_search": "Web 検索",
}


def describe_hits(hits: Sequence[Hit] | None) -> str:
    """見つかったものの一覧（Markdown）。値は、呼び出す側でマスクしてから画面に出すこと。"""
    if hits is None:
        return "見つかったもの:\n- 判定の記録が見つかりませんでした。念のため確かめます。"
    lines = ["見つかったもの:"]
    for hit in hits:
        if hit.kind == search_screen.WATCH:
            lines.append(f"- {inline(hit.text)}: {inline(hit.source)}にある言葉です")
        else:
            lines.append(
                f"- {inline(hit.text)}: このチャットで読んだもの（{inline(hit.source)}）にあり、利用者の発言にはありません"
            )
    return "\n".join(lines)


def encode_hits(hits: Sequence[Hit] | None) -> str:
    """判定の記録を、止まったときの説明に書き添える形にする。"""
    value = None if hits is None else [hit.as_dict() for hit in hits]
    # 中身は JSON。説明の終わりの印（-->）が中に現れないように、> を書き換えておく
    return _HITS_MARK + json.dumps(value, ensure_ascii=False).replace(">", "\\u003e") + _HITS_END


def hits_in(description: str) -> list[Hit] | None:
    """止まったときの説明から、判定の記録を読み戻す。読めなければ None（確かめる側に倒す）。"""
    start = description.rfind(_HITS_MARK)
    if start < 0 or not description.endswith(_HITS_END):
        return None
    try:
        value = json.loads(description[start + len(_HITS_MARK) : -len(_HITS_END)])
    except ValueError:
        return None
    if not isinstance(value, list):
        return None
    hits = [Hit.from_dict(entry) for entry in value]
    return None if any(hit is None for hit in hits) else [hit for hit in hits if hit is not None]


def _request(name: str, args: Mapping[str, Any]) -> ToolCallRequest:
    """名前と引数だけから、判定用の呼び出しを作る（権限の判定は、引数しか見ない）。"""
    call: Any = {"name": name, "args": dict(args), "id": "", "type": "tool_call"}
    return ToolCallRequest(tool_call=call, tool=None, state={}, runtime=None)  # type: ignore[arg-type]


def fenced(text: str, language: str = "") -> str:
    """文章を、コードの枠に入れる。中に枠の記号（```）があっても、枠が途中で閉じないようにする。

    枠が途中で閉じると、続きが画面の文章として表示され、確認の画面に偽の説明を書き込める。
    """
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{language}\n{text}\n{fence}"


def inline(text: str) -> str:
    """1行の説明の中に、値（ファイルの場所など）を入れる。改行や記号で、説明の続きを書き足せないようにする。"""
    flat = " ".join(text.split())[:300]
    longest = max((len(run) for run in re.findall(r"`+", flat)), default=0)
    mark = "`" * (longest + 1)
    pad = " " if longest else ""  # 中に記号があるときは、前後に空白を入れる決まり
    return f"{mark}{pad}{flat}{pad}{mark}"


def clip(text: str) -> str:
    """長い内容は、先頭と末尾だけにする。省いたことと、全体の長さを必ず書く。"""
    if len(text) <= _DETAIL_HEAD + _DETAIL_TAIL:
        return text
    omitted = len(text) - _DETAIL_HEAD - _DETAIL_TAIL
    return (
        f"{text[:_DETAIL_HEAD]}\n"
        f"…（全 {len(text):,} 文字のうち、途中の {omitted:,} 文字を省略。ここは表示されていません）…\n"
        f"{text[-_DETAIL_TAIL:]}"
    )


def describe(name: str, args: Mapping[str, Any], *, protected: bool = False, label: str | None = None) -> tuple[str, str]:
    """操作の説明（1行）と、中身（Markdown）を作る。値は、呼び出す側でマスクしてから画面に出すこと。

    label は、ツールの扱い（tools/registry.py）に書いた名前。パックのツールの説明に使う。
    """

    def text(key: str) -> str:
        value = args.get(key)
        return value if isinstance(value, str) else ("" if value is None else json.dumps(value, ensure_ascii=False))

    path = inline(text("file_path"))
    if name == "delete":
        return f"ファイルを削除します: {path}", ""
    if name == "write_file":
        content = text("content")
        what = "長期メモに書き込みます" if protected else "ファイルを書きます"
        return f"{what}: {path}（{len(content):,} 文字）", fenced(clip(content))
    if name == "edit_file":
        what = "長期メモを書き換えます" if protected else "ファイルを書き換えます"
        scope = "（当てはまる箇所をすべて）" if args.get("replace_all") else ""
        detail = f"変更前{scope}:\n{fenced(clip(text('old_string')))}\n変更後:\n{fenced(clip(text('new_string')))}"
        return f"{what}: {path}", detail
    if name == "run_python":
        files = args.get("input_files") or []
        detail = fenced(clip(text("code")), "python")
        if files:
            detail += "\n渡すファイル: " + "、".join(inline(str(item)) for item in files[:20])
        return "Python のコードを実行します", detail
    if name == "query_database":
        return "データベースに問い合わせます", fenced(clip(text("sql")), "sql")
    if name == "web_search":
        return "Web を検索します（検索のたびに料金がかかり、質問文が外部に送られます）", fenced(clip(text("question")))
    head = f"{label}（{name}）を実行します" if label and label != name else f"{name} を実行します"
    return head, fenced(clip(json.dumps(dict(args), ensure_ascii=False, indent=2)), "json")
