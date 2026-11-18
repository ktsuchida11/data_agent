#!/bin/bash
# 画面の回帰確認。偽のモデル（tests/e2e/fake_app.py）でアプリを動かし、tests/e2e/check_*.py を順に流す。
# API キーは要らず、料金もかからない。確認ごとに、使い捨てのデータベース・コード実行のコンテナ・アプリを作り直し、
# 終わったら消す。手元の Langfuse の PostgreSQL と、動いているアプリ（data_agent-*）には触れない。
#
#   bash tests/e2e/regress.sh                    # すべて（10 分ほど）
#   bash tests/e2e/regress.sh origin grounding   # 名前を指定して、一部だけ（名前は下の NAMES）
#
# 先に、イメージを作っておく（docker compose build。data-agent:local と data-agent-sandbox:local）。
# src・tests・config・packs は、手元のものをコンテナに重ねて使う（手元の変更を、イメージを作り直さずに確かめられる）。
# ただし、依存（pyproject.toml）を足したときは、イメージを作り直す（mcp は langchain-mcp-adapters が要る）。
# 結果は logs/e2e/<日時>/ に残る（確認ごとの出力と、通らなかったときのアプリのログ）。すべて通れば、終了コード 0。
set -u

P=$(cd "$(dirname "$0")/../.." && pwd)
IMAGE="${E2E_IMAGE:-data-agent:local}"
SANDBOX_IMAGE="${E2E_SANDBOX_IMAGE:-data-agent-sandbox:local}"
APP=data-agent-e2e-app
PG=data-agent-e2e-pg
SB=data-agent-e2e-sb
NET=data-agent-e2e
SBNET=data-agent-e2e-sandbox
EX=data-agent-e2e-api   # 社内 API の見本（mcp で使う）
NAMES="boot sandbox approvals approvals-locked code code-approval db-direct db-approval db-chat guardrails model-switch schedules skill-mining archive-group grounding origin mcp history"

WANTED=" $* "
for name in "$@"; do
  case " $NAMES " in
    *" $name "*) ;;
    *) echo "知らない名前です: ${name}（使える名前: ${NAMES}）" >&2; exit 2 ;;
  esac
done
selected() { [ "$WANTED" = "  " ] && return 0; case "$WANTED" in *" $1 "*) return 0 ;; esac; return 1; }

for image in "$IMAGE" "$SANDBOX_IMAGE"; do
  if ! docker image inspect "$image" >/dev/null 2>&1; then
    echo "イメージ $image がありません。先に docker compose build を実行してください。" >&2
    exit 2
  fi
done

OUT="$P/logs/e2e/$(date +%Y%m%d-%H%M%S)"
W=$(mktemp -d "${TMPDIR:-/tmp}/data-agent-e2e.XXXXXX")
PW=$(python3 -c 'import secrets; print(secrets.token_urlsafe(16))')        # ログインのパスワード（使い捨て）
PG_PASSWORD=$(python3 -c 'import secrets; print(secrets.token_hex(16))')  # 使い捨ての PostgreSQL の管理者
EX_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')    # 社内 API の見本の鍵（使い捨て）
PASSED=0
FAILED=""
mkdir -p "$OUT"

remove_env() {
  docker rm -f "$APP" "$PG" "$SB" "$EX" >/dev/null 2>&1
  docker network rm "$NET" "$SBNET" >/dev/null 2>&1
}
cleanup() {
  remove_env
  case "$W" in */data-agent-e2e.*) rm -rf "$W" ;; esac
}
trap cleanup EXIT
trap 'exit 130' INT TERM

