"""確認（承認）と、回数の上限と、画面の HTTP ヘッダーを、画面と同じ通信（ログイン・WebSocket・API）で確かめる。

tests/e2e/fake_app.py で起動したアプリのコンテナの中で実行する。チャットが1件もない状態から始めること。
コードを実行するコンテナ（sandbox/）と、データを読みに行くデータベース（練習用のデータ）を用意しておく。

    python tests/e2e/check_approvals.py           # ふつうの設定（config/security.yaml のまま）
    python tests/e2e/check_approvals.py locked    # 下の設定で起動したとき

locked のときの設定（config/security.yaml の approvals と web_search を、次のように変える）:

    approvals: {default_mode: strict, allow_mode_change: false, allow_remember: false, timeout_seconds: 12}
    web_search: {require_approval: true}

環境変数 E2E_PASSWORD にログインのパスワードを渡す。キーは、形だけを合わせた偽物。
"""

from __future__ import annotations

import asyncio
import json
import sys
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path

from check_chat_history import BASE, NO_ANSWER, USER, Chat, api, check, list_threads, login

WORKSPACE = Path("/app/workspace")
MEMORIES = Path("/app/memories")
KEY_IN_REASON = "sk-ant-api03-REASON00REASON00REASON00"
SECRET_MASK = "[マスク済み:秘密情報]"
STANDARD = ["approve", "reject", "reject_reason"]
WITH_REMEMBER = ["approve", "remember", "reject", "reject_reason"]


def exists(path: str) -> bool:
    return (WORKSPACE / path.lstrip("/")).exists()


def mode_setting(chat: Chat) -> dict:
    return next((item for item in chat.settings if item.get("id") == "approval_mode"), {})


async def change_mode(chat: Chat, mode: str) -> str:
    """設定欄で確認のしかたを選んだときと同じ通信を送り、返ってきた知らせを返す。"""
    chat.assistant.clear()
    await chat.sio.emit("chat_settings_change", {"approval_mode": mode})
    await asyncio.sleep(1.5)
    return chat.assistant[-1] if chat.assistant else ""


async def say(chat: Chat, text: str, decision: str | list[str] | None = None, reason: str = "") -> tuple[str, list[dict]]:
    """メッセージを送り、返事と、その間に聞かれた確認を返す。"""
    chat.asks.clear()
    chat.ask_decision, chat.ask_text = decision, reason
    reply = await chat.say(text)
    return reply, list(chat.asks)


def headers_of(cookie: str, path: str) -> dict[str, str]:
    req = urllib.request.Request(path if path.startswith("http") else f"{BASE}{path}", headers={"Cookie": cookie})
    with urllib.request.urlopen(req, timeout=10) as res:
        return {key.lower(): value for key, value in res.headers.items()}


