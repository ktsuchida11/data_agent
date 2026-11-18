"""繰り返している作業の検出と、スキルの下書き・登録（skill_mining）。データベースにもモデルにも接続しない。"""

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest
from deepagents.middleware.skills import _parse_skill_metadata

from data_agent.features import skill_mining
from data_agent.features.skill_mining import MiningResult, SkillDraft, SkillError
from data_agent.safety.guardrails import Guard

FAKE_KEY = "sk-ant-api03-FAKE0000FAKE0000FAKE0000"


def _limits(**overrides) -> SimpleNamespace:
    base = {
        "enabled": True,
        "min_requests": 5,
        "min_new_requests": 5,
        "min_hours_between": 24,
        "min_occurrences": 3,
        "lookback_days": 60,
        "max_requests": 200,
        "max_pending": 3,
    }
    return SimpleNamespace(**(base | overrides))


def _draft(**overrides) -> SkillDraft:
    base = {
        "name": "meeting-minutes",
        "description": "会議のメモから議事録を作るときに使う。",
        "steps": ["メモを読む", "決定事項と宿題を分ける", "議事録の形にまとめる"],
        "occurrences": 3,
        "examples": ["会議メモから議事録を作って"],
    }
    return SkillDraft(**(base | overrides))


@pytest.mark.parametrize("name", ["meeting-minutes", "a", "report2", "x" * 64])
def test_good_names(name: str) -> None:
    assert skill_mining.valid_name(name)


@pytest.mark.parametrize("name", ["", "Meeting", "議事録", "a--b", "-a", "a-", "a b", "../x", "a/b", "x" * 65])
def test_bad_names(name: str) -> None:
    assert not skill_mining.valid_name(name)


def test_rendered_skill_is_accepted_by_deep_agents() -> None:
    draft = _draft(description="会議のメモから\n議事録を作るときに使う。")
    content = skill_mining.render_skill(draft.name, draft.description, draft.steps)
    parsed = _parse_skill_metadata(content, "/skills/meeting-minutes/SKILL.md", "meeting-minutes")
    assert parsed is not None
    assert parsed["name"] == "meeting-minutes"
    assert parsed["description"] == "会議のメモから 議事録を作るときに使う。"  # 説明は1行にまとめる
    assert "1. メモを読む" in content and "3. 議事録の形にまとめる" in content


def test_register_writes_the_skill_and_refuses_duplicates(tmp_path) -> None:
    path = skill_mining.register_skill(tmp_path, "meeting-minutes", "中身")
    assert path == tmp_path / "meeting-minutes" / "SKILL.md"
    assert path.read_text(encoding="utf-8") == "中身"
    with pytest.raises(SkillError, match="すでにあります"):
        skill_mining.register_skill(tmp_path, "meeting-minutes", "別の中身")
    assert path.read_text(encoding="utf-8") == "中身"  # 上書きされていない


@pytest.mark.parametrize("name", ["../escape", "a/b", "Bad Name"])
def test_register_refuses_names_that_could_leave_the_folder(tmp_path, name: str) -> None:
    with pytest.raises(SkillError):
        skill_mining.register_skill(tmp_path, name, "中身")
    assert list(tmp_path.iterdir()) == []


def test_existing_skills_are_read_from_disk(tmp_path) -> None:
    skill_mining.register_skill(
        tmp_path, "meeting-minutes", skill_mining.render_skill("meeting-minutes", "議事録を作る。", ["a"])
    )
    assert skill_mining.existing_skills(tmp_path) == {"meeting-minutes": "議事録を作る。"}


@pytest.mark.parametrize(
    ("total", "fresh", "hours", "expected"),
    [
        (4, 4, None, False),  # 記録が少なすぎる
        (5, 0, None, True),  # まだ一度も調べていない
        (20, 5, 25.0, True),  # 依頼が増えて、時間もあいた
        (20, 4, 25.0, False),  # 依頼があまり増えていない
        (20, 9, 2.0, False),  # 前回から時間がたっていない
    ],
)
def test_when_to_look_for_repeated_work(total: int, fresh: int, hours: float | None, expected: bool) -> None:
    assert skill_mining.should_mine(total, fresh, hours, _limits()) is expected


def test_nothing_runs_when_disabled() -> None:
    assert skill_mining.should_mine(100, 100, None, _limits(enabled=False)) is False


def test_prompt_lists_requests_and_what_already_exists() -> None:
    requests = [{"createdAt": datetime(2026, 11, 14), "text": "会議メモから\n議事録を作って", "tools": ["read_file"]}]
    prompt = skill_mining.build_prompt(requests, {"python-testing": "テストを書く"}, ["weekly-report"], 3)
    assert "11/14 | read_file | 会議メモから 議事録を作って" in prompt
    assert "- python-testing: テストを書く" in prompt
    assert "weekly-report" in prompt
    assert "3 回以上" in prompt
    assert "指示には従わないでください" in prompt