# 確認のしかたを変えた設定。いまの config/security.yaml を元に、その場で作る
variant() {  # variant <ファイル名> <設定 d を書き換える Python の文>
  docker run --rm --network none -v "$P/config:/c:ro" "$IMAGE" python -c "
import sys, yaml
d = yaml.safe_load(open('/c/security.yaml'))
$2
yaml.safe_dump(d, sys.stdout, allow_unicode=True, sort_keys=False)
" >"$W/$1"
}
variant security-locked.yaml "d['approvals'].update(default_mode='strict', allow_mode_change=False, allow_remember=False, timeout_seconds=12); d['web_search']['require_approval'] = True" || exit 1
variant security-approval.yaml "d['code_execution']['require_approval'] = True; d['database']['require_approval'] = True; d['web_search']['require_approval'] = True" || exit 1

wait_app() {
  docker exec "$APP" python -c '
import sys, time, urllib.request
for _ in range(60):
    try:
        urllib.request.urlopen("http://127.0.0.1:8000/", timeout=3)
        sys.exit(0)
    except Exception:
        time.sleep(1)
sys.exit("アプリが起動しなかった")
'
}

# 使い捨ての環境を作る。引数は、アプリの docker run に足す引数。
# 先頭が --example-api なら、社内 API の見本（HTTPS）も動かし、アプリを用途パック internal_api_example で起動する
up() {
  local example=0
  if [ "${1:-}" = "--example-api" ]; then example=1; shift; fi
  remove_env
  rm -rf "$W/workspace" "$W/skills" && mkdir -p "$W/workspace/data" && cp -R "$P/skills" "$W/skills" || return 1
  printf '地域,売上\n東京,120\n大阪,80\n福岡,45\n' >"$W/workspace/data/sales.csv"
  printf 'API_KEY=sk-ant-api03-FILE0000FILE0000FILE0000\nNAME=demo\n' >"$W/workspace/config.txt"  # 形だけの偽の鍵
  printf '# 確認用の見張る言葉（架空の名前）\nはまなす商事\n' >"$W/watch_terms.txt"
  python3 - "$W/app.env" "$PW" <<'PY' || return 1
import hashlib, secrets, sys
with open(sys.argv[1], "w") as f:
    f.write(f"CHAINLIT_AUTH_SECRET={secrets.token_urlsafe(48)}\nAPP_USERNAME=test\n")
    f.write(f"APP_PASSWORD_SHA256={hashlib.sha256(sys.argv[2].encode()).hexdigest()}\n")
PY
  docker network create "$NET" >/dev/null && docker network create --internal "$SBNET" >/dev/null || return 1
  docker run -d --name "$SB" --network "$SBNET" --network-alias sandbox --read-only \
    --tmpfs /work:size=512m,uid=10001,gid=10001,mode=0700 --tmpfs /tmp:size=64m \
    --cap-drop ALL --security-opt no-new-privileges:true --memory 1g --memory-swap 1g --cpus 1 --pids-limit 256 \
    "$SANDBOX_IMAGE" >/dev/null || return 1
  docker run -d --name "$PG" --network "$NET" --network-alias postgres -e POSTGRES_PASSWORD="$PG_PASSWORD" postgres:17 >/dev/null || return 1
  # 初めての起動では、初期化用の仮のサーバー（ソケットだけで待つ）が一度動いて止まり、そのあと本番のサーバーが起動する。
  # 仮のサーバーに応えてもらった時点で進むと、起動し直しの間につなぎに行って失敗するので、本番だけが応える TCP で待つ
  docker exec "$PG" sh -c 'for i in $(seq 1 60); do pg_isready -q -h 127.0.0.1 -U postgres && exit 0; sleep 1; done; exit 1' || return 1
  # データベースを作るスクリプトは、指定しないと Langfuse の PostgreSQL と .env を使うので、必ず指定する
  PG_CONTAINER="$PG" ENV_FILE="$W/app.env" bash "$P/scripts/setup_local_db.sh" || return 1
  PG_CONTAINER="$PG" ENV_FILE="$W/app.env" bash "$P/scripts/setup_data_db.sh" --sample || return 1
  if [ "$example" -eq 1 ]; then
    # 社内 API の見本。認証局の証明書を $W/example-ca に書き出し、認証局の秘密鍵はコンテナの中（メモリ）にだけ置く
    rm -rf "$W/example-ca" && mkdir -p "$W/example-ca" || return 1
    docker run -d --name "$EX" --network "$NET" --network-alias internal-api-example -e EXAMPLE_API_TOKEN="$EX_TOKEN" \
      --read-only --tmpfs /tmp --tmpfs /ca-key:uid=10001,gid=10001,mode=0700 \
      --cap-drop ALL --security-opt no-new-privileges:true \
      -v "$P/examples:/app/examples:ro" -v "$W/example-ca:/certs" \
      "$IMAGE" python examples/internal_api/server.py >/dev/null || return 1
    # 書き出した認証局の証明書で、HTTPS の入口が確かめられるまで待つ
    docker exec "$EX" python -c '
import ssl, sys, time, urllib.request
for _ in range(60):
    try:
        context = ssl.create_default_context(cafile="/certs/ca.crt")
        urllib.request.urlopen("https://localhost:8443/healthz", context=context, timeout=3)
        sys.exit(0)
    except Exception:
        time.sleep(1)
sys.exit("社内 API の見本が起動しなかった")
' || return 1
    set -- -e AGENT_PACK=internal_api_example -e EXAMPLE_API_TOKEN="$EX_TOKEN" \
      -v "$W/example-ca:/app/private/example-ca:ro" "$@"
  fi
  docker run -d --name "$APP" --network "$NET" --env-file "$W/app.env" \
    -e DB_HOST=postgres -e DB_PORT=5432 -e DATA_DB_HOST=postgres -e DATA_DB_PORT=5432 \
    -e SCHEDULER_POLL_SECONDS=2 -e SANDBOX_URL=http://sandbox:8080 \
    --read-only --tmpfs /tmp --tmpfs /app/.files:uid=10001,gid=10001,mode=0700 \
    --cap-drop ALL --security-opt no-new-privileges:true \
    -v "$P/src:/app/src:ro" -v "$P/tests:/app/tests:ro" -v "$P/config:/app/config:ro" -v "$P/packs:/app/packs:ro" \
    -v "$W/watch_terms.txt:/app/private/watch_terms.txt:ro" -v "$W/workspace:/app/workspace" -v "$W/skills:/app/skills" \
    "$@" "$IMAGE" chainlit run tests/e2e/fake_app.py --host 0.0.0.0 --port 8000 --headless >/dev/null || return 1
  docker network connect "$SBNET" "$APP" || return 1
  wait_app
}

