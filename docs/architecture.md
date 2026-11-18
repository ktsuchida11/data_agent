# data_agent の仕組み（全体の地図）

この文書は、data_agent が「何でできていて、1 回の依頼がどう流れるか」を、短くまとめた地図です。
同じ内容を図で見られるページもあります: [architecture-map.html](architecture-map.html)（ダウンロードして、ブラウザで開く）。
細かい使い方は [README.md](../README.md)、決めた理由は [docs/adr/](adr/README.md)、開発するときの決まりは [AGENTS.md](../AGENTS.md) にあります。

最終更新: 2026-11-17

## 1. ひとことで

社内のデータ（作業フォルダのファイルや、データベース）を、Claude（Anthropic のモデル）が調べて分析し、まとめるチャットアプリ「データ分析エージェント」です。集計や表・グラフ作りのためにコードを実行し、Web も検索します。作り方は Claude Code を手本にしています。

- **画面:** Chainlit（Python で作るチャット画面）
- **エージェントの中身:** LangChain の Deep Agents（その土台は LangGraph）
- **モデル:** Claude Opus 5.5（既定）。画面の設定欄で Sonnet 5.5・Haiku 5.5 に切り替えられます。

**別の用途のエージェントの土台にもなります**（2026-11-17。ADR-0032）。守りと運用はそのまま使い、用途ごとに違うもの（名前とあいさつ、役割の文、使うツール、つなぐ社内の API）だけを、用途パック（`packs/<名前>/`）に書きます。どのパックで動かすかは、`.env` の `AGENT_PACK` で選びます（既定は `data`。データ分析）。手順は [docs/tutorials/](tutorials/README.md) にあります。

## 2. ほかのフォルダとの関係（いま動いているのはどれ？）

**いま動いているのは `data_agent/` だけです**（2026-11-16 に `coding_agent/` から名前を変えた。ADR-0029）。 ひとつ上の `LangChain/` には、ほかに次のものがあります。

| 場所 | 何か | 動いているか |
|---|---|---|
| `data_agent/` | この文書のアプリ（データ分析エージェント）。毎日使っているチャット | **動いている**（Docker のコンテナ `data_agent-data-agent-1`。http://127.0.0.1:8000） |
| `archive/customer_needs_agent/` | 顧客ニーズ調査エージェントの設計の叩き台（2026-10-30 に、設計書と一緒に作ったもの） | 動いていない。一度も動かしたことがない。2026-11-16 に `archive/` へ移した |
| `01_…md`、`文字起こし/`、`要約/` | Deep Agents を調べたときの資料 | 資料だけ |

data_agent は、`archive/` のものを、コードもデータベースも使っていません。

## 3. 動いているもの（全体図）

```
あなたのブラウザ（http://127.0.0.1:8000。この Mac からだけ開ける）
  │
  ▼
[data-agent] アプリのコンテナ
  │   画面（Chainlit）・エージェント（Deep Agents）・定時実行の見張り（30 秒ごと）
  │
  ├──▶ [Anthropic API]  Claude が返事を作る。Web 検索も、ここ経由
  │
  ├──▶ [sandbox]  コードを実行する専用の箱。インターネットにも、データベースにも届かない
  │
  ├──▶ [社内の API]  用途パックでつなぐと決めたものだけ。HTTPS（MCP）
  │      （手元で試すときは、見本の internal-api-example。--profile example のときだけ起動）
  │
  └──▶ [Langfuse のコンテナ群]  別の compose で起動している
         ├─ PostgreSQL（langfuse-postgres-1）
         │    ├─ coding_agent         会話・定時実行の予定・依頼の記録を保存する
         │    └─ coding_agent_sample  練習用のデータ。エージェントは読むだけ
         └─ Langfuse の画面（http://localhost:3000）  モデルとのやりとりの記録（トレース）
```

- **アプリのコンテナ**は、書き込める場所を、作業フォルダ（`workspace/`）・長期メモ（`memories/`）・手順書（`skills/`）と一時フォルダだけに絞っています。
- **sandbox** は、アプリとだけつながる閉じたネットワークにいます。エージェントが書いたコードが、外に情報を送ったり、データベースを触ったりできないようにするためです。
- **PostgreSQL** は、Langfuse と同じものを借りています（手元の構成。AWS では別に用意します。ADR-0009）。
- **社内の API** には、アプリの起動のときにつなぎます。つながらなければ、理由を知らせて起動を止めます（ADR-0033）。データ分析のパック（`data`）は、社内の API につなぎません。

