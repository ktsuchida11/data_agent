#!/usr/bin/env bash
# エージェントがデータを読みに行くデータベース（PostgreSQL）に、読み取り専用のロールを作る。
#
#   使い方:
#     bash scripts/setup_data_db.sh --sample           練習用のデータベースを作って、読めるようにする
#     bash scripts/setup_data_db.sh --columns <名前>   表と列の一覧を出し、読めなくする列と見張る列の下書きを作る
#     bash scripts/setup_data_db.sh <名前>             すでにあるデータベースを、読めるようにする
#
# - 対象は、手元の Langfuse と同じ PostgreSQL のコンテナにあるデータベース。
#   ほかの場所のデータベース（AWS の RDS など）につなぐときは、このスクリプトは使わず、
#   読み取り専用のロールを自分で作って、.env の DATA_DB_* に書く（docs/usage.md を参照）。
# - ロール coding_agent_reader を作り、指定したデータベースの表を「読むことだけ」許す。
#   データの中身や、ほかのロールの権限は変えない。Langfuse のデータベース（postgres）には触らない。
# - 個人情報の列は、config/hidden_columns.<名前>.txt に書くと、このロールから読めなくなる。
#   練習用ではないデータベースでは、このファイルが必要（--columns が下書きを作る）。
# - 顧客の名前などの列を config/watch_columns.<名前>.txt に書くと、その列だけを読める別のロール
#   coding_agent_screener を作る。アプリは、その値を見張る言葉にして、Web 検索の前に確かめる（ADR-0022）。
#   エージェントのツールは、このロールを使わない。
# - 接続情報を .env に追記する。パスワードは画面に表示しない。
# - 表を足したときや、読めなくする列を変えたときは、同じコマンドをもう一度実行する。
# - 別のデータベースの名前で実行すると、読むデータベースを切り替える（前のデータベースは読めなくなる）。
set -euo pipefail

PG_CONTAINER="${PG_CONTAINER:-langfuse-postgres-1}" # Langfuse の PostgreSQL のコンテナ名
PG_ADMIN="${PG_ADMIN:-postgres}"                    # その管理ユーザー
PG_DOCKER_HOST="${PG_DOCKER_HOST:-postgres}"        # コンテナ同士のネットワークでの、PostgreSQL の名前
DATA_SCHEMAS="${DATA_SCHEMAS:-public}"              # 読めるようにするスキーマ（空白区切り）
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT/.env}"
READER=coding_agent_reader
SCREENER=coding_agent_screener # 見張る言葉（顧客の名前など）を読むロール
SAMPLE_DB=coding_agent_sample

usage() {
  sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//' >&2
  exit 1
}

