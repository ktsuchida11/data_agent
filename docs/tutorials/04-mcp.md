# 4. 社内 API を MCP でつなぐ

社内の API が MCP サーバー（HTTP でつなぐもの）として公開されていれば、コードを書かずに、パックの `pack.yaml` に書くだけでつなげます（ADR-0033）。
前半で、手元の見本（架空の受注システム）で試します。後半で、本物の社内 API につなぎます。

## つないだツールに効く守り

- **使うツールの名指し:** `pack.yaml` に書いたツールだけを使います。サーバーにほかのツールがあっても、エージェントには見せません。
- **鍵:** `.env` に置きます。`pack.yaml` には `${環境変数名}` と書きます（鍵をそのまま書くと、起動の前に止まります）。
- **HTTPS:** HTTPS でつなぎます。社内の認証局の証明書を `ca_file` に指定すると、その認証局の証明書だけを信用します。
- **確認:** 実行の前の確認は、既定で「毎回」です。読むだけのツールは、確かめないようにできます。
- **結果:** 連絡先などは、会話に入る前に伏せます。長すぎる結果は切ります。
- **送る前の確かめ:** 引数に鍵や個人情報があれば、送らずに止めます。
- **つながらないとき:** アプリの起動を止めて、理由（鍵が違う、証明書を確かめられない、など）を知らせます。

## 前半: 見本で試す

見本は `examples/internal_api/server.py` です。架空の受注システムを、HTTPS（自分で作った認証局の証明書）と鍵で公開します。ツールは 4 つあり、見本のパック（`packs/internal_api_example/`）は、そのうち 3 つを使います（`delete_order` は書いていないので、使えません）。

1. 見本の鍵を作り、`.env` に足します。
   ```bash
   cd ~/pg_work_space/t_claw/LangChain/data_agent
   T=$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')
   printf '\nEXAMPLE_API_TOKEN=%s\n' "$T" >> .env
   ```
2. 見本のサーバーを起動します。ふだんは起動しない設定（`--profile example`）なので、名前を指定して起動します。
   ```bash
   docker compose --profile example up -d internal-api-example
   docker compose logs internal-api-example
   ```
   ログに `社内 API の見本: https://internal-api-example:8443/mcp` と出れば、起動しています。認証局の証明書は `private/example-ca/ca.crt` に書き出されます。
3. 見本のパックに切り替えて、作り直します。
   ```bash
   printf 'AGENT_PACK=internal_api_example\n' >> .env
   docker compose up --build -d
   ```
4. つながったかを確かめます。サーバーにつないでツールの一覧を出すだけで、ツールは実行しません。
   ```bash
   docker compose run --rm data-agent python -m data_agent.tools.mcp
   ```
   次のように出れば、つながっています（`使わない` は、`pack.yaml` に書いていないツール）。
   ```
   ■ orders（https://internal-api-example:8443/mcp）
     [使わない] delete_order(order_id)  受注を消す。見本の pack.yaml には書いていないので、…
     [使う  ] get_order(order_id)  受注番号（ORD-1001 など）で、受注の詳細（担当者の連絡先を含む）を返す。
     [使う  ] search_orders(customer)  取引先の名前（一部でもよい）で、受注を探す。受注番号・品目・…
     [使う  ] update_order_status(order_id, status)  受注の状態を変える（受付・製造中・出荷済み・…
   ```
   （説明は、長いものを途中で省いています。）
5. 画面（http://127.0.0.1:8000）で試します。モデルを呼ぶので、1 回あたり数円かかります。
   - 「はまなす商事の受注を一覧にして」: `受注の検索` を使って答えます（確認は出ません）。
   - 「ORD-1001 の担当者の連絡先は？」: 連絡先は `[マスク済み:メールアドレス]` のように伏せられます。
   - 「ORD-1001 の税込みの金額は？」: `受注の詳細` で金額を見てから、Python で書いたツール（`税込みの金額`）で計算します（[5](05-python-tool.md) の見本）。
   - 「ORD-1002 を出荷済みにして」: 実行の前に確認のボタンが出ます。「拒否」を押すと、書き換わりません。
6. 終わったら、元に戻します。
   ```bash
   sed -i '' '/^AGENT_PACK=/d' .env
   docker compose up --build -d
   docker compose --profile example stop internal-api-example
   ```

## 後半: 本物の社内 API につなぐ