## 4. 1 回の依頼の流れ

```
 ① 入口        ブラウザ → ログインの確認 → 接続元の確認（ui/connection_guard.py）
                → 画面（ui/app.py）が依頼を受け取る。秘密らしい文字列はマスクする（safety/guardrails.py）
 ② 考える      エージェント（agent.py）が Claude に依頼と道具の一覧を渡す。
                役割の文と道具の一覧は、用途パック（packs/<名前>/）で決まる
 ③ 道具を使う  Claude が道具を選ぶ。使うたびに、次の守りを通る
                  ・回数の上限（safety/limits.py）
                  ・危ない操作の前の確認ボタン（safety/approvals.py。削除、長期メモへの書き込みなど）
                  ・道具の結果と返事のマスク（safety/guard_middleware.py）
                  ・送る内容の確認（safety/search_screen.py。Web 検索と、auto にしたパックの道具。
                    見張る言葉の一覧は safety/watch_terms.py）
                  ・外に送る道具の引数の確かめ（鍵や個人情報があれば送らない。safety/guard_middleware.py）
                  どの守りを、どの道具にかけるかは、道具の扱いで決まる（tools/registry.py）
 ④ 道具の中身  ファイル: 作業フォルダの中だけ（Deep Agents の標準の道具）
                コード: run_python → sandbox で実行（tools/code_execution.py）
                データベース: 読むだけの権限で問い合わせ、結果は CSV に保存（tools/database.py）
                Web 検索: Claude の検索機能を、別の呼び出しで使う（tools/web_search.py）
                パックの道具: Python で書いたもの（packs/<名前>/tools.py）と、
                              MCP サーバーのもの（tools/mcp.py。HTTPS の社内 API など）
                手分け: サブエージェント（調べるだけの explorer と、汎用の general-purpose）
 ⑤ 返事        少しずつ画面に流す。データを読んだ回は、返事の数と URL を根拠と照合し、
                確かめられないものがあれば、下に注記を出す（safety/grounding.py）
 ⑥ 残す        会話: PostgreSQL（storage/persistence.py、storage/chat_history.py）
                監査ログ: 誰が・いつ・何をしたか（safety/audit.py。アプリのログに出る）
                トレース: Langfuse（storage/tracing.py。送る前にマスクする。
                          90 日が過ぎたら、storage/trace_retention.py が消す）
```

### 用途パックと、道具の扱い

用途パックは、用途ごとに違うものだけを書いたフォルダです（ADR-0032）。

| ファイル | 中身 |
|---|---|
| `packs/<名前>/pack.yaml` | 名前とあいさつ、役割の文のファイル、使う組み込みの道具（コードの実行・データベース・Web 検索）、Python の道具のファイル、MCP サーバー |
| `packs/<名前>/SYSTEM.md` | 役割の文（Claude への指示） |
| `packs/<名前>/tools.py` | Python で書いた道具（`@agent_tool`。任意） |

道具は、扱い（`tools/registry.py` の `ToolPolicy`）と一緒に登録します。扱いから、次のものが自動で決まります。
`uv run python -m data_agent.tools.registry` で、いまのパックの道具と扱いを一覧にできます。

| 扱い | 決まること | どこで使う |
|---|---|---|
| `require_approval` | 実行の前に確かめるか（毎回・怪しいとき・しない） | `safety/approvals.py`、`safety/search_screen.py` |
| `strict` | 「毎回確認」のチャットで確かめるか | `safety/approvals.py` |
| `check_arguments` | 送る前に、引数の鍵や個人情報を確かめるか | `safety/guard_middleware.py` |
| `max_calls_per_run` | 1 回の依頼で呼べる回数 | `safety/limits.py` |
| `grounding` | 返事の照合で、結果を根拠にするか | `safety/grounding.py` |
| `label` | 確認の画面や、定時実行の説明に出す名前 | `safety/approvals.py` |

### 定時実行の流れ

