# 別の用途に使う（用途パックと MCP）

データ分析のほかの用途のエージェントにするときの、仕組みの説明です。手順は [チュートリアル](tutorials/README.md) にあります。

[README に戻る](../README.md)

## 別の用途に使う（用途パック）
用途ごとに違うもの（名前とあいさつ、役割の文、使うツール、MCP サーバー）は、用途パック（`packs/<名前>/pack.yaml` と `SYSTEM.md`）に書きます。どのパックを使うかは、`.env` の `AGENT_PACK` で選びます（書かなければ `data`。いまのデータ分析）。守りの設定（`config/security.yaml`）は、どのパックにも共通です（`docs/adr/0032-use-case-packs-and-tool-registry.md`）。

| パック | 用途 |
|---|---|
| `packs/data/` | データ分析（毎日使っているもの） |
| `packs/internal_api_example/` | 見本: 社内の受注システム（の見本）を、MCP と Python のツールで調べる |

いまのパックの中身（使うツールと、それぞれの確認・回数の上限など）は、次で確かめられます（何も実行しません）。
```bash
uv run python -m data_agent.tools.registry
```
ツールの扱い（確認、回数の上限、送る前の引数の確かめ、返事の照合、確認の画面の名前）は、ツールと一緒に `tools/registry.py` の `ToolPolicy` に書きます。Python のツールは、パックの `tools.py` に `@agent_tool` を付けて書きます。
ガードレールは、どのツールの結果にも効きます。用途ごとの番号（社員番号など）は、`config/security.yaml` の `guardrails.custom_rules` に正規表現で足せます（試すときは `uv run python -m data_agent.safety.guardrails "文章"`）。

## 社内 API をつなぐ（MCP）
社内の API が MCP サーバー（HTTP）になっていれば、パックの `pack.yaml` の `mcp_servers` に書くだけでつなげます（`docs/adr/0033-mcp-internal-api-over-https.md`）。
- **使うツール:** `tools` に書いたものだけ。名前は「サーバー__ツール」です。
- **確認:** 既定は「毎回」です。
- **鍵:** `.env` に置き、`pack.yaml` には `${環境変数名}` と書きます。
- **社内の認証局の証明書:** `private/` に置き、`ca_file` で指定します。
- **つながらないとき:** 起動を止めて、理由を知らせます。

```bash
docker compose run --rm data-agent python -m data_agent.tools.mcp
```
サーバーにつないで、ツールの一覧と説明を出します（実行はしません）。
手元で試すための社内 API の見本（架空の受注システム。HTTPS と鍵つき）は、`docker compose --profile example up -d internal-api-example` で起動します（[チュートリアル 4](tutorials/04-mcp.md)）。社内の API は、MCP でつなぐ前提です（Python のツールから API を直接呼ぶ部品は、用意していません）。計算などのツールは、Python で足せます（[チュートリアル 5](tutorials/05-python-tool.md)）。
