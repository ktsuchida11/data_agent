"""用途パックと MCP（社内 API を HTTPS で）を、画面と同じ通信（ログイン・WebSocket・API）で確かめる（ADR-0032、ADR-0033）。

tests/e2e/fake_app.py で起動したアプリのコンテナの中で実行する。チャットが1件もない状態から始めること。
アプリは AGENT_PACK=internal_api_example で起動し、社内 API の見本（examples/internal_api/server.py）を、
同じネットワークの internal-api-example:8443 で動かしておく（tests/e2e/regress.sh の mcp）。

    python tests/e2e/check_mcp.py

環境変数 E2E_PASSWORD にログインのパスワードを渡す。キーは、形だけを合わせた偽物。
"""

from __future__ import annotations

import asyncio
import json

from check_approvals import change_mode, say
from check_chat_history import Chat, api, check, list_threads, login

KEY_FROM_MODEL = "sk-ant-api03-MODEL000MODEL000MODEL000"  # fake_app.py の KEY_FROM_MODEL


def call(name: str, args: dict | None = None) -> str:
    return f"ツールを呼んで {name} {json.dumps(args or {}, ensure_ascii=False)}"


async def main() -> None:
    cookie = login()
    check("最初は一覧が空", list_threads(cookie) == [])
    chat = Chat()
    await chat.open(cookie)
    check("あいさつが、用途パックのもの", any("受注照会エージェント（見本）" in m for m in chat.assistant), chat.assistant)

    # ---------- MCP のツール（HTTPS・社内の認証局・鍵） ----------
    reply, asks = await say(chat, call("orders__search_orders", {"customer": "はまなす商事"}))
    check("読むだけのツールは、確かめずに呼べる", asks == [], asks)
    check("HTTPS の社内 API から、結果が届く", "ORD-1001" in reply and "ORD-1002" in reply and "ORD-1003" not in reply, reply)

    reply, _ = await say(chat, call("orders__get_order", {"order_id": "ORD-1001"}))
    check(
        "結果の連絡先は、会話に入る前にマスクされる",
        "[マスク済み:メールアドレス]" in reply and "[マスク済み:電話番号]" in reply,
        reply,
    )
    check(
        "マスクする前の値は、モデルに届かない", "hamanasu-order@example.com" not in reply and "03-1234-5678" not in reply, reply
    )

    # 書き換えるツールは、毎回確かめる（pack.yaml の require_approval: true）
    update = call("orders__update_order_status", {"order_id": "ORD-1002", "status": "出荷済み"})
    reply, asks = await say(chat, update, "reject", "まだ出荷していない")
    check("書き換えるツールは、実行の前に確かめる", len(asks) == 1, asks)
    content = asks[0]["content"] if asks else ""
    check(
        "確認には、pack.yaml の名前と引数が出る",
        "受注の状態の変更（orders__update_order_status）" in content and "ORD-1002" in content,
        content,
    )
    check("確認では「以後確認しない」も選べる", asks and "remember" in asks[0]["choices"], asks)
    reply, _ = await say(chat, call("orders__search_orders", {"customer": "はまなす商事"}))
    check("拒否したら、書き換わらない", '"id": "ORD-1002"' in reply and "受付" in reply.split("ORD-1002")[1][:200], reply)

    reply, asks = await say(chat, update, "approve")
    check("承認したら、書き換わる", len(asks) == 1 and "出荷済み" in reply and "updated" in reply, (reply, asks))

    # pack.yaml に書いていないツールは、サーバーにあっても使えない
    reply, asks = await say(chat, call("orders__delete_order", {"order_id": "ORD-1004"}))
    check("書いていないツールは、エージェントにない", "orders__delete_order" in reply and asks == [], reply)
    reply, _ = await say(chat, call("orders__search_orders", {"customer": "ことり食品"}))
    check("書いていないツールの操作は、行われていない", "ORD-1004" in reply, reply)

    # 送る前の確かめ: モデルが自分から、鍵を引数に入れた
    reply, asks = await say(chat, "鍵を付けて呼んで orders__search_orders customer")
    check("鍵を含む引数は、送らずに止める", "送るのをやめました" in reply and "秘密情報" in reply and asks == [], reply)
    check("止めた鍵は、返事に残らない", KEY_FROM_MODEL not in reply, reply)

    # 回数の上限（pack.yaml の max_calls_per_run: 3）。確認には毎回「承認」で答える。
    # 上限は実行の直前に数えるので、4 回目も確認は出て、承認したあとで止まる（確認は 4 回）
    again = call("orders__update_order_status", {"order_id": "ORD-1003", "status": "製造中"})
    reply, asks = await say(chat, f"繰り返して 4 {again}", "approve")
    check(
        "1 回の依頼で呼べる回数の上限で止まる",
        "orders__update_order_status を呼べる回数の上限（3 回）" in reply and len(asks) == 4,
        (reply, len(asks)),
    )

    # ---------- Python で書いたツール（パックの tools.py。社内 API は呼ばない計算） ----------
    reply, asks = await say(chat, call("price_with_tax", {"amount": 1560000}))
    check("パックの Python のツールが使える", "税込みは 1,716,000 円" in reply and asks == [], reply)

    # ---------- 毎回確認のチャット ----------
    notice = await change_mode(chat, "strict")
    check("毎回確認に切り替えられる", "毎回確認" in notice, notice)
    reply, asks = await say(chat, call("orders__search_orders", {"customer": "あおば電機"}), "approve")
    check("毎回確認では、読むだけの MCP のツールも確かめる", len(asks) == 1 and "受注の検索" in asks[0]["content"], asks)
    reply, asks = await say(chat, call("price_with_tax", {"amount": 2980000}), "approve")
    check("毎回確認では、Python のツールも確かめる", len(asks) == 1 and "税込みの金額" in asks[0]["content"], asks)

    # 画面の履歴に、マスクする前の値が残っていない
    thread_id = list_threads(cookie)[0]["id"]
    saved = json.dumps(api(cookie, "GET", f"/project/thread/{thread_id}"), ensure_ascii=False)
    check("画面の履歴に、連絡先が残っていない", "hamanasu-order@example.com" not in saved and "03-1234-5678" not in saved)
    check("画面の履歴に、モデルが入れた鍵が残っていない", KEY_FROM_MODEL not in saved)
    await chat.close()


if __name__ == "__main__":
    asyncio.run(main())