1. 入力欄の `/` から「定時実行」を選び、「分 時 日 月 曜日」と依頼文を送る（`ui/schedule_commands.py`）。確認のボタンを押すと、予定が保存される（`features/schedules.py`）。
2. アプリの中の見張りが 30 秒ごとに時刻を見て、時刻が来た予定を、画面なしで実行する（`features/schedule_runner.py`）。
3. 無人なので、確認が必要な操作は自動で断る。結果は、左の一覧の「定時実行」グループのチャットとして残る。返事の照合と注記も、画面と同じ。

**定時実行で動くのは「依頼文」です。** 決まったスキルやタスクを指定して動かす専用の仕組みではありません。時刻が来ると、登録した依頼文を、画面と同じエージェントにそのまま渡します。

- **スキルも使われます。** エージェントは、依頼文がスキルの説明に当てはまれば、そのスキル（`skills/<名前>/SKILL.md`）を読んで、その手順で進めます。画面で頼んだときと同じです。
- **確実に使わせたいときは、依頼文にスキルの名前を書きます。** たとえば `0 9 * * 1 weekly-report のスキルで、先週の売上をまとめて` です。使うかどうかを最後に決めるのはモデルなので、必ずではありませんが、名前を書けばほぼ使います。
- **登録できるのは `/` の「定時実行」からだけです。** ふつうの会話で「毎週月曜に〜して」と頼んでも、予定は作られません。エージェントには、予定を作る道具を持たせていないためです（登録は、必ず人が確認ボタンを押す。ADR-0006）。
- **時刻は cron 式（5 項目）で書きます。** 「毎朝 9 時に」のような言葉での登録には、まだ対応していません（判断待ち。ADR-0006）。間隔は 5 分以上、1 人 20 件までです（`config/security.yaml` の `schedules`）。

### スキル（手順書）の足し方

スキルは、「こういう作業のときは、この手順で進める」を書いた手順書です（`skills/<名前>/SKILL.md`）。エージェントは、依頼に当てはまるスキルだけを、必要なときに読みます。

足し方は 3 通りです。どれも、手順書になるのは、人が中身を確かめてからです。

1. **チャットで頼む（「スキル登録」。ADR-0030）:** 入力欄の `/` から「スキル登録」を選び、どんな手順書にしたいかを書いて送ります。
   - 例: `weekly-sales: 毎週の売上を地域ごとにまとめる`。先頭の「名前:」は省けます（省くと、名前も考えます）。
   - 「今やった集計を、毎週使える手順書にして」のように、そのチャットのやりとりを指して頼めます。やりとり（利用者とエージェントの発言）も、下書きの材料にするためです。
   - 下書きは軽量モデル（Haiku 5.5）が書きます（`features/skill_mining.py` の `draft_from_request`）。名前の形、すでにあるスキルとの重なり、手順の有無を確かめ、秘密らしい文字列はマスクしてから見せます。
   - 「登録する」を押すと書き出されます。気に入らないときは「見送る」を押し、書き方を変えて頼み直します。
2. **繰り返しから提案させる（「スキル提案」。ADR-0007）:** 依頼の記録から、繰り返している作業（3 回以上）を見つけて、下書きを作ります。
   - `/` から「スキル提案」を選ぶと、その場で調べて、下書きの中身と、当てはまった依頼の例を見せます。
   - チャットを開いたときにも、記録がたまっていれば（5 件以上、前回から 24 時間以上たち 5 件以上増えていれば）、裏で調べておき、次に開いたときに知らせます。
3. **ファイルを置く:** `skills/<名前>/SKILL.md` を、手で作って置きます。先頭に `name`（英小文字・数字・ハイフン）と `description`（どんなときに使うか）を書き、その下に手順を書きます。例は `skills/python-testing/SKILL.md` です。

登録したスキルは、新しいチャットと、定時実行から使われます。アプリを作り直す必要はありません。

**エージェント自身は、スキルを書けません。** `skills/` に書き込めない設定（権限の決まり）で、下書きを作る道具も持たせていません。ファイルや過去の会話に紛れた悪い指示が、手順書として残り続けないようにするためです（ADR-0007）。ふつうの会話で「スキルにして」と頼むと、エージェントは「スキル登録」を使うよう案内します。

## 5. 守りの一覧