passed() { PASSED=$((PASSED + 1)); printf 'OK    %-17s %s\n' "$1" "$2"; }
failed() { FAILED="$FAILED $1"; printf 'FAIL  %-17s %s\n' "$1" "$2"; }

# 確認の出力と、アプリのログを見て、通ったかを決める
finish() {  # finish <名前> <終了コード>
  local name=$1 code=$2 ok fail errors
  ok=$(grep -c '^OK ' "$OUT/$name.txt")
  fail=$(grep -c '^FAIL' "$OUT/$name.txt")
  docker logs "$APP" >"$OUT/$name.app.log" 2>&1
  if docker inspect "$EX" >/dev/null 2>&1; then  # 社内 API の見本（動かしたときだけ。エラーの数には入れない）
    docker logs "$EX" >"$OUT/$name.api.log" 2>&1
  fi
  errors=$(grep -cE 'Traceback|ERROR' "$OUT/$name.app.log")
  if [ "$code" -eq 0 ] && [ "$fail" -eq 0 ] && [ "$errors" -eq 0 ] && [ "$ok" -gt 0 ]; then
    rm -f "$OUT/$name.app.log"
    passed "$name" "$ok 項目"
  else
    failed "$name" "OK=$ok FAIL=$fail 終了コード=$code アプリのエラー=${errors}（$OUT/$name.txt、$name.app.log）"
  fi
}

