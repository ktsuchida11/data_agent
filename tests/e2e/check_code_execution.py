"""コードの実行を、画面と同じ通信（ログイン・WebSocket・API）で確かめる。

tests/e2e/fake_app.py で起動したアプリのコンテナの中で実行する。チャットが1件もない状態から始めること。
- コードを実行するコンテナ（sandbox/）を、外へ出られないネットワークで起動し、SANDBOX_URL で指しておく。
- 作業フォルダ（/app/workspace）に data/sales.csv（地域,売上 の3行。合計 245）を置いておく。
- アプリは SCHEDULER_POLL_SECONDS=2 で起動しておく。

    python tests/e2e/check_code_execution.py            # ふつうの設定
    python tests/e2e/check_code_execution.py approval   # code_execution.require_approval: true で起動したとき

環境変数 E2E_PASSWORD にログインのパスワードを渡す。キーは、形だけを合わせた偽物。
"""

from __future__ import annotations

import asyncio
import sys
import urllib.request
from pathlib import Path

from check_chat_history import BASE, Chat, check, list_threads, login
from check_schedules import last_reply, wait_for_runs

from data_agent.settings import get_settings
from data_agent.storage.chat_history import build_data_layer

WORKSPACE = Path("/app/workspace")
SECRET_MASK = "[マスク済み:秘密情報]"


def outputs() -> list[str]:
    """作業フォルダに保存されたファイル（outputs/ から見た場所）。"""
    root = WORKSPACE / "outputs"
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()) if root.exists() else []


def fetch(cookie: str, chat: Chat, element: dict) -> bytes:
    """画面が添付ファイルを取りにいくのと同じ URL で、中身を取る。"""
    url = element.get("url") or f"/project/file/{element['chainlitKey']}?session_id={chat.session_id}"
    req = urllib.request.Request(url if url.startswith("http") else f"{BASE}{url}", headers={"Cookie": cookie})
    return urllib.request.urlopen(req, timeout=10).read()