| 守り | 何を防ぐか | どこ |
|---|---|---|
| ログイン | 知らない人が使う | `ui/app.py`（パスワードは `.env` にハッシュで置く） |
| 接続元の確認 | 別のサイトのページが、ログイン中のあなたとしてチャットにつなぐ | `ui/connection_guard.py`（ADR-0027） |
| 画面のヘッダー | 画面に入り込んだスクリプト | `ui/connection_guard.py`（ADR-0020） |
| 見える範囲 | `.env`・設定・コードを、エージェントが読む | `agent.py`（作業フォルダだけを見せる） |
| 操作の前の確認 | 削除や、長期メモの書き換えを、勝手にする | `safety/approvals.py`（ADR-0021） |
| 回数の上限 | 止まらずに料金がかかり続ける | `safety/limits.py`、`config/security.yaml` |
| マスク | 鍵・メールアドレス・電話番号（と、`custom_rules` に書いた用途ごとの番号）が、モデルや記録に流れる | `safety/guardrails.py`、`safety/guard_middleware.py`（ADR-0012、ADR-0032） |
| 送る前の引数の確かめ | 外に送る道具（MCP・Python の道具）の引数に、鍵や個人情報が入ったまま送る | `safety/guard_middleware.py`（ADR-0032） |
| 使う道具の名指し | MCP サーバーの、使うと決めていない道具（消す操作など）を使う。中身のすり替え | `tools/mcp.py`、`tools/registry.py`（ADR-0033） |
| 社内 API の HTTPS と鍵 | 偽のサーバーにつなぐ。鍵を設定ファイルに書く・別の場所に送る | `tools/internal_http.py`（社内の認証局だけを信用、リダイレクトを追わない。ADR-0033） |
| コードの隔離 | 書いたコードが、外に送る・DB を触る | `sandbox/`（ADR-0016） |
| DB の読み取り専用 | 書き換え。読ませたくない列を読む | `tools/database.py`、`scripts/setup_data_db.sh`（ADR-0017、0019、0023） |
| 送る内容の確認 | 手元のデータを、検索の質問（や、auto にしたパックの道具の引数）に入れて外に出す | `safety/search_screen.py`、`safety/watch_terms.py`（ADR-0022、ADR-0032） |
| 返事の照合と注記 | 作り話の数や URL に気づかない | `safety/grounding.py`（ADR-0026） |
| 監査ログ | 何が起きたか、後から分からない | `safety/audit.py` |

## 6. ファイルの地図（役割ごと）

`src/data_agent/` は、役割ごとのフォルダに分かれています（2026-11-16 に分けた。ADR-0028）。各フォルダの `__init__.py` の先頭にも、中のファイルの役割を書いています。

```
src/data_agent/
├─ agent.py        エージェントを組み立てる（道具・守り・サブエージェントをつなぐ）
├─ settings.py     .env と config/security.yaml と、用途パック（packs/<名前>/pack.yaml）を読む
├─ ui/             画面
│   ├─ app.py                 画面の本体（返事の表示、確認のボタン、設定欄）
│   ├─ connection_guard.py    入口の守り（接続元の確認、クッキー、画面のヘッダー）
│   └─ chat_commands.py ほか  入力欄の `/` のコマンド（アーカイブ、定時実行、スキルの提案と登録）
├─ tools/          道具
│   ├─ code_execution.py  コードの実行（run_python）
│   ├─ database.py        データベースを読む
│   ├─ web_search.py      Web 検索
│   ├─ outputs.py         道具が作ったファイルの保存
│   ├─ registry.py        道具と、その扱いの一覧。パックの Python の道具（@agent_tool）を読む
│   ├─ mcp.py             パックの MCP サーバーの道具（HTTPS の社内 API など）
│   └─ internal_http.py   MCP サーバーに HTTPS でつなぐ部品（鍵は .env、社内の認証局は ca_file）
├─ safety/         守り
│   ├─ approvals.py       危ない操作の前の確認
│   ├─ limits.py          回数の上限
│   ├─ guardrails.py      秘密らしい文字列を見つける規則
│   ├─ guard_middleware.py  その規則を、入力・道具の結果・返事に差し込む
│   ├─ search_screen.py   Web 検索の前の、質問文の確認
│   ├─ watch_terms.py     見張る言葉（顧客の名前など）の一覧を作る
│   ├─ grounding.py       返事の数と URL の照合と、注記
│   └─ audit.py           監査ログ
├─ storage/        保存と記録
│   ├─ persistence.py     会話の状態の保存
│   ├─ chat_history.py    左の一覧のチャットの保存
│   ├─ tracing.py         Langfuse への記録
│   └─ trace_retention.py Langfuse の古い記録を、毎日消す（既定は 90 日）
├─ features/       便利な機能
│   ├─ schedules.py       定時実行の予定
│   ├─ schedule_runner.py 定時実行の本体
│   └─ skill_mining.py    スキルの下書き（繰り返しからの提案と、チャットで頼まれたもの）
└─ evals/          点検（測るためのもの。ふだんは動かない。本物のモデルを呼ぶと料金がかかる）
    ├─ common.py          2 つの点検で共通の部品
    ├─ redteam/           指示の乗っ取りへの強さ（python -m data_agent.evals.redteam）
    └─ factcheck/         作り話の多さ（python -m data_agent.evals.factcheck）
```