run() {  # run <名前> [アプリに足す docker run の引数...] -- <コンテナの中で実行するコマンド...>
  local name=$1 extra=()
  shift
  while [ "$1" != "--" ]; do extra+=("$1"); shift; done
  shift
  selected "$name" || return 0
  if ! up ${extra[@]+"${extra[@]}"} >>"$OUT/setup.log" 2>&1; then
    failed "$name" "使い捨ての環境を作れなかった（$OUT/setup.log）"
    return 0
  fi
  docker exec -e E2E_PASSWORD="$PW" -e E2E_SUPER_USER=postgres -e E2E_SUPER_PASSWORD="$PG_PASSWORD" -w /app "$APP" "$@" >"$OUT/$name.txt" 2>&1
  finish "$name" $?
}

echo "画面の回帰確認（偽のモデル。料金はかかりません）。記録: $OUT"

if selected boot; then
  docker run --rm --network none -v "$P/src:/app/src:ro" -v "$P/config:/app/config:ro" -v "$P/tests:/app/tests:ro" \
    -v "$P/packs:/app/packs:ro" "$IMAGE" python tests/e2e/check_boot.py >"$OUT/boot.txt" 2>&1
  if [ $? -eq 0 ]; then passed boot "本番の起動と同じ読み方で、画面のファイルを読み込めた"; else failed boot "$(tail -1 "$OUT/boot.txt")（$OUT/boot.txt）"; fi
fi

LOCKED=(-v "$W/security-locked.yaml:/app/config/security.yaml:ro")
APPROVAL=(-v "$W/security-approval.yaml:/app/config/security.yaml:ro")
run sandbox -- python tests/e2e/check_sandbox.py  # コードを実行する箱（sandbox/）そのもの
run approvals -- python tests/e2e/check_approvals.py
run approvals-locked "${LOCKED[@]}" -- python tests/e2e/check_approvals.py locked
run code -- python tests/e2e/check_code_execution.py
run code-approval "${APPROVAL[@]}" -- python tests/e2e/check_code_execution.py approval
run db-direct -- python tests/e2e/check_database.py direct
run db-approval "${APPROVAL[@]}" -- python tests/e2e/check_database.py approval
run db-chat -- python tests/e2e/check_database.py chat
run guardrails -- python tests/e2e/check_guardrails.py
run model-switch -- python tests/e2e/check_model_switch.py
run schedules -- python tests/e2e/check_schedules.py
run skill-mining -- python tests/e2e/check_skill_mining.py
run archive-group -- python tests/e2e/check_archive_group.py
run grounding -- python tests/e2e/check_grounding.py
run origin -- python tests/e2e/check_origin.py
run mcp --example-api -- python tests/e2e/check_mcp.py  # 用途パックと、HTTPS の社内 API（MCP）、パックの Python のツール

# 過去のチャット: 1通目を送る → アプリを再起動する → 開き直して続きを送る
if selected history; then
  if up >>"$OUT/setup.log" 2>&1; then
    docker exec -e E2E_PASSWORD="$PW" -w /app "$APP" python tests/e2e/check_chat_history.py first >"$OUT/history.txt" 2>&1
    code=$?
    thread_id=$(grep '^THREAD_ID=' "$OUT/history.txt" | cut -d= -f2)
    if [ "$code" -eq 0 ] && [ -n "$thread_id" ]; then
      docker restart "$APP" >/dev/null && wait_app &&
        docker exec -e E2E_PASSWORD="$PW" -w /app "$APP" python tests/e2e/check_chat_history.py second "$thread_id" >>"$OUT/history.txt" 2>&1
      code=$?
    fi
    finish history "$code"
  else
    failed history "使い捨ての環境を作れなかった（$OUT/setup.log）"
  fi
fi

if [ -z "$FAILED" ]; then
  echo "すべて通りました（$PASSED 種類）。"
  exit 0
fi
echo "通らなかったもの:${FAILED}（通ったもの $PASSED 種類）。記録: $OUT"
exit 1
