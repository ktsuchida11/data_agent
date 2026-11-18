# 動かし方と準備

手元やコンテナで動かす手順と、会話の保存先・Langfuse の準備です。使い方は [使い方](usage.md)、守りの設定は [守りの仕組み](safety.md) にあります。

[README に戻る](../README.md)

## 手元で動かす
```bash
cd data_agent
uv sync --locked
cp example.env .env         # ANTHROPIC_API_KEY・ログイン情報を設定
uv run chainlit run src/data_agent/ui/app.py --host 127.0.0.1 --port 8000
```
ブラウザで http://127.0.0.1:8000 を開き、`.env` に設定したユーザー名とパスワードでログインします。

## コンテナで動かす
AWS（ECS）に載せる前の確認用です。`.env` はイメージに入れず、起動時に渡します。
会話は Langfuse と同じ PostgreSQL に保存するので、Langfuse が起動している必要があります。
```bash
cd data_agent
cp example.env .env             # まだ作っていない場合。ANTHROPIC_API_KEY・ログイン情報を設定
bash scripts/setup_local_db.sh  # 初回だけ。会話の保存先を作る
docker compose up --build       # 止めるときは Ctrl+C。片付けは docker compose down
```
開く URL とログイン方法は上と同じです。`workspace/` と `memories/` は手元のフォルダをそのまま使います。
コードを実行するための専用のコンテナ（`sandbox`）も、一緒に起動します。
データベースを読ませる設定（`scripts/setup_data_db.sh`）をしてあれば、コンテナの中からも同じデータベースにつながります。
`uv run` で起動したものと同時には動かせません（どちらも 8000 番ポートを使うため）。

## 会話を PostgreSQL に保存する
何も設定しなければ、会話はメモリ上に保存され、再起動で消えます。
手元で動いている Langfuse と同じ PostgreSQL に保存するには、次を1回だけ実行します。
```bash
bash scripts/setup_local_db.sh
```
Langfuse の PostgreSQL の中に、データベース `coding_agent` と、それだけを使えるロールを作り、接続情報を `.env` に追記します。Langfuse 自身のデータベースには手を加えません。
`.env` に `DB_HOST` があれば PostgreSQL に保存し、つながらないときは起動を止めます。

保存しているときに使える機能（過去のチャット、定時実行、スキルの提案）は、[使い方](usage.md) にあります。

## エージェントに読ませるデータベース
準備のしかたと使い方は、[使い方](usage.md) の「データベースからデータを取る」にあります。

## Langfuse へのトレースの送信（任意）
モデルに何を渡し、何が返り、どのツールを呼んだかを、Langfuse の画面で追えるようにします。

1. Langfuse の画面で、新しいプロジェクトを作る（例: `data-agent`）。
2. そのプロジェクトの Settings → API Keys で、鍵を作る。
3. `.env` に `LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY`、`LANGFUSE_BASE_URL` を足す（書き方は `example.env`）。

鍵を設定しなければ、何も送りません。送る内容は、ガードレール（[守りの仕組み](safety.md)）でマスクしてから送ります（`docs/adr/0015-langfuse-tracing.md`）。

- **残す期間:** 送った記録は、90 日が過ぎたら、アプリが 1 日に 1 回消します。日数は `config/security.yaml` の `tracing.retention_days` で変えられます（0 にすると消しません。変えたら作り直します）。消すのは、このアプリが送った記録だけです（`docs/adr/0031-langfuse-trace-retention.md`）。
- 消す対象が何件あるかは、次のコマンドで確かめられます（消しません。`--delete` を付けると、すぐに消します）。
  ```bash
  uv run python -m data_agent.storage.trace_retention
  ```