`src/` の外は、次のとおりです。

| 場所 | 中身 |
|---|---|
| `config/` | 上限・確認・マスクなどの設定（`security.yaml`。どのパックにも共通）と、データベースで読めなくする列の一覧 |
| `packs/` | 用途パック。`data/`（データ分析）と、見本の `internal_api_example/`（社内の受注システムを MCP で調べ、税込みの金額を Python の道具で出す） |
| `examples/internal_api/` | 社内 API の見本（HTTPS と鍵つき。チュートリアルと画面の回帰確認で使う。本番では使わない） |
| `sandbox/` | コードを実行する箱（コンテナ）の作り方 |
| `scripts/` | データベースの準備と、依存ライブラリの点検 |
| `tests/` | 単体テスト（`tests/*.py`）と、画面と同じ通信での確認（`tests/e2e/`） |
| `docs/adr/` | 決めたことと、その理由（36 件。一覧は `docs/adr/README.md`、書き方のひな形は `template.md`。書き方と一覧の漏れは `tests/test_adr_records.py` が確かめる） |
| `docs/tutorials/` | 別の用途のエージェントにする手順（パックの作り方、MCP、Python の道具、確かめ方） |
| `workspace/`・`memories/`・`skills/` | エージェントが使う、作業フォルダ・長期メモ・手順書 |
| `private/` | 見張る言葉の一覧、社内の認証局の証明書など、表に出さないもの（アプリは読めて、エージェントからは見えない） |
| `.chainlit/` | 画面の設定（`config.toml`）と、画面の言葉の翻訳（Chainlit が作るもの） |
| `Dockerfile`・`compose.yaml` | コンテナで動かすための定義 |

## 7. 何をしたいときに、どこを見るか

| したいこと | 見るところ |
|---|---|
| 動かす・作り直す | `docker compose up --build`（README の「コンテナで動かす」） |
| 上限や確認の決まりを変える | `config/security.yaml`（変えたら作り直す） |
| Claude への指示を変える | `packs/<名前>/SYSTEM.md`（データ分析は `packs/data/SYSTEM.md`） |
| 別の用途のエージェントにする | `docs/tutorials/`（用途パックを作り、`.env` の `AGENT_PACK` で選ぶ） |
| 道具を足す | Python なら `packs/<名前>/tools.py`（`@agent_tool`）、MCP なら `pack.yaml` の `mcp_servers`。中身は `uv run python -m data_agent.tools.registry` |
| 社内の API をつなぐ | `pack.yaml` の `mcp_servers`（鍵は `.env`、証明書は `private/`）。確かめるのは `docker compose run --rm data-agent python -m data_agent.tools.mcp` |
| 用途ごとの番号を伏せる | `config/security.yaml` の `guardrails.custom_rules`（試すのは `uv run python -m data_agent.safety.guardrails "文章"`） |
| なぜそうなっているかを知る | `docs/adr/README.md` の一覧 |
| まだ決めていないことを見る | `docs/adr/README.md` の「オーナーの判断待ち」 |
| 何が起きたかを調べる | アプリのログの `"log": "audit"` の行と、Langfuse の画面 |
| Langfuse の記録を残す日数を変える | `config/security.yaml` の `tracing.retention_days`（既定は 90 日。0 で消さない。変えたら作り直す） |
| 変えたあとに、整えて、壊れていないか確かめる（料金なし） | `uv run ruff format .`、`uv run ruff check .`、`uv run pytest -q`、`bash tests/e2e/regress.sh` |
| モデルの強さを測る（料金あり） | `python -m data_agent.evals.redteam`、`python -m data_agent.evals.factcheck`（オーナーが実行） |
