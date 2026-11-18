# 6. 確かめる

パックやツールを変えたら、次の順に確かめます。上の 4 つは料金がかかりません。

| # | 何を確かめるか | コマンド | 時間 |
|---|---|---|---|
| 1 | パックの中身（使うツールと、その扱い） | `uv run python -m data_agent.tools.registry` | すぐ |
| 2 | MCP サーバーにつながるか、ツールの名前が合っているか | `docker compose run --rm data-agent python -m data_agent.tools.mcp` | 数秒 |
| 3 | 単体テスト（守りと、ツールの決まり） | `uv run pytest -q` | 10 秒ほど |
| 4 | 画面の回帰確認（偽のモデルで、画面と同じ通信） | `bash tests/e2e/regress.sh` | 10 分ほど |
| 5 | 本物のモデルでの点検（オーナーが実行） | 下を参照 | 数分。約 1 ドル |

## 1〜3: すぐできる確認

```bash
cd ~/pg_work_space/t_claw/LangChain/data_agent
uv run python -m data_agent.tools.registry
uv run pytest -q
```

ガードレールの独自のルールを足したときは、文章を通して確かめます（[3](03-guardrails.md)）。

```bash
uv run python -m data_agent.safety.guardrails "担当は EMP-123456"
```

## 4: 画面の回帰確認

偽のモデルでアプリを動かし、ログイン・確認のボタン・ガードレール・MCP（HTTPS の社内 API の見本）などを、画面と同じ通信でまとめて確かめます。使い捨てのコンテナとデータベースを作り、終わったら消します。動いているアプリには触れません。

```bash
docker compose build
bash tests/e2e/regress.sh
bash tests/e2e/regress.sh mcp
```

- 1 行目: 依存（`pyproject.toml`）を変えたときは、先にイメージを作り直します。
- 2 行目: すべてを流します（10 分ほど）。
- 3 行目: 名前を指定して、一部だけ流します（`mcp` は、パックと MCP の確認）。

結果は `logs/e2e/<日時>/` に残ります。「すべて通りました」と出れば、問題ありません。

## 5: 本物のモデルでの点検（料金がかかる）

指示の乗っ取りへの強さ（OWASP LLM01）と、作り話の多さ（LLM09）を、本物のモデルで測ります。API の料金がかかるので、オーナーが実行します。

```bash
uv run python -m data_agent.evals.redteam --model all
uv run python -m data_agent.evals.factcheck --model all
```

- いまの問題集は、データ分析のパック向けです（ADR-0020、ADR-0025）。
- 新しいパックで使うツール（MCP など）の点検は、問題集に足してから測ります（ADR-0033 の「次にやること」）。

## そのほか

- **依存ライブラリの脆弱性:** `bash scripts/audit_deps.sh`。毎日 9 時にも、自動で調べています（ADR-0024）。
- **Langfuse:** http://localhost:3000 で、1 回の依頼の流れ（どのツールを、どんな引数で呼んだか）を見られます。
- **監査ログ:** `docker compose logs data-agent | grep '"log": "audit"'` で、ツールの呼び出し・確認・止めた送信（`send_denied`）などを見られます。

次は [7. 実際のデータで使う前の確認の一覧](07-checklist.md)。
