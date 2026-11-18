"""Web 検索の質問文の判定（search_screen.py）と、確認の仕組みへの組み込み（approvals.py）。

エージェントに組み込んだときの動きは、LangChain の create_agent に、同じミドルウェアと確認の決まりを渡して確かめる
（agent.py は ChatAnthropic を import するため、ここでは読み込まない）。
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from fake_model import FakeModel
from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from data_agent.safety import approvals, search_screen, watch_terms
from data_agent.safety.approvals import STANDARD, STRICT, SUSPICIOUS, ApprovalPolicy
from data_agent.safety.search_screen import LOCAL, WATCH, Hit, screen
from data_agent.safety.watch_terms import WatchTerms, WatchTermsFile

SECRET_FILE = "# 来期の見通し（社外秘）\n\n- 未公表の取引先のコード: AOBA-7Q4KX\n- 来期の売上目標: 4 億 8,200 万円\n- 仕入れ単価の上限: 1 個 318 円\n"
CUSTOMERS = "id,name,plan\n1,みどり商会,standard\n2,あおぞら工業,premium\n3,ひかり物産,standard\n"
README = (
    "# 在庫アプリ\n\n## インストール\n\n`uv sync` で入れます。chainlit 2.12.0 と langchain 1.4.7 を使います（2026 年 11 月）。\n"
)


def numbered(text: str) -> str:
    """read_file の返し方（行番号とタブ）。"""
    return "\n".join(f"{i:6d}\t{line}" for i, line in enumerate(text.split("\n"), 1))


def conversation(*steps: tuple[str, dict, str], user: str = "依頼です") -> list:
    """利用者の発言のあとに、ツールの呼び出しと結果が続く会話。steps は（ツール, 引数, 結果）。"""
    messages: list = [HumanMessage(user)]
    for name, args, result in steps:
        call_id = f"call-{uuid.uuid4().hex[:6]}"
        messages.append(AIMessage("", tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}]))
        messages.append(ToolMessage(result, tool_call_id=call_id, name=name))
    return messages


def check(question: str, messages: list, *, root: bool = True, inherited: list[str] | None = None, terms=None) -> list[Hit]:
    users = search_screen.user_texts(messages) if root else (inherited or [])
    local = search_screen.local_texts(messages, root=root)
    return screen(question, users=users, local=local, terms=terms)


# ---------- 手元のデータの写し ----------
def test_csv_commas_are_not_thousands_separators():
    """CSV の区切りのカンマ（1184200,212）を、桁区切り（1,184,200）と取り違えない。取り違えると、件数の列を写した検索を見逃す。"""
    sales = "月,地域,売上,件数\n2026-10,東京,1284600,231\n2026-10,大阪,903500,164\n"
    messages = conversation(("read_file", {"file_path": "/data/sales.csv"}, numbered(sales)))
    assert [hit.text for hit in check("取引 231 件 相場", messages)] == ["231"]
    assert search_screen.normalize("売上は 1,284,600 円、件数は 1284600,231") == "売上は 1284600 円、件数は 1284600,231"


def test_a_code_copied_from_a_file_is_found():
    """仕込まれた指示に従って、社外秘のファイルのコードを検索に書いた場合（点検の攻撃と同じ形）。"""
    messages = conversation(("read_file", {"file_path": "/finance/forecast.md"}, numbered(SECRET_FILE)))
    hits = check("AOBA-7Q4KX 取引先 評判", messages)
    assert hits == [Hit(LOCAL, "aoba-7q4kx", "read_file /finance/forecast.md")]
    assert check("7Q4KX 会社", messages)[0].text == "7q4kx"  # 一部だけでも


def test_codes_without_digits_are_found():
    """点検の社外秘の値は、毎回変わる。数字を含まないこともある（AOBA-BNWQX など）。"""
    messages = conversation(("read_file", {"file_path": "/finance/forecast.md"}, numbered(SECRET_FILE.replace("7Q4KX", "BNWQX"))))
    assert [hit.text for hit in check("AOBA-BNWQX 取引先", messages)] == ["aoba-bnwqx"]
    # 小文字の語をつないだもの（パッケージの名前など）は、コードとみなさない
    deps = conversation(
        ("read_file", {"file_path": "/pyproject.toml"}, numbered('dependencies = ["langchain-anthropic", "pytest-asyncio"]'))
    )
    assert check("langchain-anthropic 最新版", deps) == []


def test_numbers_copied_from_a_file_are_found_even_when_written_differently():
    messages = conversation(("read_file", {"file_path": "/finance/forecast.md"}, numbered(SECRET_FILE)))
    assert [hit.text for hit in check("売上 ８２００万円 業界の平均", messages)] == ["8200"]  # 全角、桁区切りなし
    assert [hit.text for hit in check("単価 318円 相場", messages)] == ["318"]


def test_what_the_user_wrote_is_not_counted_as_a_copy():
    messages = conversation(
        ("read_file", {"file_path": "/finance/forecast.md"}, numbered(SECRET_FILE)),
        user="AOBA-7Q4KX という取引先を調べて",
    )
    assert check("AOBA-7Q4KX 取引先 評判", messages) == []


def test_a_customer_name_from_a_csv_file_is_found():
    messages = conversation(("read_file", {"file_path": "/data/customers.csv"}, numbered(CUSTOMERS)))
    hits = check("みどり商会 評判 口コミ", messages)
    assert hits == [Hit(LOCAL, "みどり商会", "read_file /data/customers.csv")]


def test_a_customer_name_from_a_database_result_is_found():
    result = "4 行、3 列。保存先: /outputs/x/customers.csv\n列: id（int4）、name（text）、plan（text）\n" + CUSTOMERS
    messages = conversation(("query_database", {"sql": "SELECT * FROM customers"}, result))
    hits = check("あおぞら工業 与信", messages)
    assert hits == [Hit(LOCAL, "あおぞら工業", "query_database")]


def test_a_long_sentence_copied_from_a_file_is_found():
    note = "議事録\n新しい在庫管理の画面を12月の第2週に社内公開する方針で合意した\n"
    messages = conversation(("read_file", {"file_path": "/notes/meeting.md"}, numbered(note)))
    hits = check("新しい在庫管理の画面を12月の第2週に社内公開 競合の動き", messages)
    assert len(hits) == 1 and hits[0].text.startswith("新しい在庫管理の画面を12月の第2週に社内公開")


def test_ordinary_questions_after_reading_files_are_not_flagged():
    """よくある検索。版の番号、西暦、カタカナだけの言葉、短い言葉は、写しとみなさない。"""
    messages = conversation(("read_file", {"file_path": "/README.md"}, numbered(README)))
    for question in (
        "chainlit インストール 方法",
        "langchain 1.4.7 リリースノート",
        "2026 年 Python の新機能",
        "uv sync の使い方",
    ):
        assert check(question, messages) == [], question
    # 分かっている空振り: ファイルの見出し（4 文字以上で、漢字やひらがなを含むもの）を、そのまま質問に使うと、確かめる
    assert [h.text for h in check("在庫アプリ 作り方", messages)] == ["在庫アプリ"]


def test_web_search_results_are_not_counted_as_local_data():
    """Web 検索の結果は、もともと外にあったもの。そこにある番号で、もう一度検索してよい。"""
    messages = conversation(("web_search", {"question": "Python 最新版"}, "Python 3.13 が出ました（PEP 719）"))
    assert check("PEP 719 の内容", messages) == []


def test_line_numbers_of_read_file_are_not_counted():
    long_file = "\n".join("同じ行" for _ in range(600))  # 行番号は 600 まで付くが、中身に数字はない
    messages = conversation(("read_file", {"file_path": "/big.txt"}, numbered(long_file)))
    assert check("HTTP 404 の意味", messages) == []


def test_questions_measured_with_real_models_are_not_flagged_without_local_reads():
    """2026-11-15 の測定で、3 つのモデルが実際に出した検索（手元のファイルを読む前のもの）。"""
    messages = [HumanMessage("Python の venv で仮想環境を作る手順を、Web で調べて教えてください。")]
    for question in (
        "Python の標準モジュール venv で仮想環境を作成・有効化・無効化する手順（Windows / macOS / Linux それぞれのコマンド）を Python 公式ドキュメントで確認したい",
        "pytest で特定のテストだけを実行する方法（ファイル指定、ノードID指定 file::Class::test、-k によるキーワード指定、-m によるマーカー指定、--lf 前回失敗分の再実行など）を公式ドキュメントで確認したい",
        "Git で直前のコミットのメッセージを修正する方法（git commit --amend）と、すでに push 済みの場合の注意点を知りたい",
    ):
        assert check(question, messages) == []


# ---------- 会話の要約と、サブエージェント ----------
def test_a_summary_of_the_conversation_counts_as_local_data_not_as_the_user():
    summary = HumanMessage(
        "Here is a summary of the conversation to date:\n\n取引先のコードは AOBA-7Q4KX だった。",
        additional_kwargs={"lc_source": "summarization"},
    )
    messages = [summary, HumanMessage("続けて")]
    assert search_screen.user_texts(messages) == ["続けて"]
    assert [hit.source for hit in check("AOBA-7Q4KX 評判", messages)] == ["会話の要約"]


def test_in_a_subagent_the_task_description_counts_as_local_data():
    """親のモデルが書いた依頼文には、読んだ内容の写しが入りうる。利用者が書いた文章は、親から引き継いだものだけ。"""
    messages = [HumanMessage("取引先 AOBA-7Q4KX の評判を Web で調べて")]
    hits = check("AOBA-7Q4KX 評判", messages, root=False, inherited=["取引先の評判を調べて"])
    assert [hit.source for hit in hits] == ["サブエージェントへの依頼文"]
    # 利用者が自分で書いたコードなら、写しではない
    assert check("AOBA-7Q4KX 評判", messages, root=False, inherited=["AOBA-7Q4KX の評判を調べて"]) == []


# ---------- 見張る言葉 ----------
def test_watch_terms_are_found_even_when_the_user_wrote_them():
    terms = WatchTerms.parse("# 顧客\nみどり商会\nHikari Bussan\nab\n")
    messages = [HumanMessage("みどり商会 の評判を調べて")]
    assert check("みどり 商会 評判", messages, terms=terms) == [Hit(WATCH, "みどり商会", "見張る言葉の一覧")]
    assert [h.text for h in check("ＨＩＫＡＲＩ　ＢＵＳＳＡＮ reviews", messages, terms=terms)] == ["hikari bussan"]
    assert check("hikari bussanx", messages, terms=terms) == []  # 英数字は、語の切れ目で比べる
    assert terms.ascii == ("hikari bussan",)  # 短すぎる言葉（ab）は読まない


def test_watch_terms_file(tmp_path: Path):
    path = tmp_path / "watch_terms.txt"
    terms = WatchTermsFile(path)
    assert terms() == WatchTerms()  # ファイルがなければ、空
    path.write_text("みどり商会\n", encoding="utf-8")
    assert terms().wide == ("みどり商会",)
    path.write_text("あおぞら工業\nひかり物産\n", encoding="utf-8")
    assert terms().wide == ("あおぞら工業", "ひかり物産")  # 書き換えたら、読み直す
    path.write_bytes(b"\xff\xfe broken")
    broken = terms()
    assert broken.error == "ファイル: UnicodeDecodeError"
    # 読めないときは、どの検索も確かめる
    assert [hit.kind for hit in screen("venv 作り方", users=[], local=[], terms=broken)] == [WATCH]


def test_company_kinds_are_also_watched_without_them():
    """顧客の一覧には「株式会社〜」とあっても、質問では会社の種類を書かないことが多い。"""
    terms = WatchTerms.of(["株式会社はまなす商事", "㈲ひばり食品", "株式会社日本", "Acme Co., Ltd.", "山田 太郎", "#9 ストア"])
    messages = [HumanMessage("調べて")]
    assert [h.text for h in check("はまなす商事 評判", messages, terms=terms)] == ["はまなす商事"]
    assert [h.text for h in check("ひばり食品 口コミ", messages, terms=terms)] == ["ひばり食品"]
    assert [h.text for h in check("acme ltd 決算", messages, terms=terms)] == ["acme"]
    assert [h.text for h in check("山田太郎 経歴", messages, terms=terms)] == ["山田太郎"]
    assert check("日本の天気", messages, terms=terms) == []  # 除いた形が短すぎるもの（日本）は見張らない
    assert "#9ストア" in terms.wide  # データベースの値は、# で始まっても説明ではない
    assert WatchTerms.of(["名" * 201]) == WatchTerms()  # 長すぎる値は、名前ではないとみなす


def test_many_watch_terms_are_found_like_a_plain_search():
    """数万件の言葉を、頭の文字で引いて探す。1つずつ比べたときと同じものが見つかる。"""
    import random
    import re

    rng = random.Random(7)
    wide = ["".join(rng.choice("山田佐藤鈴木花子太郎商事物産") for _ in range(rng.randint(2, 5))) for _ in range(3_000)]
    plain = ["".join(rng.choice("abcde-. ") for _ in range(rng.randint(3, 7))).strip() for _ in range(3_000)]
    terms = WatchTerms.of([*wide, *plain, "@acme", "acme inc."])
    for _ in range(15):
        q = search_screen.normalize(
            " ".join(rng.choice([*wide[:300], *plain[:300], "の評判", "@acme", "xacme", "acme"]) for _ in range(12))
        )
        compact = re.sub(r"\s+", "", q)
        expected = sorted(t for t in terms.wide if t in compact) + sorted(
            t for t in terms.ascii if re.search(rf"(?<![0-9a-z]){re.escape(t)}(?![0-9a-z])", q)
        )
        assert terms.find(q) == expected


def test_refreshing_terms_reads_again_and_fails_closed(monkeypatch):
    now = [0.0]
    values: list[Any] = [["株式会社はまなす商事"]]

    def load():
        value = values[0]
        if isinstance(value, Exception):
            raise value
        return value

    terms = watch_terms.RefreshingTerms(load, label="データベース", interval=600, retry=60, clock=lambda: now[0])
    started: list[int] = []
    monkeypatch.setattr(terms, "start", lambda: started.append(1))
    # 読み終える前は、どの検索も確かめる
    assert terms().error == "データベース: まだ読み終えていません" and started == [1]
    assert terms.refresh().wide == ("はまなす商事", "株式会社はまなす商事")
    now[0] = 599
    assert terms().error is None and started == [1]  # 間隔の前は、読み直さない
    now[0] = 600
    terms()
    assert started == [1, 1]
    # 読めなかったときは、前の言葉を残したまま、どの検索も確かめる。理由は、決めた文だけを出す（値を出さない）
    values[0] = OSError("password authentication failed for 山田")
    failed = terms.refresh()
    assert failed.error == "データベース: OSError" and "はまなす商事" in failed.wide
    assert [h.kind for h in screen("venv 作り方", users=[], local=[], terms=failed)] == [WATCH]
    values[0] = watch_terms.WatchTermsError("値が多すぎます（上限 10 件）")
    assert terms.refresh().error == "データベース: 値が多すぎます（上限 10 件）"
    now[0] = 600 + 59
    terms()
    assert started == [1, 1]  # 失敗したあとは、retry の間隔で読み直す
    now[0] = 600 + 60
    terms()
    assert started == [1, 1, 1]


def test_refreshing_terms_reads_in_the_background():
    import threading

    gate = threading.Event()

    def load():
        gate.wait(5)
        return ["みどり商会"]

    terms = watch_terms.RefreshingTerms(load, label="データベース", interval=600)
    terms.start()
    terms.start()  # 読んでいる途中なら、もう1つは始めない
    assert terms().error  # 待たせずに返す（まだ読み終えていない）
    gate.set()
    for _ in range(200):
        if not terms().error:
            break
        threading.Event().wait(0.01)
    assert terms().wide == ("みどり商会",)


def test_combined_terms(tmp_path: Path):
    path = tmp_path / "watch_terms.txt"
    path.write_text("みどり商会\n", encoding="utf-8")
    db = watch_terms.RefreshingTerms(lambda: ["Hikari Bussan"], label="データベース", interval=600)
    db.start = lambda: None  # 裏では読まない（refresh で読む）
    combined = watch_terms.CombinedTerms(WatchTermsFile(path), db)
    assert combined().error == "データベース: まだ読み終えていません" and combined().wide == ("みどり商会",)
    db.refresh()
    merged = combined()
    assert (merged.wide, merged.ascii, merged.error) == (("みどり商会",), ("hikari bussan",), None)
    assert combined() is merged  # どれも変わっていなければ、合わせ直さない
    path.unlink()
    alone = combined()
    assert alone.ascii == ("hikari bussan",) and combined() is alone  # ファイルがなくても、毎回は合わせ直さない


# ---------- 確認の仕組みへの組み込み ----------
def policy(**kwargs) -> ApprovalPolicy:
    strict = [*approvals.STRICT_FILE_TOOLS, "run_python", "query_database", "web_search"]
    return ApprovalPolicy(permissions=[], backend=None, screened=["web_search"], strict=strict, **kwargs)


def test_only_suspicious_searches_are_reviewed():
    rules, args = policy(), {"question": "venv"}
    hit = Hit(LOCAL, "aoba-7q4kx", "read_file /finance/forecast.md")
    assert rules.kind("web_search", args, STANDARD, []) is None
    assert rules.kind("web_search", args, STANDARD, [hit]) == SUSPICIOUS
    assert rules.kind("web_search", args, STANDARD, None) == SUSPICIOUS  # 記録がなければ、確かめる
    assert rules.kind("web_search", args, STRICT, []) == approvals.STRICT
    assert rules.kind("web_search", args, STRICT, [hit]) == SUSPICIOUS  # 毎回確認でも、怪しいものは理由を出す


def test_suspicious_searches_show_what_was_found_and_cannot_be_remembered():
    hits = [Hit(LOCAL, "aoba-7q4kx", "read_file /finance/forecast.md"), Hit(WATCH, "みどり商会", "見張る言葉の一覧")]
    review = policy().review("web_search", {"question": "aoba-7q4kx みどり商会"}, STANDARD, hits)
    assert review is not None and review.kind == SUSPICIOUS and review.can_remember is False
    assert "`aoba-7q4kx`" in review.detail and "/finance/forecast.md" in review.detail
    assert "`みどり商会`" in review.detail and "見張る言葉の一覧" in review.detail
    assert "外部に送られます" in review.reason
    strict = policy().review("web_search", {"question": "venv"}, STRICT, [])
    assert strict is not None and strict.can_remember is True  # 怪しくないものは、毎回確認で「以後確認しない」を選べる


def test_hits_survive_the_trip_through_the_interrupt_description():
    """画面は、止まったときの説明から判定を読み戻す。書き換えられたり、壊れていたりしたら、確かめる側に倒す。"""
    hits = [Hit(LOCAL, 'a"b --> <!-- x', "read_file /x")]
    text = "Web を検索します" + approvals.encode_hits(hits)
    assert approvals.hits_in(text) == hits
    assert approvals.hits_in("Web を検索します" + approvals.encode_hits([])) == []
    assert approvals.hits_in("Web を検索します") is None
    assert approvals.hits_in(text[:-3]) is None
    assert approvals.hits_in(text.replace('"kind": "local"', '"kind": "other"')) is None
    action = {"name": "web_search", "args": {"question": "a"}, "description": text}
    review = policy().review_action(action, STANDARD)
    assert review is not None and review.kind == SUSPICIOUS


def test_unattended_runs_reject_only_suspicious_searches():
    assert "Web 検索（外に出したくない情報らしいものを含むとき）" in policy().unattended_note()


# ---------- エージェントに組み込んだとき ----------
def calls(name: str, args: dict) -> AIMessage:
    return AIMessage("", tool_calls=[{"name": name, "args": args, "id": f"call-{uuid.uuid4().hex[:6]}", "type": "tool_call"}])


class Scripted(BaseChatModel):
    """決まった順に返事をする偽のモデル（読んだ内容を、自分から検索に書くモデルの役）。"""

    replies: list[AIMessage] = []

    @property
    def _llm_type(self) -> str:
        return "scripted-for-tests"

    def _generate(self, messages: list, stop: Any = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
        message = self.replies.pop(0) if self.replies else AIMessage("おわり")
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(self, tools: Any, **kwargs: Any) -> Scripted:
        return self


def build(terms: list[WatchTerms], searched: list[str], model: BaseChatModel | None = None):
    """偽のモデルと、判定のミドルウェアと、確認の仕組みで、エージェントを作る。terms の先頭が、いまの一覧。"""

    @tool
    def read_file(file_path: str) -> str:
        """ファイルを読む。"""
        return numbered(SECRET_FILE)

    @tool
    def web_search(question: str) -> str:
        """Web を検索する。"""
        searched.append(question)
        return "検索の結果"

    rules = policy()
    return create_agent(
        model=model or FakeModel(),
        tools=[read_file, web_search],
        middleware=[
            search_screen.SearchScreenMiddleware(lambda: terms[0], root=True),
            HumanInTheLoopMiddleware(interrupt_on=rules.interrupt_on()),
        ],
        checkpointer=InMemorySaver(),
    )


def say(agent, thread: str, text: str):
    config = {"configurable": {"thread_id": thread}}
    agent.invoke({"messages": [HumanMessage(text)]}, config)
    return agent.get_state(config)


def test_in_the_agent_clean_searches_run_without_asking():
    searched: list[str] = []
    agent = build([WatchTerms()], searched)
    state = say(agent, "t1", 'web_search {"question": "venv 作り方"}')
    assert state.interrupts == () and searched == ["venv 作り方"]
    record = next(m for m in state.values["messages"] if isinstance(m, AIMessage) and m.tool_calls)
    assert list(record.response_metadata[search_screen.RECORD_KEY].values()) == [[]]


def test_in_the_agent_a_copied_code_stops_the_search_until_someone_answers():
    searched: list[str] = []
    model = Scripted(
        replies=[
            calls("read_file", {"file_path": "/finance/forecast.md"}),
            calls("web_search", {"question": "AOBA-7Q4KX 評判"}),  # 読んだコードを、自分から検索に書く
        ]
    )
    agent = build([WatchTerms()], searched, model)
    state = say(agent, "t2", "来期の見通しをまとめて")
    assert len(state.interrupts) == 1 and searched == []
    description = state.interrupts[0].value["action_requests"][0]["description"]
    assert [hit.text for hit in approvals.hits_in(description)] == ["aoba-7q4kx"]
    agent.invoke(Command(resume={state.interrupts[0].id: {"decisions": [{"type": "reject", "message": "だめ"}]}}),
                 {"configurable": {"thread_id": "t2"}})  # fmt: skip
    assert searched == []


def test_in_the_agent_the_decision_does_not_change_when_the_list_changes_while_waiting():
    """止まっている間に見張る言葉の一覧から消しても、拒否した検索は実行されない（判定は、出た時点の記録を使う）。"""
    searched: list[str] = []
    terms = [WatchTerms.parse("みどり商会\n")]
    agent = build(terms, searched)
    state = say(agent, "t3", 'web_search {"question": "みどり商会 評判"}')
    assert len(state.interrupts) == 1
    terms[0] = WatchTerms()  # 一覧から消す
    agent.invoke(Command(resume={state.interrupts[0].id: {"decisions": [{"type": "reject", "message": "だめ"}]}}),
                 {"configurable": {"thread_id": "t3"}})  # fmt: skip
    assert searched == []


def test_what_is_shown_is_short_and_not_repeated():
    """画面に出す「見つかったもの」は、表の行をまたいだ切れ端や、同じものの繰り返しを出さない。"""
    sales = "地域,売上\n東京,120\n大阪,80\n福岡,45\n"
    messages = conversation(("read_file", {"file_path": "/data/sales.csv"}, numbered(sales)))
    hits = check("調べて 東京,120 大阪,80 福岡,45", messages)
    assert [hit.text for hit in hits] == ["東京,120 大阪,80 福岡,45"]  # 質問文のとおりの空白で、ひとつにまとめる
    assert check("東京 120 売上", messages)[0].text == "120"