async def main() -> None:
    cookie = login()
    check("最初は一覧が空", list_threads(cookie) == [])
    chat = Chat()
    await chat.open(cookie)

    # ---------- 実行と結果 ----------
    reply = await chat.say("実行して print(6 * 7)")
    check("コードが実行され、結果がモデルに届く", "終了コード: 0" in reply and "42" in reply, reply)
    check("ファイルを作らなければ、何も保存されない", outputs() == [] and chat.elements == [], outputs())

    reply = await chat.say(
        "入力付きで実行して /data/sales.csv import pandas as pd; print(pd.read_csv('sales.csv')['売上'].sum())"
    )
    check("作業フォルダのファイルを渡して集計できる", "245" in reply, reply)

    # ---------- グラフ ----------
    chat.elements.clear()
    reply = await chat.say(
        "実行して import matplotlib.pyplot as plt; plt.bar(['東京', '大阪'], [120, 80]); plt.title('地域別の売上'); plt.savefig('chart.png')"
    )
    saved = [name for name in outputs() if name.endswith("/chart.png")]
    check("グラフが作業フォルダに保存される", len(saved) == 1, outputs())
    check("保存先のパスがモデルに伝わる", f"/outputs/{saved[0]}" in reply, reply)
    png = (WORKSPACE / "outputs" / saved[0]).read_bytes()
    check("保存されたのは PNG の画像", png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 3000, len(png))
    images = [e for e in chat.elements if e.get("type") == "image"]
    check("画面に、画像として表示される", len(images) == 1 and images[0]["name"] == "chart.png", chat.elements)
    check("画面が取りにいく画像の中身が、保存したものと同じ", fetch(cookie, chat, images[0]) == png)
    check(
        "作成したファイルの場所が、メッセージで知らされる",
        any("作成したファイル" in m and "chart.png" in m for m in chat.assistant),
        chat.assistant,
    )

    # ---------- Excel と PowerPoint ----------
    chat.elements.clear()
    await chat.say(
        "実行して import pandas as pd; pd.DataFrame({'地域': ['東京'], '売上': [120]}).to_excel('report.xlsx', index=False)"
    )
    files = [e for e in chat.elements if e.get("type") == "file"]
    check("Excel は、ダウンロードできるファイルとして出る", len(files) == 1 and files[0]["name"] == "report.xlsx", chat.elements)
    check("ダウンロードした中身が Excel の形式", fetch(cookie, chat, files[0])[:2] == b"PK")
    chat.elements.clear()
    await chat.say(
        "実行して from pptx import Presentation; p = Presentation(); s = p.slides.add_slide(p.slide_layouts[0]); s.shapes.title.text = '売上の報告'; p.save('deck.pptx')"
    )
    check("PowerPoint も、ファイルとして出る", [e["name"] for e in chat.elements] == ["deck.pptx"], chat.elements)

    # ---------- 守り ----------
    before = outputs()
    reply = await chat.say("入力付きで実行して /.env print(open('.env').read())")
    check("読めない決まりのファイルは、コードに渡せない", "permission denied" in reply, reply)
    reply = await chat.say("入力付きで実行して /.git/config print(1)")
    check("同じく、.git の中も渡せない", "permission denied" in reply, reply)

    reply = await chat.say("実行して import socket; socket.create_connection(('1.1.1.1', 53), timeout=3)")
    check("コードからインターネットに出られない", "終了コード: 1" in reply and "Error" in reply, reply)
    reply = await chat.say("実行して import socket; socket.create_connection(('postgres', 5432), timeout=3)")
    check("コードからデータベースに届かない", "終了コード: 1" in reply and "gaierror" in reply, reply)  # 名前を引けない

    reply = await chat.say("実行して print('sk-ant-api03-' + 'CODE0000' * 3)")
    check("出力に出たキーは、モデルに渡る前にマスクされる", SECRET_MASK in reply and "CODE0000CODE0000" not in reply, reply)
    reply = await chat.say("実行して open('leak.txt', 'w').write('KEY=sk-ant-api03-' + 'LEAK0000' * 3)")
    check("キーを含むファイルは保存されない", "保存しなかったファイル" in reply and "秘密情報" in reply, reply)
    check("守りに引っかかった実行では、ファイルが増えていない", outputs() == before, outputs())

    reply = await chat.say("実行して import time; time.sleep(1); raise SystemExit(3)")
    check("失敗したコードの終了コードが伝わる", "終了コード: 3" in reply, reply)

    # ---------- サブエージェント ----------
    reply = await chat.say("汎用役に頼んで 実行して print(100 + 23)")
    check("汎用のサブエージェントも、コードを実行できる", "123" in reply, reply)
    reply = await chat.say("探索役に頼んで 実行して print(100 + 23)")
    check("調べるだけのサブエージェントは、コードを実行できない", "123" not in reply and "run_python" in reply, reply)

    # ---------- 履歴 ----------
    thread_id = list_threads(cookie)[0]["id"]
    db = build_data_layer(get_settings())
    rows = await db.execute_query('SELECT count(*) AS n FROM "Element"')
    check("画像やファイルは、履歴には保存しない（作業フォルダに残る）", rows[0]["n"] == 0, rows)
    await chat.close()
    again = Chat()
    await again.open(cookie, thread_id=thread_id)
    reply = await again.say("続きの質問です")
    check("開き直しても、続きから話せる", "通目" in reply, reply)
    await again.close()

    # ---------- 定時実行（無人） ----------
    chat = Chat()
    await chat.open(cookie)
    chat.ask_decision = "confirm"
    reply = await chat.say("*/5 * * * * 実行して print(1000 + 234)", command="定時実行")
    check("コードを実行する依頼を、定時実行に登録できる", "登録しました" in reply, reply)
    await db.execute_query("""UPDATE "Schedule" SET "nextRunAt" = (now() AT TIME ZONE 'utc') - interval '1 minute'""", {})
    runs = await wait_for_runs(cookie, 1)
    check("時刻が来ると、無人で実行される", len(runs) == 1, [t["name"] for t in list_threads(cookie)])
    check("無人の実行でも、コードが動く", "1234" in last_reply(cookie, runs[0]["id"]), last_reply(cookie, runs[0]["id"]))
    await chat.close()
    print("DONE")


async def approval() -> None:
    """実行のたびに承認を求める設定（require_approval: true）のとき。"""
    cookie = login()
    chat = Chat()
    await chat.open(cookie)
    chat.ask_decision = "reject"
    reply = await chat.say("実行して open('made.txt', 'w').write('x')")
    check("承認を求められ、拒否すると実行されない", "許可しませんでした" in reply and outputs() == [], (reply, outputs()))
    chat.ask_decision = "approve"
    reply = await chat.say("実行して open('made.txt', 'w').write('x')")
    check("承認すると実行される", "終了コード: 0" in reply and len(outputs()) == 1, (reply, outputs()))
    await chat.close()
    print("DONE")


if __name__ == "__main__":
    asyncio.run(approval() if sys.argv[1:] == ["approval"] else main())
