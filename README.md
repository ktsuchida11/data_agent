# data-agent（データ分析エージェント）

LangChain Deep Agents + LangGraph で作る、社内のデータ（ファイルやデータベース）を調べて分析するエージェントです。Claude Code の作り方を手本にしています（2026-11-16 に coding_agent から名前を変えた。ADR-0029）。
チャット画面は Chainlit、LLM は Anthropic API を使います。
設計の考え方は、ひとつ上のフォルダの `01_DeepAgents利用法とClaudeCode風エージェントの作り方.md` を参照してください。
別の用途のエージェントの土台としても使えます（[別の用途に使う](docs/packs.md)）。

## 動かし方

```bash
cd data_agent
uv sync --locked
cp example.env .env         # ANTHROPIC_API_KEY・ログイン情報を設定
uv run chainlit run src/data_agent/ui/app.py --host 127.0.0.1 --port 8000
```

ブラウザで http://127.0.0.1:8000 を開き、`.env` に設定したユーザー名とパスワードでログインします。
コンテナで動かす手順（コードの実行を使うときは、こちら）と、会話の保存先・Langfuse の準備は、[動かし方と準備](docs/setup.md) にあります。

## できること

- **チャットで調べて、まとめる:** 作業フォルダのファイル、データベース（読み取りだけ）、Web 検索を使います。
- **コードを実行する:** 集計、グラフ、Excel・PowerPoint・Word のファイル作り。専用のコンテナの中で動きます。
- **繰り返しを楽にする:** 定時実行、スキル（手順書）の提案と登録、過去のチャットの一覧。
- **守り:** 危ない操作の前の確認、回数の上限、鍵や個人情報のマスク、Web 検索の前の確認、返事の数の照合、監査ログ。

## フォルダの地図

| フォルダ | 中身 |
|---|---|
| `src/data_agent/` | アプリ本体（画面、道具、守り、保存）。中の地図は [docs/architecture.md](docs/architecture.md) の「6. ファイルの地図」 |
| `packs/` | 用途パック。Claude への指示（`SYSTEM.md`）と、使う道具（`pack.yaml`・`tools.py`） |
| `config/` | 守りの設定（`security.yaml`）と、データベースで読めなくする列・見張る列の一覧 |
| `sandbox/` | コードを実行する、別のコンテナ |
| `tests/` | 単体テストと、画面の回帰確認（`tests/e2e/`） |
| `evals/` | モデルの強さを測る点検（料金がかかる。アプリには入れない） |
| `scripts/` | データベースの準備と、依存ライブラリの点検 |
| `docs/` | 説明、決めたこと（`docs/adr/`）、別の用途にする手順（`docs/tutorials/`） |
| `examples/internal_api/` | 社内 API の見本のサーバー（手順書と回帰確認だけで使う。アプリには入れない） |
| `workspace/`・`memories/`・`skills/` | エージェントが使う場所（作業フォルダ、長期メモ、手順書） |
| `private/` | 表に出さないもの（見張る言葉、社内の認証局の証明書） |
| `.chainlit/` | 画面の設定（`config.toml`）と、画面の言葉の翻訳 |
| `.github/workflows/` | GitHub の CI（整形・点検・単体テスト） |

守りのために、分けているフォルダがあります。

- エージェントから見えるのは、`workspace/`・`memories/`・`skills/` だけです。`private/` は、アプリは読めますが、エージェントからは見えません。
- `examples/internal_api/` には、見本の認証局の秘密鍵も置きます。アプリのコンテナには渡しません。

## 設定の置き場所

| 変えたいこと | ファイル |
|---|---|
| API キー、ログイン、モデル、用途パック（`AGENT_PACK`）、データベースの接続先 | `.env`（ひな形は `example.env`） |
| 操作の前の確認、回数の上限、マスク、Web 検索、ログインの失敗の制限、記録を残す日数 | `config/security.yaml` |
| Claude への指示、あいさつ、使う道具、MCP サーバー | `packs/<名前>/` の `SYSTEM.md`・`pack.yaml` |
| データベースで読めなくする列と、見張る列（変えたら `bash scripts/setup_data_db.sh <DB名>`） | `config/hidden_columns.<DB名>.txt`・`config/watch_columns.<DB名>.txt` |
| 見張る言葉（顧客の名前など）、社内の認証局の証明書 | `private/`（書き方は `private/watch_terms.example.txt`） |
| 画面を開く URL（接続元）など、画面の設定 | `.chainlit/config.toml` |

コンテナで動かしているときは、`config/` と `packs/` を変えたら作り直します（`docker compose up --build`）。

## 資料の案内

| 知りたいこと | 資料 |
|---|---|
| 動かし方と準備（コンテナ、会話の保存先、Langfuse） | [docs/setup.md](docs/setup.md) |
| 使い方（過去のチャット、定時実行、スキル、コードの実行、データベース、Web 検索、モデルの切り替え） | [docs/usage.md](docs/usage.md) |
| 守りの仕組み（操作の前の確認、回数の上限、Web 検索の前の確認、マスクと監査ログ、返事の照合、接続元の検査、エージェントから見えるもの） | [docs/safety.md](docs/safety.md) |
| 開発と確かめ（点検・整形・テスト、画面の回帰確認、安全性の点検） | [docs/development.md](docs/development.md) |
| 別の用途に使う（用途パック、MCP） | [docs/packs.md](docs/packs.md)、手順は [docs/tutorials/](docs/tutorials/README.md) |
| 全体の仕組みと、ファイルの地図 | [docs/architecture.md](docs/architecture.md)（図で見るときは [docs/architecture-map.html](docs/architecture-map.html) をブラウザで開く） |
| 決めたことと、その理由 | [docs/adr/README.md](docs/adr/README.md) |
| 進捗（フェーズ） | [docs/progress.md](docs/progress.md) |
| 開発する人と AI エージェント向けの決まり | [AGENTS.md](AGENTS.md)（`CLAUDE.md` は、Claude Code に同じものを読ませるための 1 行） |

## コードを変えたら（料金なし）

```bash
uv run ruff format .
uv run ruff check .
uv run pytest -q
```

GitHub に送ると、同じ 3 つを CI（GitHub Actions）も流します。画面の回帰確認と、安全性の点検は [開発と確かめ](docs/development.md) にあります。

## ライセンスとセキュリティ

MIT ライセンスです（[LICENSE](LICENSE)）。脆弱性を見つけたときは、公開の Issue には書かず、[SECURITY.md](SECURITY.md) の手順で知らせてください。