MODE=setup
case "${1:-}" in
  --sample)
    [ $# -eq 1 ] || usage
    DATA_DB="$SAMPLE_DB"
    ;;
  --columns)
    [ $# -eq 2 ] || usage
    MODE=columns
    DATA_DB="$2"
    ;;
  "" | -*) usage ;;
  *)
    [ $# -eq 1 ] || usage
    DATA_DB="$1"
    ;;
esac
HIDDEN_FILE="${HIDDEN_FILE:-$ROOT/config/hidden_columns.$DATA_DB.txt}" # 読めなくする列の一覧
WATCH_FILE="${WATCH_FILE:-$ROOT/config/watch_columns.$DATA_DB.txt}"    # 見張る言葉として読む列の一覧
# 読めるようにするときのコマンド（案内に使う）
if [ "$DATA_DB" = "$SAMPLE_DB" ]; then RERUN="bash scripts/setup_data_db.sh --sample"; else RERUN="bash scripts/setup_data_db.sh $DATA_DB"; fi

# 名前は SQL にそのまま書くので、英数字と _ だけを受け付ける
valid_name() { [[ "$1" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; }
for name in "$DATA_DB" $DATA_SCHEMAS; do
  if ! valid_name "$name"; then
    echo "名前 $name は使えません（英数字と _ だけにしてください）。" >&2
    exit 1
  fi
done
if [ "$DATA_DB" = "postgres" ]; then
  echo "postgres は Langfuse のデータベースなので、対象にできません。" >&2
  exit 1
fi

# 管理ユーザーで SQL を実行する。SQL は標準入力で渡す（第1引数は接続先のデータベース。残りは psql への引数）
admin_sql() {
  local db="${1:-postgres}"
  [ $# -gt 0 ] && shift
  docker exec -i "$PG_CONTAINER" psql -v ON_ERROR_STOP=1 -q -At -U "$PG_ADMIN" -d "$db" "$@"
}

if ! docker exec "$PG_CONTAINER" pg_isready -q -U "$PG_ADMIN"; then
  echo "PostgreSQL のコンテナ $PG_CONTAINER に接続できません。Langfuse を起動してください。" >&2
  exit 1
fi
db_exists="$(echo "select 1 from pg_database where datname = '$DATA_DB'" | admin_sql)"

# ---------- 表と列の一覧（--columns） ----------
# 列の名前だけを見る。データの中身は読まない。権限も変えない
if [ "$MODE" = columns ]; then
  if [ "$db_exists" != "1" ]; then
    echo "データベース $DATA_DB がありません。" >&2
    exit 1
  fi
  listing="$(admin_sql "$DATA_DB" -v schemas="$DATA_SCHEMAS" <<'SQL'
WITH cols AS (
  SELECT n.nspname AS sch, c.relname AS tbl, c.oid AS relid, c.reltuples::bigint AS est,
         CASE c.relkind WHEN 'v' THEN 'ビュー' WHEN 'm' THEN 'ビュー' ELSE '表' END AS kind,
         a.attnum, a.attname AS col, format_type(a.atttypid, a.atttypmod) AS typ,
         CASE
           WHEN a.attname ~* '(passw|pwd|secret|private_?key|credential|api_?key|(^|_)salt(_|$)|(access|refresh|auth|session|reset)_?token|(^|_)token$)' THEN 'パスワードや鍵らしい'
           WHEN a.attname ~* '(ip_?addr|user_?agent)' THEN '接続元の情報らしい'
           WHEN a.attname ~* '(e[-_]?mail|メール)' THEN 'メールアドレスらしい'
           WHEN a.attname ~* '(phone|mobile|(^|_)tel(_|$|no|num)|(^|_)fax|電話|携帯)' THEN '電話番号らしい'
           WHEN a.attname ~* '(address|(^|_)addr(_|$)|street|住所|番地)' THEN '住所らしい'
           WHEN a.attname ~* '(zip|postal|post_?code|郵便)' THEN '郵便番号らしい'
           WHEN a.attname ~* '(birth|(^|_)dob(_|$)|生年月日|誕生)' THEN '生年月日らしい'
           WHEN a.attname ~* '(first_?name|last_?name|full_?name|family_?name|given_?name|middle_?name|nickname|kana|氏名|姓名|フリガナ|ふりがな)' THEN '氏名らしい'
           WHEN a.attname ~* '(my_?number|マイナンバー|(^|_)ssn(_|$)|passport|license_?(no|num)|免許)' THEN '公的な番号らしい'
           WHEN a.attname ~* '(card_?(no|num)|credit_?card|カード番号)' THEN 'カード番号らしい'
           WHEN a.attname ~* '(account_?(no|num)|iban|口座)' THEN '口座番号らしい'
           WHEN a.attname ~* '(gender|(^|_)sex(_|$)|salary|income|性別|年収|給与)' THEN '機微な情報らしい'
         END AS why,
         a.attname ~* '(name|名前|名$|担当者)' AS namelike
  FROM pg_catalog.pg_class c
  JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
  JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
  WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f') AND NOT c.relispartition
    AND n.nspname = ANY (string_to_array(:'schemas', ' '))
), marked AS (
  -- 名前（name）の列は、メールアドレスなどがある表のものだけを、人の名前とみなす
  SELECT *, bool_or(why IS NOT NULL) OVER (PARTITION BY relid) AS person_table,
         CASE WHEN sch = 'public' THEN tbl ELSE sch || '.' || tbl END AS ref
  FROM cols
), named AS (
  -- 人の名前らしい列。エージェントに読ませ（ADR-0023）、Web 検索の前に見張る（ADR-0022）
  SELECT *, coalesce(why = '氏名らしい', false) OR (namelike AND person_table AND why IS NULL) AS person_name
  FROM marked
)
SELECT f || '|' || line FROM (
  -- h: 読めなくする列の一覧の行。w: 見張る列の一覧の行
  SELECT DISTINCT 'h' AS f, sch, tbl, 0 AS attnum,
         format('# ---- %s %s%s', kind, ref, CASE WHEN est > 0 THEN format('（約 %s 行）', est) ELSE '' END) AS line
  FROM named
  UNION ALL
  SELECT 'h', sch, tbl, attnum,
         CASE
           WHEN person_name THEN format('# %s.%s    # 人の名前らしい。エージェントは読める（ADR-0023）。読めなくするなら、先頭の # を消す（%s）', ref, col, typ)
           WHEN why IS NOT NULL THEN format('%s.%s    # %s（%s）', ref, col, why, typ)
           WHEN namelike THEN format('# %s.%s    # 名前の列らしい（%s）', ref, col, typ)
           ELSE format('# %s.%s    # %s', ref, col, typ)
         END
  FROM named
  UNION ALL
  SELECT 'w', sch, tbl, attnum,
         CASE
           WHEN person_name THEN format('%s.%s    # 人の名前らしい（%s）', ref, col, typ)
           ELSE format('# %s.%s    # ？ 顧客や取引先の名前なら、先頭の # を消す（%s）', ref, col, typ)
         END
  FROM named
  WHERE person_name OR namelike
) AS lines
ORDER BY f, sch, tbl, attnum;
SQL
)"
  # 表の見出し（# ----）の前に、空の行を入れる
  hidden_listing="$(echo "$listing" | sed -n 's/^h|//p' | awk '/^# ---- /{print ""} {print}')"
  watch_listing="$(echo "$listing" | sed -n 's/^w|//p')"
  if [ -z "$hidden_listing" ]; then
    echo "データベース $DATA_DB のスキーマ（${DATA_SCHEMAS}）に、表がありません。" >&2
    exit 1
  fi
  echo "データベース $DATA_DB の表と列（先頭に # のない行が、読めなくする列の候補）:"
  echo "$hidden_listing"
  echo
  made=""
  if [ -f "$HIDDEN_FILE" ]; then
    echo "読めなくする列の一覧は、すでにあります。書き換えていません: $HIDDEN_FILE"
  else
    made=yes
    {
    cat <<HEADER
# 読めなくする列の一覧（データベース: ${DATA_DB}）。scripts/setup_data_db.sh が読む。
#
# - 1行に1つ、「表.列」と書く。書いた列は、エージェントから読めなくなる。
# - 先頭に # がある行は、無視される（その列は読める）。# を消すと、読めなくなる。
# - 表をまるごと読めなくするときは、「表.*」と書く。
# - 下の印（〜らしい）は、列の名前からの推測。データの中身は見ていない。
#   印のない列にも個人情報が入っていることがあるので、必ず目で確かめること。
# - 読めなくした列がある表を使うビューは、自動で読めなくなる。
#   読ませてよいビューは、「+ビューの名前」と書く。
# - 人の名前らしい列は、先頭に # を付けてある（エージェントは読める。ADR-0023）。
#   Web 検索の前に見張るので、config/watch_columns.${DATA_DB}.txt にも書く（下書きを一緒に作る）。
# - 書き換えたら、次のコマンドをもう一度実行する。
#     $RERUN
HEADER
    echo "$hidden_listing"
    } >"$HIDDEN_FILE"
    echo "下書きを作りました: $HIDDEN_FILE"
  fi
  if [ -f "$WATCH_FILE" ]; then
    echo "見張る列の一覧は、すでにあります。書き換えていません: $WATCH_FILE"
  elif [ -n "$watch_listing" ]; then
    made=yes
    {
    cat <<HEADER
# 見張る言葉として読む列の一覧（データベース: ${DATA_DB}）。scripts/setup_data_db.sh が読む（ADR-0022）。
#
# - 1行に1つ、「表.列」と書く。書いた列の値（顧客の名前など）が Web 検索の質問文にあると、
#   検索の前に確かめる（利用者が自分で書いた場合も）。先頭に # がある行は、無視される。
# - 読むのは、この列だけを読めるロール coding_agent_screener（エージェントのツールは使わない）。
# - 下の印は、列の名前からの推測。顧客や取引先の名前の列を、目で確かめて選ぶこと。
#   「なし」「未設定」のような値が多い列を書くと、ふつうの検索でも確認が出る。
# - 書き換えたら、次のコマンドをもう一度実行する。
#     $RERUN

HEADER
    echo "$watch_listing"
    } >"$WATCH_FILE"
    echo "見張る列の下書きを作りました: $WATCH_FILE"
  fi
  if [ -n "$made" ]; then
    echo "中身を確かめて、必要なら書き換えてから、次を実行してください。"
    echo "  $RERUN"
  fi
  exit 0
fi

# ---------- 読めるようにする ----------
if [ ! -f "$ENV_FILE" ]; then
  echo "$ENV_FILE がありません。先に cp example.env .env を実行してください。" >&2
  exit 1
fi

# 読めなくする列の一覧を読む。「表.列」は HIDDEN に、「+ビュー」は ALLOWED に、カンマ区切りで入れる
HIDDEN=""
ALLOWED=""
if [ -f "$HIDDEN_FILE" ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%%#*}" # 「#」から後ろは説明
    line="$(printf '%s' "$line" | tr -d '[:space:]')"
    [ -z "$line" ] && continue
    case "$line" in
      *[,\'\"\\\;]*)
        echo "$HIDDEN_FILE に、使えない文字を含む行があります: $line" >&2
        exit 1
        ;;
    esac
    if [ "${line#+}" != "$line" ]; then
      ALLOWED="${ALLOWED:+$ALLOWED,}${line#+}"
    else
      HIDDEN="${HIDDEN:+$HIDDEN,}$line"
    fi
  done <"$HIDDEN_FILE"
elif [ "$DATA_DB" != "$SAMPLE_DB" ]; then
  echo "読めなくする列の一覧がありません: $HIDDEN_FILE" >&2
  echo "個人情報の列を読ませないために、先に次を実行して、下書きを作ってください。" >&2
  echo "  bash scripts/setup_data_db.sh --columns $DATA_DB" >&2
  exit 1
fi

# 見張る言葉として読む列の一覧を読む。「表.列」を、カンマ区切りで WATCH に入れる
WATCH=""
if [ -f "$WATCH_FILE" ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%%#*}" # 「#」から後ろは説明
    line="$(printf '%s' "$line" | tr -d '[:space:]')"
    [ -z "$line" ] && continue
    case "$line" in
      *[,\'\"\\\;+*]*)
        echo "$WATCH_FILE に、使えない文字を含む行があります（列は1つずつ書きます）: $line" >&2
        exit 1
        ;;
    esac
    WATCH="${WATCH:+$WATCH,}$line"
  done <"$WATCH_FILE"
fi

role_exists="$(echo "select 1 from pg_roles where rolname = '$READER'" | admin_sql)"
env_has_data_db=no
if grep -q '^DATA_DB_HOST=' "$ENV_FILE"; then env_has_data_db=yes; fi

if [ "$env_has_data_db" = yes ] && [ "$role_exists" != "1" ]; then
  echo "$ENV_FILE に DATA_DB_HOST がありますが、ロール $READER がありません。" >&2
  echo "$ENV_FILE から DATA_DB_ で始まる行を消してから、もう一度実行してください。" >&2
  exit 1
fi
screener_exists="$(echo "select 1 from pg_roles where rolname = '$SCREENER'" | admin_sql)"
env_has_screener=no
if grep -q '^DATA_DB_SCREENER_USER=' "$ENV_FILE"; then env_has_screener=yes; fi
if [ "$env_has_screener" = yes ] && [ "$screener_exists" != "1" ]; then
  echo "$ENV_FILE に DATA_DB_SCREENER_USER がありますが、ロール $SCREENER がありません。" >&2
  echo "$ENV_FILE から DATA_DB_SCREENER_ で始まる行を消してから、もう一度実行してください。" >&2
  exit 1
fi

# 練習用のデータベース。なければ作って、サンプルのデータを読み込む
if [ "$DATA_DB" = "$SAMPLE_DB" ] && [ "${1:-}" = "--sample" ] && [ "$db_exists" != "1" ]; then
  echo "CREATE DATABASE $SAMPLE_DB" | admin_sql
  admin_sql "$SAMPLE_DB" <"$ROOT/scripts/sample_data.sql"
  db_exists=1
  echo "練習用のデータベース $SAMPLE_DB を作りました。"
fi
if [ "$db_exists" != "1" ]; then
  echo "データベース $DATA_DB がありません。" >&2
  exit 1
fi

# 読むデータベースを切り替えるとき。前のデータベースの権限を外す
OLD_DB=""
if [ "$env_has_data_db" = yes ]; then
  OLD_DB="$(grep '^DATA_DB_NAME=' "$ENV_FILE" | head -n 1 | cut -d= -f2-)"
  if [ "$OLD_DB" = "$DATA_DB" ]; then
    OLD_DB=""
  elif ! valid_name "$OLD_DB" || [ "$OLD_DB" = postgres ]; then
    echo "$ENV_FILE の DATA_DB_NAME を読み取れませんでした。" >&2
    echo "$ENV_FILE から DATA_DB_ で始まる行を消してから、もう一度実行してください。" >&2
    exit 1
  fi
fi

# 読み取り専用のロール。パスワードはコマンドの引数に載せず、標準入力で渡す
READER_PASSWORD=""
if [ "$env_has_data_db" = no ]; then
  READER_PASSWORD="$(openssl rand -hex 24)" # 英数字だけなので、SQL にそのまま書ける
  if [ "$role_exists" = "1" ]; then
    echo "ALTER ROLE $READER LOGIN PASSWORD '$READER_PASSWORD'" | admin_sql
  else
    echo "CREATE ROLE $READER LOGIN PASSWORD '$READER_PASSWORD'" | admin_sql
  fi
fi

# 権限。読むことだけを許し、読めなくする列を外す。途中で失敗したら、全部をもとに戻す（1つのトランザクション）。
# 値は psql の変数で渡し、SQL の中では名前として正しく引用する
result="$(admin_sql "$DATA_DB" -v reader="$READER" -v schemas="$DATA_SCHEMAS" -v hidden="$HIDDEN" -v allowed="$ALLOWED" <<'SQL'
BEGIN;
\o /dev/null
SELECT set_config('coding_agent.reader', :'reader', true), set_config('coding_agent.schemas', :'schemas', true),
       set_config('coding_agent.hidden', :'hidden', true), set_config('coding_agent.allowed', :'allowed', true);
\o
DO $do$
DECLARE
  reader  text := current_setting('coding_agent.reader');
  schemas text[] := string_to_array(current_setting('coding_agent.schemas'), ' ');
  item    text;
  parts   text[];
  sch     text;
  tbl     text;
  col     text;
  rels    oid[];
  allowed oid[] := '{}';
  r       record;
  cols    text;
BEGIN
  -- 1. 読むことだけを許す。このロールで始めたトランザクションは、既定で読み取り専用にする
  EXECUTE format('ALTER ROLE %I SET default_transaction_read_only = on', reader);
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), reader);
  FOREACH sch IN ARRAY schemas LOOP
    EXECUTE format('GRANT USAGE ON SCHEMA %I TO %I', sch, reader);
    EXECUTE format('GRANT SELECT ON ALL TABLES IN SCHEMA %I TO %I', sch, reader);
  END LOOP;

  -- 2. 読めなくする列を、表と列の名前で確かめる。書き間違いは、ここで止める（気づかずに読めるままになるのを防ぐ）
  CREATE TEMP TABLE _hidden (relid oid NOT NULL, attname name) ON COMMIT DROP;  -- attname が空なら、表の全体
  FOREACH item IN ARRAY string_to_array(current_setting('coding_agent.hidden'), ',') LOOP
    CONTINUE WHEN item = '';
    parts := string_to_array(item, '.');
    IF cardinality(parts) = 2 THEN
      sch := NULL; tbl := parts[1]; col := parts[2];
    ELSIF cardinality(parts) = 3 THEN
      sch := parts[1]; tbl := parts[2]; col := parts[3];
    ELSE
      RAISE EXCEPTION '書き方が正しくありません（「表.列」と書いてください）: %', item;
    END IF;
    SELECT array_agg(c.oid) INTO rels
    FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relname = tbl AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
      AND CASE WHEN sch IS NULL THEN n.nspname = ANY (schemas) ELSE n.nspname = sch END;
    IF rels IS NULL THEN
      RAISE EXCEPTION '表 % がありません（%）', tbl, item;
    ELSIF cardinality(rels) > 1 THEN
      RAISE EXCEPTION '表 % が、複数のスキーマにあります。「スキーマ.表.列」と書いてください（%）', tbl, item;
    ELSIF col <> '*' AND NOT EXISTS (
      SELECT 1 FROM pg_catalog.pg_attribute
      WHERE attrelid = rels[1] AND attname = col AND attnum > 0 AND NOT attisdropped
    ) THEN
      RAISE EXCEPTION '表 % に、列 % がありません（%）', tbl, col, item;
    END IF;
    -- 子の表（パーティションなど）も、同じ列を読めなくする
    INSERT INTO _hidden (relid, attname)
    WITH RECURSIVE family (relid) AS (
      SELECT rels[1]
      UNION
      SELECT i.inhrelid FROM pg_catalog.pg_inherits i JOIN family f ON i.inhparent = f.relid
    )
    SELECT relid, CASE WHEN col = '*' THEN NULL ELSE col::name END FROM family;
  END LOOP;

  -- 3. その表は、表の全体への許可を外して、読ませる列だけを許す
  FOR r IN SELECT relid, bool_or(attname IS NULL) AS whole FROM _hidden GROUP BY relid LOOP
    EXECUTE format('REVOKE SELECT ON %s FROM %I', r.relid::regclass, reader);
    IF NOT r.whole THEN
      SELECT string_agg(quote_ident(a.attname), ', ' ORDER BY a.attnum) INTO cols
      FROM pg_catalog.pg_attribute a
      WHERE a.attrelid = r.relid AND a.attnum > 0 AND NOT a.attisdropped
        AND NOT EXISTS (SELECT 1 FROM _hidden h WHERE h.relid = r.relid AND h.attname = a.attname);
      IF cols IS NOT NULL THEN
        EXECUTE format('GRANT SELECT (%s) ON %s TO %I', cols, r.relid::regclass, reader);
      END IF;
    END IF;
  END LOOP;

  -- 4. ビューは、作った人の権限で中の表を読む。読めなくした列がある表を使うビューは、読めなくする
  --    （「+ビュー」と書かれたものは、読ませてよいと確かめたものとして、外さない）
  FOREACH item IN ARRAY string_to_array(current_setting('coding_agent.allowed'), ',') LOOP
    CONTINUE WHEN item = '';
    parts := string_to_array(item, '.');
    IF cardinality(parts) = 1 THEN
      sch := NULL; tbl := parts[1];
    ELSIF cardinality(parts) = 2 THEN
      sch := parts[1]; tbl := parts[2];
    ELSE
      RAISE EXCEPTION '書き方が正しくありません（「+ビューの名前」と書いてください）: +%', item;
    END IF;
    SELECT array_agg(c.oid) INTO rels
    FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relname = tbl AND c.relkind IN ('v', 'm')
      AND CASE WHEN sch IS NULL THEN n.nspname = ANY (schemas) ELSE n.nspname = sch END;
    IF rels IS NULL OR cardinality(rels) <> 1 THEN
      RAISE EXCEPTION 'ビュー % が見つからないか、複数あります（+%）', tbl, item;
    END IF;
    allowed := allowed || rels[1];
  END LOOP;
  CREATE TEMP TABLE _views (viewid oid NOT NULL) ON COMMIT DROP;
  INSERT INTO _views (viewid)
  WITH RECURSIVE dep (viewid) AS (
    SELECT w.ev_class
    FROM (SELECT DISTINCT relid FROM _hidden) h
    JOIN pg_catalog.pg_depend d
      ON d.classid = 'pg_rewrite'::regclass AND d.refclassid = 'pg_class'::regclass AND d.refobjid = h.relid
    JOIN pg_catalog.pg_rewrite w ON w.oid = d.objid
    WHERE w.ev_class <> h.relid AND w.ev_class <> ALL (allowed)
    UNION
    SELECT w.ev_class
    FROM dep
    JOIN pg_catalog.pg_depend d
      ON d.classid = 'pg_rewrite'::regclass AND d.refclassid = 'pg_class'::regclass AND d.refobjid = dep.viewid
    JOIN pg_catalog.pg_rewrite w ON w.oid = d.objid
    WHERE w.ev_class <> dep.viewid AND w.ev_class <> ALL (allowed)
  )
  SELECT viewid FROM dep;
  FOR r IN SELECT viewid FROM _views LOOP
    EXECUTE format('REVOKE SELECT ON %s FROM %I', r.viewid::regclass, reader);
  END LOOP;

  -- 5. 本当に読めなくなったかを確かめる。ほかの経路（すべてのロールへの許可など）で読めるなら、止める
  FOR r IN SELECT relid, attname FROM _hidden UNION ALL SELECT viewid, NULL FROM _views LOOP
    IF (r.attname IS NULL AND has_any_column_privilege(reader, r.relid, 'SELECT'))
       OR (r.attname IS NOT NULL AND has_column_privilege(reader, r.relid, r.attname::text, 'SELECT')) THEN
      RAISE EXCEPTION '% の % を、読めなくできませんでした。すべてのロールへの許可（PUBLIC）など、ほかの経路で読めるようになっています。データベースの管理者に、その許可を外してもらってください',
        r.relid::regclass, coalesce(r.attname::text, '全体');
    END IF;
  END LOOP;
END
$do$;
SELECT 'columns|' || count(*) FROM (SELECT DISTINCT relid, attname FROM _hidden WHERE attname IS NOT NULL) AS x;
SELECT 'tables|' || count(*) FROM (SELECT DISTINCT relid FROM _hidden WHERE attname IS NULL) AS x;
SELECT 'view|' || viewid::regclass FROM _views ORDER BY 1;
COMMIT;
SQL
)"
hidden_columns="$(echo "$result" | sed -n 's/^columns|//p')"
hidden_tables="$(echo "$result" | sed -n 's/^tables|//p')"
hidden_views="$(echo "$result" | sed -n 's/^view|//p')"

# ---------- 見張る言葉を読むロール（Web 検索の前の確認。ADR-0022） ----------
# 一覧に書いた列だけを読めるようにする。一覧が空なら、このデータベースでは何も読めなくする
SCREENER_PASSWORD=""
watch_columns=0
if [ -n "$WATCH" ] && [ "$env_has_screener" = no ]; then
  SCREENER_PASSWORD="$(openssl rand -hex 24)" # 英数字だけなので、SQL にそのまま書ける
  if [ "$screener_exists" = "1" ]; then
    echo "ALTER ROLE $SCREENER LOGIN PASSWORD '$SCREENER_PASSWORD'" | admin_sql
  else
    echo "CREATE ROLE $SCREENER LOGIN PASSWORD '$SCREENER_PASSWORD'" | admin_sql
  fi
  screener_exists=1
fi
if [ "$screener_exists" = "1" ]; then
  watch_result="$(admin_sql "$DATA_DB" -v screener="$SCREENER" -v schemas="$DATA_SCHEMAS" -v watch="$WATCH" <<'SQL'
BEGIN;
\o /dev/null
SELECT set_config('coding_agent.screener', :'screener', true), set_config('coding_agent.schemas', :'schemas', true),
       set_config('coding_agent.watch', :'watch', true);
\o
DO $do$
DECLARE
  screener text := current_setting('coding_agent.screener');
  schemas  text[] := string_to_array(current_setting('coding_agent.schemas'), ' ');
  item     text;
  parts    text[];
  sch      text;
  tbl      text;
  col      text;
  rels     oid[];
  s        record;
BEGIN
  -- 1. このロールで始めたトランザクションは、既定で読み取り専用にする。このデータベースで許していたものは、いったん外す
  --    （表への許可を外すと、列ごとの許可も外れる）
  EXECUTE format('ALTER ROLE %I SET default_transaction_read_only = on', screener);
  FOR s IN SELECT nspname FROM pg_catalog.pg_namespace WHERE nspname !~ '^pg_' AND nspname <> 'information_schema' LOOP
    EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I', s.nspname, screener);
    EXECUTE format('REVOKE ALL ON SCHEMA %I FROM %I', s.nspname, screener);
  END LOOP;
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), screener);

  -- 2. 一覧に書いた列だけを、読めるようにする。書き間違いは、ここで止める
  CREATE TEMP TABLE _watch (relid oid NOT NULL, attname name NOT NULL) ON COMMIT DROP;
  FOREACH item IN ARRAY string_to_array(current_setting('coding_agent.watch'), ',') LOOP
    CONTINUE WHEN item = '';
    parts := string_to_array(item, '.');
    IF cardinality(parts) = 2 THEN
      sch := NULL; tbl := parts[1]; col := parts[2];
    ELSIF cardinality(parts) = 3 THEN
      sch := parts[1]; tbl := parts[2]; col := parts[3];
    ELSE
      RAISE EXCEPTION '書き方が正しくありません（「表.列」と書いてください）: %', item;
    END IF;
    SELECT array_agg(c.oid) INTO rels
    FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relname = tbl AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
      AND CASE WHEN sch IS NULL THEN n.nspname = ANY (schemas) ELSE n.nspname = sch END;
    IF rels IS NULL THEN
      RAISE EXCEPTION '表 % がありません（%）', tbl, item;
    ELSIF cardinality(rels) > 1 THEN
      RAISE EXCEPTION '表 % が、複数のスキーマにあります。「スキーマ.表.列」と書いてください（%）', tbl, item;
    ELSIF NOT EXISTS (
      SELECT 1 FROM pg_catalog.pg_attribute
      WHERE attrelid = rels[1] AND attname = col AND attnum > 0 AND NOT attisdropped
    ) THEN
      RAISE EXCEPTION '表 % に、列 % がありません（%）', tbl, col, item;
    END IF;
    SELECT n.nspname INTO sch
    FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace WHERE c.oid = rels[1];
    EXECUTE format('GRANT USAGE ON SCHEMA %I TO %I', sch, screener);
    EXECUTE format('GRANT SELECT (%I) ON %s TO %I', col, rels[1]::regclass, screener);
    INSERT INTO _watch VALUES (rels[1], col::name);
  END LOOP;
END
$do$;
SELECT 'watch|' || count(*) FROM (SELECT DISTINCT relid, attname FROM _watch) AS x;
COMMIT;
SQL
)"
  watch_columns="$(echo "$watch_result" | sed -n 's/^watch|//p')"
fi
if [ -n "$SCREENER_PASSWORD" ]; then
  cat >>"$ENV_FILE" <<ENV

# Web 検索の前の確認で、見張る言葉（顧客の名前など）を読むロール。scripts/setup_data_db.sh が追記（ADR-0022）
# つなぐ先は DATA_DB_HOST と同じ。読めるのは、config/watch_columns.<データベース名>.txt に書いた列だけ
DATA_DB_SCREENER_USER=$SCREENER
DATA_DB_SCREENER_PASSWORD=$SCREENER_PASSWORD
ENV
fi

# 前のデータベースの権限を外して、.env の名前を書き換える
if [ -n "$OLD_DB" ]; then
  if [ "$(echo "select 1 from pg_database where datname = '$OLD_DB'" | admin_sql)" = "1" ]; then
    old_roles="$READER"
    if [ "$screener_exists" = "1" ]; then old_roles="$READER $SCREENER"; fi
    for old_role in $old_roles; do
      admin_sql "$OLD_DB" -v reader="$old_role" <<'SQL'
\o /dev/null
SELECT set_config('coding_agent.reader', :'reader', false);
\o
DO $do$
DECLARE
  reader text := current_setting('coding_agent.reader');
  s      record;
BEGIN
  FOR s IN SELECT nspname FROM pg_catalog.pg_namespace WHERE nspname !~ '^pg_' AND nspname <> 'information_schema' LOOP
    EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I', s.nspname, reader);
    EXECUTE format('REVOKE ALL ON SCHEMA %I FROM %I', s.nspname, reader);
  END LOOP;
  EXECUTE format('REVOKE ALL ON DATABASE %I FROM %I', current_database(), reader);
END
$do$;
SQL
    done
  fi
  # .env の DATA_DB_NAME の行だけを書き換える（名前は英数字と _ だけなので、そのまま書ける）
  if sed --version >/dev/null 2>&1; then
    sed -i "s/^DATA_DB_NAME=.*/DATA_DB_NAME=$DATA_DB/" "$ENV_FILE"
  else
    sed -i '' "s/^DATA_DB_NAME=.*/DATA_DB_NAME=$DATA_DB/" "$ENV_FILE"
  fi
fi

report_hidden() {
  if [ "${hidden_columns:-0}" != 0 ] || [ "${hidden_tables:-0}" != 0 ]; then
    echo "読めなくした列: ${hidden_columns:-0} 列。まるごと読めなくした表: ${hidden_tables:-0}（${HIDDEN_FILE}）"
  elif [ -f "$HIDDEN_FILE" ]; then
    echo "読めなくした列はありません（$HIDDEN_FILE に、有効な行がありません）。"
  else
    echo "読めなくした列はありません。個人情報の列があるときは、--columns で一覧を作ってください。"
  fi
  if [ -n "$hidden_views" ]; then
    echo "次のビューは、読めなくした列がある表を使っているので、読めなくしました:"
    echo "$hidden_views" | sed 's/^/  /'
  fi
  if [ "${watch_columns:-0}" != 0 ]; then
    echo "見張る言葉として読む列: ${watch_columns} 列（${WATCH_FILE}）。ロール $SCREENER が読みます。"
  elif [ "$screener_exists" = "1" ]; then
    echo "見張る言葉として読む列はありません（$WATCH_FILE に、有効な行がありません）。"
  else
    echo "顧客の名前などを Web 検索の前に見張るときは、$WATCH_FILE に「表.列」を書いて、もう一度実行してください。"
  fi
  if [ -n "$SCREENER_PASSWORD" ]; then
    echo "見張る言葉を読むロールの接続情報を、$ENV_FILE に追記しました。アプリを起動し直してください。"
  fi
}

if [ -n "$OLD_DB" ]; then
  echo "読むデータベースを、$OLD_DB から $DATA_DB に切り替えました。アプリを起動し直してください。"
  report_hidden
  exit 0
fi
if [ "$env_has_data_db" = yes ]; then
  if [ -n "$SCREENER_PASSWORD" ]; then
    echo "ロール $READER の権限を付け直しました（データベース: ${DATA_DB}）。"
  else
    echo "ロール $READER の権限を付け直しました（データベース: ${DATA_DB}）。.env は変更していません。"
  fi
  report_hidden
  exit 0
fi

# ホストに公開されているポート。公開されていなければ空になり、下で 5432 を使う
HOST_PORT="$(docker port "$PG_CONTAINER" 5432/tcp 2>/dev/null | head -n 1 | sed 's/.*://' || true)"

cat >>"$ENV_FILE" <<ENV

# エージェントがデータを読みに行くデータベース（読み取り専用のロール）。scripts/setup_data_db.sh が追記
DATA_DB_HOST=127.0.0.1
DATA_DB_PORT=${HOST_PORT:-5432}
DATA_DB_NAME=$DATA_DB
DATA_DB_USER=$READER
DATA_DB_PASSWORD=$READER_PASSWORD
# コンテナ（docker compose）で動かすときの接続先。compose.yaml が、上の2つの代わりに使う
DATA_DB_DOCKER_HOST=$PG_DOCKER_HOST
DATA_DB_DOCKER_PORT=5432
ENV

echo "ロール $READER を用意し、接続情報を $ENV_FILE に追記しました（データベース: ${DATA_DB}）。"
report_hidden