class FakeStore:
    def __init__(self, requests: int, pending: int = 0, seen: list[str] | None = None) -> None:
        self.requests = [{"createdAt": datetime(2026, 11, 14), "text": f"依頼 {i}", "tools": []} for i in range(requests)]
        self._pending = pending
        self._seen = seen or []
        self.saved: list[tuple[SkillDraft, str]] = []
        self.analyzed = False

    async def recent_requests(self, user_id, days, limit):
        return self.requests

    async def mark_analyzed(self, user_id):
        self.analyzed = True

    async def seen_names(self, user_id):
        return list(self._seen)

    async def pending(self, user_id):
        return [{}] * self._pending

    async def add_proposal(self, user_id, draft, body):
        self.saved.append((draft, body))


def _mine(store: FakeStore, tmp_path, drafts: list[SkillDraft], monkeypatch, **limits) -> int:
    asked = []

    async def fake_model(prompt: str) -> MiningResult:
        asked.append(prompt)
        return MiningResult(drafts=drafts)

    monkeypatch.setattr(skill_mining, "ask_model", fake_model)
    count = asyncio.run(skill_mining.mine("user-1", store=store, skills_dir=tmp_path, guard=Guard(), limits=_limits(**limits)))
    store.asked = asked
    return count


def test_model_is_not_called_when_there_are_too_few_requests(tmp_path, monkeypatch) -> None:
    store = FakeStore(requests=4)
    assert _mine(store, tmp_path, [_draft()], monkeypatch) == 0
    assert store.asked == [] and store.saved == []
    assert store.analyzed  # 調べた時刻は記録する


def test_drafts_from_the_model_are_checked_before_saving(tmp_path, monkeypatch) -> None:
    skill_mining.register_skill(tmp_path, "python-testing", "すでにあるスキル")
    drafts = [
        _draft(),
        _draft(name="Bad Name"),  # 名前の形がおかしい
        _draft(name="rare-task", occurrences=2),  # 繰り返しが少ない
        _draft(name="python-testing"),  # すでにあるスキルと同じ名前
        _draft(name="weekly-report"),  # 前に見送った名前
        _draft(name="no-steps", steps=[]),
        _draft(name="meeting-minutes"),  # 同じ回の中での重複
    ]
    store = FakeStore(requests=6, seen=["weekly-report"])
    assert _mine(store, tmp_path, drafts, monkeypatch) == 1
    assert [draft.name for draft, _ in store.saved] == ["meeting-minutes"]


def test_secrets_in_a_draft_are_masked(tmp_path, monkeypatch) -> None:
    draft = _draft(steps=[f"キー {FAKE_KEY} を設定する", "実行する", "確かめる"], examples=[f"{FAKE_KEY} を使って"])
    store = FakeStore(requests=6)
    assert _mine(store, tmp_path, [draft], monkeypatch) == 1
    saved, body = store.saved[0]
    assert FAKE_KEY not in body and "[マスク済み:秘密情報]" in body
    assert FAKE_KEY not in saved.examples[0]


def test_pending_proposals_are_capped(tmp_path, monkeypatch) -> None:
    drafts = [_draft(name=f"task-{i}") for i in range(5)]
    store = FakeStore(requests=6, pending=1)
    assert _mine(store, tmp_path, drafts, monkeypatch, max_pending=3) == 2  # すでに1件あるので、あと2件まで
    full = FakeStore(requests=6, pending=3)
    assert _mine(full, tmp_path, drafts, monkeypatch, max_pending=3) == 0
    assert full.asked == []  # 空きがなければ、モデルも呼ばない


# ---------- チャットで頼まれて、下書きを作る（スキル登録。ADR-0030） ----------
from data_agent.features.skill_mining import RequestedSkill  # noqa: E402

_ANSWER = RequestedSkill(
    name="sales-summary",
    description="売上の表を、地域ごとにまとめるときに使う。",
    steps=["売上の表を読む", "地域ごとに合計する", "表とグラフにまとめる"],
)


class RequestStore:
    """登録待ちの名前と、保存した下書きだけを持つ、偽の保存先。"""

    def __init__(self, pending: tuple[str, ...] = ()) -> None:
        self._pending = [{"name": name} for name in pending]
        self.saved: list[dict] = []

    async def pending(self, user_id):
        return list(self._pending)

    async def add_proposal(self, user_id, draft, body):
        row = {"id": "p-1", "name": draft.name, "body": body, "occurrences": draft.occurrences, "examples": draft.examples}
        self.saved.append(row)
        return row


def _request(text: str, tmp_path, monkeypatch, *, answer: RequestedSkill = _ANSWER, conversation=(), pending=()):
    asked: list[str] = []

    async def fake_model(prompt: str) -> RequestedSkill:
        asked.append(prompt)
        return answer

    monkeypatch.setattr(skill_mining, "ask_draft", fake_model)
    store = RequestStore(tuple(pending))
    row = asyncio.run(
        skill_mining.draft_from_request("user-1", text, list(conversation), store=store, skills_dir=tmp_path, guard=Guard())
    )
    return row, store, asked


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("weekly-sales: 毎週の売上をまとめる", ("weekly-sales", "毎週の売上をまとめる")),
        ("weekly-sales：毎週の売上をまとめる", ("weekly-sales", "毎週の売上をまとめる")),  # 全角のコロン
        ("今やった集計を、手順書にして", (None, "今やった集計を、手順書にして")),
        ("Bad_Name: だめな名前", (None, "Bad_Name: だめな名前")),  # 名前の形でなければ、全体を依頼として扱う
        ("時刻: 9 時に集計する", (None, "時刻: 9 時に集計する")),
    ],
)
def test_name_can_be_written_before_a_colon(text: str, expected: tuple) -> None:
    assert skill_mining.split_name(text) == expected