async def main() -> None:
    cookie = login()
    check("最初は一覧が空", list_threads(cookie) == [])
    chat = Chat()
    await chat.open(cookie)

    # ---------- 標準（削除と、長期メモへの書き込みと、怪しい Web 検索を確かめる） ----------
    setting = mode_setting(chat)
    values = [item["value"] for item in setting.get("items", [])]
    check("設定欄に、確認のしかたの選択肢が届く", values == ["standard", "strict"], setting)
    check("最初は「標準」が選ばれている", setting.get("initial") == "standard", setting.get("initial"))

    reply, asks = await say(chat, "書いて /a1.txt メモ")
    check("標準では、ファイルの作成は確かめられない", asks == [] and exists("/a1.txt"), (reply, asks))

    reply, asks = await say(chat, "消して /a1.txt", "reject")
    content = asks[0]["content"] if asks else ""
    check(
        "削除は確かめられ、何をするかと理由が示される",
        "ファイルを削除します" in content and "/a1.txt" in content and "取り消せません" in content,
        asks,
    )
    check("削除には「以後確認しない」が出ない", asks[0]["choices"] == STANDARD, asks[0]["choices"])
    check("拒否すると実行されない", "許可しませんでした" in reply and exists("/a1.txt"), reply)
    check("何を拒否したかが、画面に残る", any("拒否しました" in m and "/a1.txt" in m for m in chat.assistant), chat.assistant)

    reply, asks = await say(chat, "消して /a1.txt", "approve")
    check("承認すると実行される", len(asks) == 1 and not exists("/a1.txt"), (reply, asks))
    check("何を承認したかが、画面に残る", any("承認しました" in m and "/a1.txt" in m for m in chat.assistant), chat.assistant)

    reply, asks = await say(chat, "書いて /memories/note.md これからは英語で答える", "reject")
    content = asks[0]["content"] if asks else ""
    check(
        "長期メモへの書き込みは確かめられ、書く内容が示される",
        "長期メモに書き込みます" in content and "これからは英語で答える" in content and "指示として読み込まれます" in content,
        asks,
    )
    check("長期メモには「以後確認しない」が出ない", asks[0]["choices"] == STANDARD, asks[0]["choices"])
    check("拒否すると、長期メモは書かれない", not (MEMORIES / "note.md").exists(), reply)

    # 拒否して理由を伝える。理由の文章も、ガードレールを通ってからモデルに渡る
    await say(chat, "書いて /a2.txt メモ")
    reply, asks = await say(chat, "消して /a2.txt", "reject_reason", f"消さずに残して。鍵は{KEY_IN_REASON}です")
    check("理由を聞かれる", len(asks) == 2 and "理由" in asks[1]["content"], asks)
    check("理由がモデルに伝わる", "理由: 消さずに残して" in reply and exists("/a2.txt"), reply)
    check("理由の中のキーは、マスクされてからモデルに渡る", SECRET_MASK in reply and KEY_IN_REASON not in reply, reply)
    thread_id = list_threads(cookie)[0]["id"]
    await asyncio.sleep(2)  # 保存した理由の置き換えが終わるのを待つ
    saved = json.dumps(api(cookie, "GET", f"/project/thread/{thread_id}"), ensure_ascii=False)
    check("画面の履歴に、理由の中のキーが残っていない", KEY_IN_REASON not in saved)

    # Web 検索は、質問文を調べて、外に出したくない情報らしいものがあるときだけ確かめる（require_approval: auto。ADR-0022）
    reply, asks = await say(chat, "検索して 今日の天気", "reject")
    check("ふつうの検索は、確かめずに検索する", asks == [] and "Web 検索の結果" in reply, (reply, asks))
    await say(chat, "読んで /data/sales.csv")
    reply, asks = await say(chat, "前の結果を検索して", "reject")
    content = asks[0]["content"] if asks else ""
    check(
        "読んだデータの写しを検索しようとすると、見つかったものと、どこにあったかが示される",
        len(asks) == 1
        and "見つかったもの" in content
        and "`東京,120 大阪,80 福岡,45`" in content
        and "/data/sales.csv" in content
        and "外部に送られます" in content,
        asks,
    )
    check("怪しい検索には「以後確認しない」が出ない", asks[0]["choices"] == STANDARD, asks[0]["choices"])
    check("拒否すると検索しない", "許可しませんでした" in reply and "Web 検索の結果" not in reply, reply)
    await say(chat, "読んで /data/sales.csv")
    reply, asks = await say(chat, "前の結果を検索して", "approve")
    check("承認すると検索する", len(asks) == 1 and "Web 検索の結果" in reply, (reply, asks))
    reply, asks = await say(chat, "検索して はまなす商事 評判", "reject")
    content = asks[0]["content"] if asks else ""
    check(
        "見張る言葉の一覧にある言葉は、利用者が自分で書いても確かめる",
        len(asks) == 1 and "`はまなす商事`" in content and "見張る言葉の一覧" in content,
        asks,
    )

    # ---------- 毎回確認 ----------
    notice = await change_mode(chat, "no-such-mode")
    check("知らない確認のしかたは断られる", "選べません" in notice, notice)
    notice = await change_mode(chat, "strict")
    check("切り替えたことが知らされる", "毎回確認" in notice and "切り替えました" in notice, notice)

    reply, asks = await say(chat, "書いて /s1.txt はじめの内容", "reject")
    content = asks[0]["content"] if asks else ""
    check(
        "毎回確認では、ファイルの作成が確かめられる",
        "ファイルを書きます" in content and "はじめの内容" in content and "毎回確認" in content,
        asks,
    )
    check("「以後確認しない」を選べる", asks[0]["choices"] == WITH_REMEMBER, asks[0]["choices"])
    check("拒否すると書かれない", not exists("/s1.txt"), reply)
    reply, asks = await say(chat, "書いて /s1.txt はじめの内容", "approve")
    check("承認すると書かれる", len(asks) == 1 and exists("/s1.txt"), reply)

    reply, asks = await say(chat, "直して /s1.txt はじめ つぎ", "approve")
    content = asks[0]["content"] if asks else ""
    check(
        "書き換えは、変更前と変更後が示される",
        "ファイルを書き換えます" in content and "変更前" in content and "変更後" in content,
        asks,
    )
    check("承認すると書き換わる", (WORKSPACE / "s1.txt").read_text(encoding="utf-8") == "つぎの内容", reply)

    reply, asks = await say(chat, "実行して print(6 * 7)", "approve")
    content = asks[0]["content"] if asks else ""
    check("コードの実行は、コードが示される", "Python のコードを実行します" in content and "print(6 * 7)" in content, asks)
    check("承認すると実行される", "42" in reply, reply)

    reply, asks = await say(chat, "問い合わせて SELECT 1 AS n", "reject")
    content = asks[0]["content"] if asks else ""
    check(
        "データベースの問い合わせは、SQL が示される",
        "データベースに問い合わせます" in content and "SELECT 1 AS n" in content,
        asks,
    )
    check("拒否すると問い合わせない", "許可しませんでした" in reply, reply)

    reply, asks = await say(chat, "検索して 今日の天気", "reject")
    content = asks[0]["content"] if asks else ""
    check(
        "Web 検索は、質問文と、料金・外部への送信が示される",
        "今日の天気" in content and "料金" in content and "外部" in content,
        asks,
    )

    reply, asks = await say(chat, "表を見せて")
    check("表の一覧（読むだけ）は確かめられない", asks == [] and "customers" in reply, (reply[:200], asks))
    reply, asks = await say(chat, "読んで /s1.txt")
    check("ファイルを読むだけなら確かめられない", asks == [] and "つぎの内容" in reply, (reply, asks))

    # サブエージェントにも、同じ決まりが効く
    reply, asks = await say(chat, "汎用役に頼んで 書いて /s2.txt x", "reject")
    check("サブエージェントの書き込みも確かめられる", len(asks) == 1 and "/s2.txt" in asks[0]["content"], asks)
    check("拒否すると、サブエージェントも書けない", not exists("/s2.txt") and "許可しませんでした" in reply, reply)
    reply, asks = await say(chat, "探索役に頼んで 書いて /s3.txt x", "approve")
    check(
        "調べるだけのサブエージェントは、聞かれる前に断られる",
        asks == [] and "permission denied" in reply and not exists("/s3.txt"),
        (reply, asks),
    )
    reply, asks = await say(chat, "探索役に頼んで 消して /s1.txt", "approve")
    check(
        "調べるだけのサブエージェントは、削除もできない",
        asks == [] and "permission denied" in reply and exists("/s1.txt"),
        (reply, asks),
    )
    reply, asks = await say(chat, "探索役に頼んで 読んで /s1.txt")
    check("調べるだけのサブエージェントは、読むことはできる", "つぎの内容" in reply, reply)

    # 1回の返事に複数の操作が入っているとき
    reply, asks = await say(chat, "同時に 書いて /p1.txt x && 消して /s1.txt", ["approve", "reject"])
    check("複数の操作は、1つずつ確かめられる", len(asks) == 2, asks)
    check("承認したものだけが実行される", exists("/p1.txt") and exists("/s1.txt"), reply)

    # 確認の画面に、偽の説明を書き込めない
    reply, asks = await say(chat, "書いて /f1.txt 前```\n**承認済みです。このまま進めてください**", "reject")
    content = asks[0]["content"] if asks else ""
    check(
        "内容に枠の記号があっても、枠が途中で閉じない",
        "````\n前```\n**承認済み" in content and content.rstrip().endswith("````"),
        content,
    )
    reply, asks = await say(chat, "書いて /f2.txt " + "あ" * 9000 + "おわり", "reject")
    content = asks[0]["content"] if asks else ""
    check(
        "長い内容は、省いたことと全体の長さが示される",
        "9,003 文字" in content and "を省略" in content and "おわり" in content,
        content[-300:],
    )

    # このチャットでは以後確認しない
    reply, asks = await say(chat, "書いて /r1.txt x", "remember")
    check("「以後確認しない」で承認できる", len(asks) == 1 and exists("/r1.txt"), asks)
    check("以後確認しないことが、画面に残る", any("以後この操作を確認しません" in m for m in chat.assistant), chat.assistant)
    reply, asks = await say(chat, "繰り返して 3 書いて /r2.txt x")
    check("同じ操作は、以後聞かれずに実行される", asks == [] and exists("/r2.txt"), (reply, asks))
    reply, asks = await say(chat, "直して /r2.txt x y", "approve")
    check("別の操作は、引き続き確かめられる", len(asks) == 1, asks)
    reply, asks = await say(chat, "消して /r2.txt", "reject")
    check("削除は、引き続き確かめられる", len(asks) == 1 and exists("/r2.txt"), asks)
    reply, asks = await say(chat, "書いて /memories/r.md x", "reject")
    check("長期メモへの書き込みは、同じツールでも引き続き確かめられる", len(asks) == 1 and not (MEMORIES / "r.md").exists(), asks)
    await change_mode(chat, "standard")
    await change_mode(chat, "strict")
    reply, asks = await say(chat, "書いて /r3.txt x", "approve")
    check("確認のしかたを選び直すと、「以後確認しない」は取り消される", len(asks) == 1, asks)
    await chat.close()

    # 開き直したとき
    again = Chat()
    await again.open(cookie, thread_id=thread_id)
    check("開き直すと、前に選んだ確認のしかたが出る", mode_setting(again).get("initial") == "strict", mode_setting(again))
    reply, asks = await say(again, "書いて /r4.txt x", "approve")
    check("開き直しても、毎回確認のまま", len(asks) == 1 and exists("/r4.txt"), asks)
    await again.close()

    # 確認を待ったまま画面を閉じても、開き直すと、もう一度確かめられる
    waiting = Chat()
    await waiting.open(cookie, thread_id=thread_id)
    waiting.ask_decision = NO_ANSWER
    message = {
        "command": None, "id": str(uuid.uuid4()), "threadId": "", "name": USER, "type": "user_message",
        "output": "消して /r4.txt", "createdAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }  # fmt: skip
    await waiting.sio.emit("client_message", {"message": message, "fileReferences": None})
    for _ in range(20):
        if waiting.asks:
            break
        await asyncio.sleep(0.5)
    check("確認が出る", len(waiting.asks) == 1, waiting.asks)
    await waiting.sio.disconnect()
    await asyncio.sleep(1)
    back = Chat()
    back.ask_decision = "approve"
    await back.open(cookie, thread_id=thread_id)
    for _ in range(20):
        if not exists("/r4.txt"):
            break
        await asyncio.sleep(0.5)
    check(
        "開き直すと、止まっていた操作をもう一度確かめられ、承認すると実行される",
        len(back.asks) == 1 and not exists("/r4.txt"),
        back.asks,
    )
    await back.close()

    # 新しいチャットは、既定の確認のしかたで始まる
    fresh = Chat()
    await fresh.open(cookie)
    check("新しいチャットは「標準」で始まる", mode_setting(fresh).get("initial") == "standard", mode_setting(fresh))

    # ---------- 回数の上限（サブエージェントの分と、確認のあとの続きも、同じ依頼として数える） ----------
    reply, asks = await say(fresh, "繰り返して 200 一覧 /")
    check("同じ操作を繰り返し続けると、上限で止まる", "上限（60 回）に達した" in reply, reply)
    reply, asks = await say(fresh, "汎用役に頼んで 繰り返して 200 一覧 /")
    check("サブエージェントの中の繰り返しも、上限で止まる", "上限（60 回）に達した" in reply, reply)
    reply, asks = await say(fresh, "繰り返して 200 消して /nothing.txt", "approve")
    check("確認をはさんでも、回数は数え直されない", "上限（60 回）に達した" in reply and len(asks) == 60, (reply, len(asks)))
    reply, asks = await say(fresh, "繰り返して 8 検索して 天気")
    check(
        "Web 検索は、1回の依頼で決まった回数まで",
        "繰り返し終了（8 回）" in reply and "web_search を呼べる回数の上限（5 回）" in reply,
        reply,
    )
    reply, asks = await say(fresh, "繰り返して 3 汎用役に頼んで 繰り返して 4 検索して 天気")
    check("サブエージェントに検索させても、合わせて決まった回数まで", "上限（5 回）" in reply, reply)
    reply, asks = await say(fresh, "続きの質問です")
    check("上限で止まったあとも、次の依頼は受け付ける", "通目" in reply, reply)

    # ---------- 画面の HTTP ヘッダー ----------
    page = headers_of(cookie, "/")
    csp = page.get("content-security-policy", "")
    check("画面は、外部の画像を読み込まない設定で配られる", "img-src 'self' data: blob:" in csp, csp)
    check("画面は、ほかのサイトの枠に埋め込めない", "frame-ancestors 'self'" in csp, csp)
    check("中身から種類を推測させない", page.get("x-content-type-options") == "nosniff", page)

    fresh.elements.clear()
    code = (
        "import matplotlib.pyplot as plt; plt.plot([1, 2]); plt.savefig('chart.png'); "
        "open('page.html', 'w').write('<script>document.title = 1</script>'); "
        "open('pic.svg', 'w').write('<svg xmlns=\"http://www.w3.org/2000/svg\"><script>1</script></svg>')"
    )
    reply, asks = await say(fresh, f"実行して {code}")
    await asyncio.sleep(1)
    files = {e["name"]: e for e in fresh.elements}
    check("作ったファイルが画面に届く", {"chart.png", "page.html", "pic.svg"} <= set(files), list(files))

    def file_headers(name: str) -> dict[str, str]:
        element = files[name]
        return headers_of(cookie, element.get("url") or f"/project/file/{element['chainlitKey']}?session_id={fresh.session_id}")

    for name in ("page.html", "pic.svg"):
        got = file_headers(name)
        check(
            f"作った {name} は、ダウンロードとして配られ、開いてもスクリプトが動かない",
            got.get("content-disposition", "").startswith("attachment")
            and got.get("content-security-policy", "").startswith("sandbox"),
            got,
        )
    got = file_headers("chart.png")
    check(
        "画像は、その場に表示できる形で配られる", "content-disposition" not in got and got.get("content-type") == "image/png", got
    )
    await fresh.close()
    print("DONE")


async def locked() -> None:
    """既定が「毎回確認」で、利用者が切り替えられない設定のとき。"""
    cookie = login()
    chat = Chat()
    await chat.open(cookie)
    check("切り替えられない設定では、設定欄に確認のしかたが出ない", mode_setting(chat) == {}, chat.settings)
    notice = await change_mode(chat, "standard")
    check("通信で切り替えようとしても断られる", "選べません" in notice, notice)

    reply, asks = await say(chat, "書いて /l1.txt x", "reject")
    check("既定が「毎回確認」なら、切り替えなくても確かめられる", len(asks) == 1 and not exists("/l1.txt"), asks)
    check("「以後確認しない」を出さない設定では、出ない", asks[0]["choices"] == STANDARD, asks[0]["choices"])

    reply, asks = await say(chat, "検索して 今日の天気", "approve")
    check(
        "設定で毎回確かめるとしたツールは、その理由が示される",
        len(asks) == 1 and "config/security.yaml" in asks[0]["content"],
        asks,
    )

    reply, asks = await say(chat, "書いて /l2.txt x", NO_ANSWER)
    check("時間内に答えないと、実行されない", "時間内に" in reply and not exists("/l2.txt"), reply)
    check("時間切れだったことが、画面に残る", any("時間内に答えがなかった" in m for m in chat.assistant), chat.assistant)

    # 確認を待ったまま画面を閉じ、開き直して答えたあとで、閉じた側の待ち時間が切れたとき
    await chat.close()
    thread_id = list_threads(cookie)[0]["id"]
    waiting = Chat()
    await waiting.open(cookie, thread_id=thread_id)
    waiting.ask_decision = NO_ANSWER
    message = {
        "command": None, "id": str(uuid.uuid4()), "threadId": "", "name": USER, "type": "user_message",
        "output": "書いて /l4.txt x", "createdAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }  # fmt: skip
    await waiting.sio.emit("client_message", {"message": message, "fileReferences": None})
    for _ in range(20):
        if waiting.asks:
            break
        await asyncio.sleep(0.5)
    await waiting.sio.disconnect()
    back = Chat()
    back.ask_decision = "approve"
    await back.open(cookie, thread_id=thread_id)
    for _ in range(20):
        if exists("/l4.txt"):
            break
        await asyncio.sleep(0.5)
    check("開き直して承認すると、実行される", len(back.asks) == 1 and exists("/l4.txt"), back.asks)
    await asyncio.sleep(15)  # 閉じた側の待ち時間（12 秒）が切れるのを待つ
    reply, asks = await say(back, "読んで /l4.txt")
    check("閉じた側の時間切れは、済んだ操作に影響しない", "x" in reply and exists("/l4.txt"), reply)
    steps = api(cookie, "GET", f"/project/thread/{thread_id}")["steps"]
    texts = [s.get("output") or "" for s in steps]
    check(
        "閉じた側の答えは使われない",
        not any("許可しませんでした" in x and "/l4.txt" in x for x in texts),
        [x[:80] for x in texts[-6:]],
    )
    await back.close()
    chat = Chat()
    await chat.open(cookie)

    # 定時実行（無人）では、確かめる操作は自動で拒否される
    from check_schedules import last_reply, wait_for_runs

    from data_agent.settings import get_settings
    from data_agent.storage.chat_history import build_data_layer

    db = build_data_layer(get_settings())
    chat.asks.clear()
    chat.ask_decision = "confirm"
    reply = await chat.say("*/5 * * * * 書いて /l3.txt x", command="定時実行")
    check(
        "登録のときに、自動で拒否される操作が示される",
        "ファイルの作成" in chat.asks[0]["content"] and "Web 検索" in chat.asks[0]["content"],
        chat.asks,
    )
    check("登録できる", "登録しました" in reply, reply)
    await db.execute_query("""UPDATE "Schedule" SET "nextRunAt" = (now() AT TIME ZONE 'utc') - interval '1 minute'""", {})
    runs = await wait_for_runs(cookie, 1)
    check("時刻が来ると実行される", len(runs) == 1, [t["name"] for t in list_threads(cookie)])
    answer = last_reply(cookie, runs[0]["id"])
    check("無人の実行では、確かめる操作は自動で拒否される", "自動で拒否しました" in answer and not exists("/l3.txt"), answer)
    await chat.close()
    print("DONE")


if __name__ == "__main__":
    asyncio.run(locked() if sys.argv[1:] == ["locked"] else main())
