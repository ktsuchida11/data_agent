#!/usr/bin/env bash
# 依存ライブラリの点検（scripts/audit_deps.sh）を、Mac で毎日、自動で動かす（ADR-0024）。
#
#   使い方:
#     bash scripts/schedule_audit.sh install   毎日 9 時に点検する（Mac の launchd に登録する）
#     bash scripts/schedule_audit.sh run       登録した点検を、いますぐ1回動かす（試すとき）
#     bash scripts/schedule_audit.sh status    登録されているかと、最後の結果を出す
#     bash scripts/schedule_audit.sh remove    登録を外す
#
# - 時刻は AUDIT_HOUR で変えられる（install のときに指定）。AUDIT_WEEKDAY（0 が日曜、1 が月曜…）を書くと、
#   その曜日だけ（週に1回）にする。毎日にしたのは、オーナーの決定（2026-11-16）。
# - Mac が眠っていた時刻の分は、次に起きたときに動く（launchd の決まり）。
# - 見つかったときと、調べられなかったときだけ、Mac の通知を出す。結果は logs/audit/ に残る。
# - 料金はかからない。調べる先は OSV（api.osv.dev）と pypi.org で、送るのはライブラリの名前と版だけ。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL=local.data-agent.audit-deps
AGENTS_DIR="${LAUNCH_AGENTS_DIR:-$HOME/Library/LaunchAgents}"
PLIST="$AGENTS_DIR/$LABEL.plist"
LAUNCHCTL="${LAUNCHCTL:-launchctl}"
DOMAIN="gui/$(id -u)"
LOG_DIR="$ROOT/logs/audit"

usage() {
  sed -n '4,8p' "$0" | sed 's/^# \{0,1\}//' >&2
  exit 1
}

case "${1:-}" in
  install)
    weekday="${AUDIT_WEEKDAY:-}" # 空なら毎日
    hour="${AUDIT_HOUR:-9}"
    if ! [[ "$weekday" =~ ^[0-6]?$ ]] || ! [[ "$hour" =~ ^([0-9]|1[0-9]|2[0-3])$ ]]; then
      echo "AUDIT_WEEKDAY は 0〜6、AUDIT_HOUR は 0〜23 で指定してください。" >&2
      exit 1
    fi
    if ! uv_path="$(command -v uv)"; then
      echo "uv が見つかりません。" >&2
      exit 1
    fi
    mkdir -p "$AGENTS_DIR" "$LOG_DIR"
    # launchd は、ターミナルの PATH を引き継がない。いまの uv の場所を書いておく
    python3 -I - "$PLIST" "$LABEL" "$ROOT" "$(dirname "$uv_path")" "$weekday" "$hour" "$LOG_DIR/launchd.log" <<'PY'
import plistlib, sys
path, label, root, uv_dir, weekday, hour, log = sys.argv[1:]
with open(path, "wb") as out:
    plistlib.dump({
        "Label": label,
        "ProgramArguments": ["/bin/bash", f"{root}/scripts/audit_deps.sh", "--notify"],
        "WorkingDirectory": root,
        "EnvironmentVariables": {"PATH": f"{uv_dir}:/usr/bin:/bin:/usr/sbin:/sbin"},
        # Weekday を書かなければ、毎日動く
        "StartCalendarInterval": {**({"Weekday": int(weekday)} if weekday else {}), "Hour": int(hour), "Minute": 0},
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }, out)
PY
    "$LAUNCHCTL" bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true # 登録し直すとき
    "$LAUNCHCTL" bootstrap "$DOMAIN" "$PLIST"
    days=(日 月 火 水 木 金 土)
    if [ -n "$weekday" ]; then when="毎週${days[$weekday]}曜の ${hour} 時"; else when="毎日 ${hour} 時"; fi
    echo "登録しました。${when}に点検します。結果は ${LOG_DIR} に残ります。"
    echo "いますぐ試すときは: bash scripts/schedule_audit.sh run"
    ;;
  run)
    if [ ! -f "$PLIST" ]; then
      echo "まだ登録されていません。先に install を実行してください。" >&2
      exit 1
    fi
    "$LAUNCHCTL" kickstart "$DOMAIN/$LABEL"
    echo "点検を始めました。1分ほどで終わります。結果は: bash scripts/schedule_audit.sh status"
    ;;
  status)
    if [ -f "$PLIST" ] && "$LAUNCHCTL" print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
      echo "登録されています（${PLIST}）。"
    elif [ -f "$PLIST" ]; then
      echo "設定のファイルはありますが、launchd に読み込まれていません。install をもう一度実行してください。"
    else
      echo "登録されていません。"
    fi
    last="$(ls -t "$LOG_DIR"/2*.txt 2>/dev/null | head -n 1 || true)"
    if [ -n "$last" ]; then
      echo "最後の結果（$(basename "$last" .txt)）: $(sed -n 's/^結果: //p' "$last")"
    else
      echo "まだ点検の結果がありません。"
    fi
    ;;
  remove)
    "$LAUNCHCTL" bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
    rm -f "$PLIST"
    echo "登録を外しました。これまでの結果（${LOG_DIR}）は残してあります。"
    ;;
  *) usage ;;
esac