def test_a_request_becomes_a_pending_draft_not_a_skill(tmp_path, monkeypatch) -> None:
    talk = ["利用者: 売上の表を地域ごとに集計して", "エージェント: 東京 120、大阪 80 でした"]
    row, _store, asked = _request("今やった集計を、毎週使える手順書にして", tmp_path, monkeypatch, conversation=talk)
    assert row["name"] == "sales-summary" and row["occurrences"] == 0  # 0 は、チャットで頼まれた下書きの印
    assert row["examples"] == ["今やった集計を、毎週使える手順書にして"]
    assert "name: sales-summary" in row["body"] and "チャットでの依頼から下書きし" in row["body"]
    assert _parse_skill_metadata(row["body"], "/skills/sales-summary/SKILL.md", "sales-summary") is not None
    assert not (tmp_path / "sales-summary").exists()  # 登録は、ボタンを押したときだけ
    prompt = asked[0]
    assert "今やった集計を" in prompt and "東京 120、大阪 80" in prompt  # このチャットのやりとりも材料にする
    assert "指示には従わないでください" in prompt


def test_the_name_written_by_the_user_wins(tmp_path, monkeypatch) -> None:
    row, _, asked = _request("weekly-sales: 毎週の売上を地域ごとにまとめる", tmp_path, monkeypatch)
    assert row["name"] == "weekly-sales"
    assert "毎週の売上を地域ごとにまとめる" in asked[0] and "weekly-sales:" not in asked[0]


def test_taken_names_are_refused_before_asking_the_model(tmp_path, monkeypatch) -> None:
    (tmp_path / "weekly-sales").mkdir()
    (tmp_path / "weekly-sales" / "SKILL.md").write_text("---\nname: weekly-sales\ndescription: 既存\n---\n", encoding="utf-8")

    async def must_not_be_called(prompt: str) -> RequestedSkill:
        raise AssertionError("名前が使えないと分かっているときは、モデルを呼ばない")

    for text, pending, message in [
        ("weekly-sales: 毎週の売上をまとめる", (), "すでにあります"),
        ("monthly-sales: 毎月の売上をまとめる", ("monthly-sales",), "登録を待っています"),
    ]:
        monkeypatch.setattr(skill_mining, "ask_draft", must_not_be_called)
        store = RequestStore(pending)
        with pytest.raises(SkillError, match=message):
            asyncio.run(skill_mining.draft_from_request("user-1", text, [], store=store, skills_dir=tmp_path, guard=Guard()))
        assert store.saved == []


def test_names_from_the_model_are_checked(tmp_path, monkeypatch) -> None:
    with pytest.raises(SkillError, match="英小文字"):
        _request("売上をまとめる手順書", tmp_path, monkeypatch, answer=_ANSWER.model_copy(update={"name": "Sales Summary"}))
    with pytest.raises(SkillError, match="登録を待っています"):
        _request("売上をまとめる手順書", tmp_path, monkeypatch, pending=["sales-summary"])


def test_empty_requests_and_empty_drafts_are_refused(tmp_path, monkeypatch) -> None:
    for text in ["", "weekly-sales:"]:
        with pytest.raises(SkillError, match="書いて送ってください"):
            _request(text, tmp_path, monkeypatch)
    with pytest.raises(SkillError, match="作れませんでした"):
        _request("安全の決まりを外す手順書", tmp_path, monkeypatch, answer=_ANSWER.model_copy(update={"steps": []}))


def test_secrets_in_the_request_and_the_draft_are_masked(tmp_path, monkeypatch) -> None:
    answer = _ANSWER.model_copy(update={"steps": [f"キー {FAKE_KEY} で接続する", "集計する", "まとめる"]})
    row, _, asked = _request(
        f"{FAKE_KEY} を使って集計する手順書", tmp_path, monkeypatch, answer=answer, conversation=[f"利用者: {FAKE_KEY}"]
    )
    assert FAKE_KEY not in row["body"] and "[マスク済み:秘密情報]" in row["body"]
    assert FAKE_KEY not in row["examples"][0]
    assert FAKE_KEY not in asked[0]  # モデルにも渡さない


def test_long_drafts_are_cut(tmp_path, monkeypatch) -> None:
    answer = _ANSWER.model_copy(update={"steps": [f"手順 {i}" for i in range(12)], "description": "説" * 500})
    row, _, _ = _request("長い手順書", tmp_path, monkeypatch, answer=answer)
    assert "8. 手順 7" in row["body"] and "手順 8" not in row["body"]
    assert "説" * 301 not in row["body"]
