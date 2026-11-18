"""繰り返している作業の検出と、スキルの提案・登録、チャットで頼んだスキルの下書き（スキル登録）を、画面と同じ通信で確かめる。

tests/e2e/fake_app.py で起動したアプリのコンテナの中で実行する。チャットも記録も1件もない状態から始めること。
skills フォルダ（/app/skills）は、書き込める形で用意しておくこと。
偽の検出役は、「議事録」や「レポート」を含む依頼が3件以上あると、決まった下書きを返す。

    python tests/e2e/check_skill_mining.py

環境変数 E2E_PASSWORD にログインのパスワードを渡す。
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from check_chat_history import Chat, api, check, list_threads, login
from check_schedules import press
from deepagents.middleware.skills import _parse_skill_metadata

from data_agent.settings import get_settings
from data_agent.storage.chat_history import build_data_layer

SKILLS = Path("/app/skills")
COMMAND = "スキル提案"
REQUEST = "スキル登録"


async def main() -> None:
    db = build_data_layer(get_settings())

    async def count(table: str, where: str = "true") -> int:
        return (await db.execute_query(f'SELECT count(*) AS n FROM "{table}" WHERE {where}'))[0]["n"]

    cookie = login()
    check("最初は一覧が空", list_threads(cookie) == [])
    chat = Chat()
    await chat.open(cookie)
    check("最初は、承認待ちの知らせが出ない", not any("見つかっています" in m for m in chat.assistant), chat.assistant)

    reply = await chat.say("見せて", command=COMMAND)
    check("記録が少ないうちは、調べない", "まだ 0 件" in reply, reply)

    for text in (
        "会議メモから議事録を作って",
        "こんにちは",
        "昨日の打ち合わせの議事録を作って",
        "別の質問です",
        "定例会の議事録を作って",
    ):
        await chat.say(text)
    check("依頼が記録される（コマンドは記録されない）", await count("RequestLog") == 5, await count("RequestLog"))
    await chat.close()

    # 自動: 記録がたまった状態でチャットを開くと、裏で調べる
    second = Chat()
    await second.open(cookie)
    await asyncio.sleep(3)
    check("チャットを開くと、裏で調べて下書きが作られる", await count("SkillProposal", "status = 'pending'") == 1)
    check("下書きができただけでは、スキルは登録されない", not (SKILLS / "meeting-minutes").exists())
    await second.close()

    third = Chat()
    await third.open(cookie)
    check(
        "次にチャットを開くと、承認待ちがあると知らされる",
        any("1 件見つかっています" in m for m in third.assistant),
        third.assistant,
    )

    # 中身を見て、登録する
    reply = await third.say("提案を見せて", command=COMMAND)
    check(
        "提案に、名前・回数・手順書の中身が出る",
        all(s in reply for s in ("meeting-minutes", "3 回", "name: meeting-minutes", "1. ")),
        reply,
    )
    check("提案に、当てはまった依頼の例が出る", "定例会の議事録を作って" in reply, reply)
    button = next((a for a in third.actions if a["name"] == "skill_register"), None)
    check("登録するボタンと見送るボタンが付く", button is not None and any(a["name"] == "skill_dismiss" for a in third.actions))

    forged = dict(button, id=str(uuid.uuid4()), payload={"proposal_id": str(uuid.uuid4())})
    api(cookie, "POST", "/project/action", {"sessionId": third.session_id, "action": forged})
    await asyncio.sleep(1)
    check("存在しない提案の ID では、何も登録されない", not (SKILLS / "meeting-minutes").exists())

    await press(cookie, third, "skill_register")
    path = SKILLS / "meeting-minutes" / "SKILL.md"
    check("ボタンを押すと、手順書が skills に書き出される", path.exists())
    parsed = _parse_skill_metadata(path.read_text(encoding="utf-8"), str(path), "meeting-minutes")
    check(
        "書き出した手順書を、エージェントの仕組みが読み取れる", parsed is not None and parsed["name"] == "meeting-minutes", parsed
    )
    check("提案が登録済みになる", await count("SkillProposal", "status = 'registered'") == 1)

    reply = await third.say("もう一度見せて", command=COMMAND)
    check("登録したものは、もう提案されない", "見つかりませんでした" in reply, reply)

    # 見送る
    for number in range(1, 4):
        await third.say(f"週次のレポートを作って {number}")
    third.actions.clear()
    reply = await third.say("また見せて", command=COMMAND)
    check("別の繰り返しが見つかると、新しい提案が出る", "weekly-report" in reply, reply)
    await press(cookie, third, "skill_dismiss")
    check("見送ると、スキルは登録されない", not (SKILLS / "weekly-report").exists())
    check("提案が見送りになる", await count("SkillProposal", "status = 'dismissed'") == 1)
    reply = await third.say("さらに見せて", command=COMMAND)
    check("見送ったものは、もう提案されない", "見つかりませんでした" in reply, reply)
    await third.close()

    # チャットを削除すると、その中の依頼の記録も消える
    before = await count("RequestLog")
    target = next(t["id"] for t in list_threads(cookie) if t["name"] == "提案を見せて")
    api(cookie, "DELETE", "/project/thread", {"threadId": target})
    check(
        "チャットを削除すると、その中の依頼の記録も消える",
        (before, await count("RequestLog")) == (8, 5),
        (before, await count("RequestLog")),
    )

    # チャットで頼んで、下書きを作る（スキル登録。ADR-0030）。偽の下書き役は、いつも sales-summary を返す
    fourth = Chat()
    await fourth.open(cookie)
    await fourth.say("そのまま言って 売上の表を地域ごとに集計しました")
    reply = await fourth.say("今やった集計を、毎週使える手順書にして", command=REQUEST)
    check(
        "頼んだ内容から下書きができ、名前・頼んだ内容・手順書の中身が出る",
        all(
            s in reply for s in ("sales-summary", "チャットで頼まれて作りました", "今やった集計を", "name: sales-summary", "1. ")
        ),
        reply,
    )
    check("このチャットのやりとりも、下書きの材料になる", "前のやりとりを読んだ" in reply, reply)
    check(
        "下書きにも、登録するボタンと見送るボタンが付く",
        {"skill_register", "skill_dismiss"} <= {a["name"] for a in fourth.actions},
    )
    check("下書きができただけでは、登録されない", not (SKILLS / "sales-summary").exists())
    await press(cookie, fourth, "skill_register")
    path = SKILLS / "sales-summary" / "SKILL.md"
    check("ボタンを押すと、手順書が skills に書き出される", path.exists())
    written = path.read_text(encoding="utf-8") if path.exists() else ""
    parsed = _parse_skill_metadata(written, str(path), "sales-summary")
    check(
        "チャットで頼んだ手順書も、エージェントの仕組みが読み取れる",
        parsed is not None and "チャットでの依頼から下書きし" in written,
        parsed,
    )

    reply = await fourth.say("weekly-sales: 毎週の売上をまとめる", command=REQUEST)
    check("先頭に書いた名前で、下書きができる", "name: weekly-sales" in reply, reply)
    reply = await fourth.say("sales-summary: 同じ名前でもう一つ", command=REQUEST)
    check("すでにあるスキルの名前は断る", "すでにあります" in reply, reply)
    reply = await fourth.say("weekly-sales: 待っている名前でもう一つ", command=REQUEST)
    check("登録を待っている下書きの名前は断る", "登録を待っています" in reply, reply)
    reply = await fourth.say("unsafe-steps: 手順書にできない依頼です", command=REQUEST)
    check("手順書にできない依頼は、下書きにしない", "作れませんでした" in reply, reply)
    reply = await fourth.say("fence-test: 枠を閉じる手順書", command=REQUEST)
    check("下書きに枠の記号があっても、枠が途中で閉じない（偽の説明を書き足せない）", "````markdown" in reply, reply)
    check(
        "コマンドで頼んだ内容は、依頼の記録に入らない（ふつうの発言の 1 件だけ増える）",
        await count("RequestLog") == 6,
        await count("RequestLog"),
    )
    await fourth.close()
    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
