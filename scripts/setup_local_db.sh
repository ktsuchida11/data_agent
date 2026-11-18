#!/usr/bin/env bash
# 手元の Langfuse と同じ PostgreSQL に、data_agent 用のデータベースとロールを作る。
#
#   使い方: bash scripts/setup_local_db.sh
#
# - Langfuse のデータベース（postgres）には手を加えない。別のデータベース coding_agent を作り、
#   その持ち主としてロール coding_agent を作る。
# - ロールがすでにあれば、何も変更しない（パスワードも変えない）。
# - 新しく作った場合は、接続情報を .env に追記する。パスワードは画面に表示しない。
set -euo pipefail

PG_CONTAINER="${PG_CONTAINER:-langfuse-postgres-1}" # Langfuse の PostgreSQL のコンテナ名
PG_ADMIN="${PG_ADMIN:-postgres}"                    # その管理ユーザー
ENV_FILE="${ENV_FILE:-$(cd "$(dirname "$0")/.." && pwd)/.env}"
DB_NAME=coding_agent
DB_USER=coding_agent

# 管理ユーザーで SQL を実行する。SQL は標準入力で渡す（第1引数は接続先のデータベース）
admin_sql() {
  docker exec -i "$PG_CONTAINER" psql -v ON_ERROR_STOP=1 -q -At -U "$PG_ADMIN" -d "${1:-postgres}"
}

if [ ! -f "$ENV_FILE" ]; then
  echo "$ENV_FILE がありません。先に cp example.env .env を実行してください。" >&2
  exit 1
fi
if ! docker exec "$PG_CONTAINER" pg_isready -q -U "$PG_ADMIN"; then
  echo "PostgreSQL のコンテナ $PG_CONTAINER に接続できません。Langfuse を起動してください。" >&2
  exit 1
fi

if [ "$(echo "select 1 from pg_roles where rolname = '$DB_USER'" | admin_sql)" = "1" ]; then
  echo "ロール $DB_USER はすでにあります。何も変更しませんでした。"
  echo "接続できない場合は、$ENV_FILE の DB_PASSWORD を確認してください。"
  exit 0
fi
if grep -q '^DB_PASSWORD=' "$ENV_FILE"; then
  echo "$ENV_FILE に DB_PASSWORD がありますが、ロール $DB_USER がありません。" >&2
  echo "$ENV_FILE から DB_ で始まる行を消してから、もう一度実行してください。" >&2
  exit 1
fi

DB_PASSWORD="$(openssl rand -hex 24)" # 英数字だけなので、SQL にそのまま書ける
# ホストに公開されているポート。公開されていなければ空になり、下で 5432 を使う
HOST_PORT="$(docker port "$PG_CONTAINER" 5432/tcp 2>/dev/null | head -n 1 | sed 's/.*://' || true)"

# パスワードはコマンドの引数に載せず、標準入力で渡す
if ! admin_sql <<SQL; then
CREATE ROLE $DB_USER LOGIN PASSWORD '$DB_PASSWORD';
CREATE DATABASE $DB_NAME OWNER $DB_USER;
REVOKE ALL ON DATABASE $DB_NAME FROM PUBLIC;
SQL
  echo "作成の途中で失敗しました。作りかけのロールを消します。" >&2
  echo "DROP ROLE IF EXISTS $DB_USER" | admin_sql || true
  exit 1
fi

# pgvector が入っている PostgreSQL なら、有効にしておく（意味検索を足すときに使う）
if [ "$(echo "select 1 from pg_available_extensions where name = 'vector'" | admin_sql)" = "1" ]; then
  echo "CREATE EXTENSION IF NOT EXISTS vector" | admin_sql "$DB_NAME"
  echo "pgvector を有効にしました。"
else
  echo "この PostgreSQL には pgvector が入っていません（会話の保存には不要です）。"
fi

cat >>"$ENV_FILE" <<ENV

# 会話の保存先（Langfuse と同じ PostgreSQL）。scripts/setup_local_db.sh が追記
DB_HOST=127.0.0.1
DB_PORT=${HOST_PORT:-5432}
DB_NAME=$DB_NAME
DB_USER=$DB_USER
DB_PASSWORD=$DB_PASSWORD
ENV

echo "データベース $DB_NAME とロール $DB_USER を作り、接続情報を $ENV_FILE に追記しました。"