1. **API の担当者に、次の 3 つをもらいます。**
   - MCP の入口の URL（`https://…/mcp` のようなもの。streamable HTTP）
   - 鍵（トークン）。読むだけの権限のものがあれば、それをもらいます。
   - 社内の認証局の証明書（PEM の形。`-----BEGIN CERTIFICATE-----` で始まるファイル）。公的な認証局の証明書を使っているなら要りません。
2. **証明書を `private/` に置きます。** たとえば `private/orders-ca.crt`。`private/` は、アプリからは読めて、エージェントからは見えない場所です。
3. **鍵を `.env` に足します。** 名前は自由です（例: `ORDERS_API_TOKEN`）。
   ```bash
   printf '\nORDERS_API_TOKEN=%s\n' 'もらった鍵' >> .env
   ```
   （`'もらった鍵'` を、実際の鍵に置き換えます。ターミナルの履歴に残したくなければ、エディタで `.env` に書きます。）
4. **パックの `pack.yaml` に、サーバーを書きます。** 最初は、使うと分かっているツールを 1 つだけ書きます。
   ```yaml
   mcp_servers:
     orders:                     # 英小文字・数字・_。ツールの名前は「orders__…」になる
       url: https://orders.example.internal/mcp
       headers:
         Authorization: Bearer ${ORDERS_API_TOKEN}
       ca_file: private/orders-ca.crt
       tools:
         search_orders:
           label: 受注の検索
           require_approval: false
   ```
5. **ツールの一覧を確かめます。** 説明（モデルに見せる文）も読みます。知らないサーバーの説明に、おかしな指示が書かれていないかを見ます。
   ```bash
   docker compose run --rm data-agent python -m data_agent.tools.mcp
   ```
6. **使うツールを、`tools` に書き足します。** ツールごとに、次の扱いを決めます。

   | 項目 | 意味 | 決め方 |
   |---|---|---|
   | `label` | 確認の画面などに出す名前 | 利用者に分かる名前 |
   | `require_approval` | 実行の前に確かめるか。`true`（毎回）・`auto`（送る内容が怪しいときだけ）・`false` | 書き換える・送る・消すものは `true`。読むだけなら `false`。社外に送るものは `auto` か `true` |
   | `max_calls_per_run` | 1 回の依頼で呼べる回数 | 料金がかかるもの、重いもの、書き換えるものに付ける |
   | `grounding` | 返事の数の照合で、結果を根拠にするか | 数を返すツールは `true` |
   | `description` | モデルに見せる説明を、自分で書いたものにする | サーバーの説明が長い・分かりにくい・信用できないとき |

   サーバーの側の設定（`url` と同じ深さ）には、次のものもあります。
   - `timeout_seconds`（既定 30 秒）
   - `max_output_chars`（結果の長さの上限。既定 20,000 文字）
   - `check_arguments`（送る前の確かめ。既定は `true`）
   - `enabled`（`false` にすると使わない）
7. **パックの中身を確かめてから、作り直します。**
   ```bash
   uv run python -m data_agent.tools.registry
   docker compose up --build -d
   ```
8. **役割の文（`SYSTEM.md`）に、ツールの使い方を書きます。** どんなときにどのツールを使うか、書き換えるのはどんなときか、を短く書きます。

## うまくいかないとき

アプリが起動しないときは、`docker compose logs data-agent` に理由が出ます。

| 出た知らせ | 直し方 |
|---|---|
| `鍵が違うか、権限がありません（401）` | `.env` の鍵と、`pack.yaml` の `${…}` の名前を確かめる |
| `サーバーの証明書を確かめられませんでした。ca_file を確かめてください` | `ca_file` が、その API の証明書を発行した認証局のものかを確かめる |
| `サーバーにつながりませんでした` | URL と、コンテナから社内のネットワークに届くか（VPN など）を確かめる |
| `pack.yaml に書いたツールがありません` | 名前を、`python -m data_agent.tools.mcp` の一覧のとおりに直す |
| `${ORDERS_API_TOKEN} が、.env（環境変数）にありません` | `.env` に足して、作り直す |
| `url が http:// です` | `https://` にする（同じ PC の中だけで使うときに限り、`allow_http: true`） |

## まだできないこと

- **ログインして鍵をもらう形:** OAuth のこと。まだ使えません。
- **クライアント証明書（mTLS）:** まだ使えません。
- **プロキシを通る API:** まだ使えません。
- **アプリの中で MCP サーバーのプログラムを動かす形（stdio）:** 使うときは、別のコンテナで HTTP にして動かします。

次は [5. 自分のツールを Python で足す](05-python-tool.md)。
