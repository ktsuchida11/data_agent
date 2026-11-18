#!/usr/bin/env bash
# 依存ライブラリの既知の脆弱性を調べる（ADR-0024）。
#
#   使い方:
#     bash scripts/audit_deps.sh            アプリとコード実行の環境のライブラリを調べ、結果を logs/audit/ に残す
#     bash scripts/audit_deps.sh --notify   見つかったとき・調べられなかったときに、Mac の通知も出す（定期実行用）
#
# - 調べるのは、アプリのライブラリ（uv.lock）と、コードを実行する環境のライブラリ（sandbox/requirements.txt）。
# - 調べる先は、OSV（api.osv.dev。無料の脆弱性のデータベース）。送るのは、ライブラリの名前と版だけ。
#   コードを実行する環境の分は、版の一覧から仮のプロジェクトを作るので、pypi.org にもつなぐ。
# - 終了コード: 0 = 見つからなかった、1 = 見つかった、2 = 調べられなかった（ネットワークなど）。
# - 結果は logs/audit/<日時>.txt に残す（イメージには入らない）。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="${AUDIT_LOG_DIR:-$ROOT/logs/audit}"
NOTIFY=no
case "${1:-}" in
  "") ;;
  --notify) NOTIFY=yes ;;
  *)
    sed -n '4,6p' "$0" | sed 's/^# \{0,1\}//' >&2
    exit 2
    ;;
esac

if ! command -v uv >/dev/null 2>&1; then
  echo "uv が見つかりません。" >&2
  exit 2
fi

mkdir -p "$OUT_DIR"
REPORT="$OUT_DIR/$(date +%Y%m%d-%H%M%S).txt"
# 作業用の場所（macOS の mktemp は、場所を書かないと $TMPDIR を見ないので、書いておく）
TMP="$(mktemp -d "${TMPDIR:-/tmp}/audit_deps.XXXXXX")" || {
  echo "作業用の場所を作れませんでした。" >&2
  exit 2
}
trap 'rm -rf "$TMP"' EXIT

# 1つを調べる。結果の文章を report に足し、状態（ok / found <件数> / error）を返す
#   audit_one <見出し> <調べるフォルダ>
audit_one() {
  local title="$1" dir="$2" out code
  set +e
  out="$(cd "$dir" && uv audit --frozen --preview-features audit 2>&1)"
  code=$?
  set -e
  {
    echo "===== $title ====="
    echo "$out"
    echo
  } >>"$REPORT"
  # 結果の1行目（Found N known vulnerabilities and M adverse project statuses in K packages）で判定する
  local head vulns statuses
  head="$(echo "$out" | grep -E '^Found (no|[0-9]+) known vulnerabilit' | head -n 1 || true)"
  vulns="$(echo "$head" | sed -n 's/^Found \([0-9][0-9]*\) known.*/\1/p')"
  statuses="$(echo "$head" | sed -n 's/.* and \([0-9][0-9]*\) adverse project status.*/\1/p')"
  if [ $code -eq 0 ] && [ -n "$head" ]; then
    echo ok
  elif [ -n "$vulns" ] || [ -n "$statuses" ]; then
    echo "found 脆弱性 ${vulns:-0} 件、注意が要るライブラリ ${statuses:-0} 件"
  else
    echo error
  fi
}

{
  echo "依存ライブラリの点検（$(date '+%Y-%m-%d %H:%M')。$(uv --version)）"
  echo
} >"$REPORT"

# アプリのライブラリ（uv.lock をそのまま調べる）
app="$(audit_one "アプリ（uv.lock）" "$ROOT")"

# コードを実行する環境のライブラリ。版を固定した一覧（ハッシュ付き）から、同じ版だけを使う仮のプロジェクトを作る
sandbox=error
if python3 -I - "$ROOT/sandbox/requirements.txt" "$TMP/pyproject.toml" <<'PY'
import json, sys
pins = []
for raw in open(sys.argv[1], encoding="utf-8"):
    line = raw.split("#", 1)[0].rstrip().removesuffix("\\").strip()
    if line and not raw[0].isspace() and "==" in line:
        pins.append(line)
if not pins:
    sys.exit("sandbox/requirements.txt に、版を固定した行がありません")
with open(sys.argv[2], "w", encoding="utf-8") as out:
    out.write('[project]\nname = "sandbox-audit"\nversion = "0"\nrequires-python = ">=3.12,<3.13"\ndependencies = [\n')
    out.writelines(f"    {json.dumps(pin)},\n" for pin in pins)  # JSON の文字列は、TOML の文字列としても読める
    out.write("]\n")
PY
then
  set +e
  lock_out="$(cd "$TMP" && uv lock --quiet 2>&1)"
  lock_code=$?
  set -e
  if [ $lock_code -eq 0 ]; then
    sandbox="$(audit_one "コードを実行する環境（sandbox/requirements.txt）" "$TMP")"
  else
    printf '===== コードを実行する環境（sandbox/requirements.txt） =====\n仮のプロジェクトを作れませんでした:\n%s\n\n' "$lock_out" >>"$REPORT"
  fi
fi

label() {
  case "$1" in
    ok) echo "見つからなかった" ;;
    found*) echo "見つかった（${1#found }）" ;;
    *) echo "調べられなかった" ;;
  esac
}
summary="アプリ: $(label "$app")。コードを実行する環境: $(label "$sandbox")。"
echo "結果: $summary" >>"$REPORT"
echo "$summary"
echo "詳しい結果: $REPORT"

status=0
if [ "${app%% *}" = found ] || [ "${sandbox%% *}" = found ]; then
  status=1
elif [ "$app" = error ] || [ "$sandbox" = error ]; then
  status=2
fi
if [ "$NOTIFY" = yes ] && [ $status -ne 0 ]; then
  if [ $status -eq 1 ]; then message="既知の脆弱性が見つかりました。$summary"; else message="調べられませんでした。$summary"; fi
  # 通知の文は、決めた文だけにする（ライブラリの出力をそのまま渡さない）
  osascript -e "display notification \"$message\" with title \"data_agent の依存ライブラリ\"" >/dev/null 2>&1 || true
fi
exit $status
