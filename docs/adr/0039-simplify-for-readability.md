# ADR-0039: 読みやすさのために、フォルダの地図を README に置き、重複をまとめ、画面のファイルを分け、点検の道具をアプリの外に出す

- **状態:** 採用（実装済み）。オーナーが決めた（2026-11-19。「おすすめ作業を順番に対応してください」）
- **日付:** 2026-11-19
- **決定者:** プロジェクトオーナー
- **対象:** data_agent

## 状況

- オーナーから、次の声があった（2026-11-18〜19）。
  - 「なるべく簡素にわかりやすいコードで重複が少ないものが読みやすく、わかりやすいので」
  - 「もう一度コードのレビューをおねがいします。たくさんフォルダあって分かりにくかたんのです」
- レビューで分かったこと:
  - **フォルダが多い理由は 2 種類あり、混ざっていた。** 守りの境目のために分けているもの（`private/` はエージェントから見えない、`examples/internal_api/` は見本の認証局の秘密鍵をアプリに渡さない、`workspace/`・`memories/`・`skills/` だけがエージェントから見える）と、分かりにくさの元になっているものがあった。
  - **設定の置き場所が 6 か所あり、まとめた表がなかった**（`.env`、`config/security.yaml`、`config/*_columns.*.txt`、`packs/*/pack.yaml`、`.chainlit/config.toml`、`private/`）。
  - **同じ処理が 2 か所にあった。** 最後の返事の取り出し（定時実行と点検）、モデルごとのエージェントの作り置き（画面と定時実行）、空白を詰める処理（見張る言葉と、検索の前の確認）、文字列をそろえる処理（返事の照合と、検索の前の確認）、2 つの点検の入口（進み具合の表示、結果の記録、モデルごとの測定）。6 行以上がそのまま同じ部分は、点検の入口の 2 ファイルだけだった。
  - **`ui/app.py` が 749 行・関数 40 個だった。** 起動、ログイン、設定欄、会話の流れ、返事の表示、確認のダイアログが 1 ファイルにあった。
  - **点検の道具（約 2,700 行。コードの約 4 分の 1）が、アプリのコード（`src/data_agent/evals/`）に混ざっていた。** 測るときにだけ使い、ふだんの動きには使わない。アプリのイメージにも入っていた。
  - **前の名前のころの名残があった。** `skills/python-testing/` は、coding_agent のころの「pytest の書き方」の手順書で、データ分析では使わない。

## 決定

1. **README に「フォルダの地図」と「設定の置き場所」の表を置く。** フォルダの表は README に一本化し、`docs/architecture.md` の「6. ファイルの地図」は、`src/` の中の地図と、資料・フォルダに入らないファイルの表だけにする（同じ表を 2 か所に持たない）。守りのために分けているフォルダは、その理由も README に書く。ADR-0037 の決定 4（フォルダの表は説明書に一本化）を変える。
2. **同じ処理を 1 か所にまとめる。** モデルに見せるもの、守りの決まり、画面の動きは変えない。
   - 最後の返事の取り出し: `safety/grounding.py` の `final_text`（定時実行と、2 つの点検が使う）
   - モデルごとのエージェントの作り置き: `functools.cache`（画面と定時実行。辞書を手で持たない）
   - 空白を詰める処理: `safety/search_screen.py` の `compact`（見張る言葉の一覧も使う）
   - 文字列をそろえる処理: `search_screen.normalize` は、`grounding.normalize` に、大文字・小文字と空白の違いをそろえる処理を足したものにする
   - 2 つの点検の入口: 実行、進み具合の表示、結果の表示と保存の骨組みを、`evals/common.py` の `run_model`・`report`・`trace_record`・`yes` にまとめる
   - ログインの失敗の回数の制限（ADR-0038）: 記録用のクラスと「もう知らせたか」の印をやめ、接続元ごとに「回数と最後に試した時刻」だけを持つ。429 と、接続元の検査の 403 の返事は、Starlette の部品（`JSONResponse`・`PlainTextResponse`）で返す。監査ログの `login_throttled` は、失敗して受け付けなくなった時点で 1 回書く（前は、最初の 429 のとき。5 回目に正しいパスワードで入れたときは書かない）。
3. **`ui/app.py` を 3 つに分ける。** 動きは変えない。
   - `ui/app.py`: 起動と終了、ログイン、チャットの始まりと再開、設定欄、依頼の受け付け、「実行 → 確認 → 再開」の繰り返し（749 行 → 447 行）
   - `ui/replies.py`: 返事とツールの表示、作ったファイルの表示、返事の照合の注記（205 行）
   - `ui/approval_dialog.py`: 操作の前の確認のダイアログ（107 行）
   - 保存した入力をマスクしたものに置き換える処理は、履歴の部品がある `ui/chat_commands.py` の `scrub_saved_input` に移す。
   - `ui/replies.py` は、Chainlit が直接実行するファイルではないので、`@dataclass` を使ってよい（`Turn`）。`ui/app.py` では、これまでどおり使わない。
4. **点検の道具を、アプリの外に出す。** `src/data_agent/evals/` を、リポジトリの直下の `evals/` に移す。
   - コマンドは `uv run python -m evals.redteam`・`uv run python -m evals.factcheck`（リポジトリの直下で実行する）。
   - アプリのイメージには入れない（`Dockerfile` と `.dockerignore` は変えない）。攻撃を試すための指示の一覧が、本番のイメージに入らなくなる。
   - 単体テストから読み込めるように、`pyproject.toml` に `[tool.pytest.ini_options] pythonpath = ["."]` を足す。
5. **見本のスキルを入れ替える。** `skills/python-testing/` を消し、データ分析の見本 `skills/data-summary/SKILL.md`（データを集計して、表とグラフにまとめる手順）を置く。スキルの一覧はモデルへの指示に入るので、モデルに見せるものが、この分だけ変わる。
6. **作業の決まりに足す。** 書く前に、同じことをする関数がほかにないか探して、使い回す。使っている部品に既にあるもの（Starlette の応答など）は、手で書かない（AGENTS.md の「作業の決まり」）。

## 検討した案

| 案 | 内容 | 判断 |
|---|---|---|
| **A. 上の 1〜6** | 地図の表、重複のまとめ、画面のファイルの分割、点検の道具の移動、見本のスキルの入れ替え | **採用**。オーナーが選んだ（レビューでおすすめした 1〜4 を、順番に） |
| B. フォルダを大きく組み替える | `private/`・`examples/`・`memories/`・`skills/` を 1 つにまとめる、など | 不採用。フォルダごとに守りの境目（エージェントから見えるか、アプリのイメージに入るか、読み取り専用か）が違う。まとめると、`compose.yaml` と `Dockerfile` の渡し方を作り直すことになり、守りを崩しやすい。理由を README に書くことにした |
| C. 機能ごとにフォルダを作る | 定時実行なら、予定・本体・画面のコマンドを 1 つのフォルダに | 不採用。画面の部品（Chainlit を使う `ui/`）を分けているのは、手元で単体テストをするため。地図で追えるようにした |
| D. `evals/` を `tests/` の下に置く | テストと一緒に置く | 不採用。点検は、本物のモデルで測る道具（料金がかかる）で、料金のかからない確かめ（`tests/`）とは扱いが違う |

## 確かめたこと（2026-11-19）

- **単体テスト:** 603 件が通った（`uv run --offline pytest -q`。この ADR の分、ADR の書き方の確かめが 1 件増えた）。ログインの制限のテストは 16 件から 14 件に整理した（重なる 3 件を消し、5 回目に成功したときは監査ログに書かないことを確かめる 1 件を足した）。`uv run ruff check .`・`uv run ruff format --check .` も通った。
- **点検の自己点検（偽のモデル。料金なし）:** Docker の中で、`evals/` を重ねて `python -m evals.redteam --selftest`（21 種類）と `python -m evals.factcheck --selftest`（12 問）を流し、どちらも「問題ありません」だった。
- **画面の回帰確認:** `bash tests/e2e/regress.sh` で、全 19 種類・398 項目が通った（`logs/e2e/20261119-080923`）。本番と同じ読み方での起動（`boot`）、返事の表示・確認のダイアログ・照合の注記（`approvals`・`grounding` など）、ログインの制限（`login`）を含む。
- **本物のモデル:** まだ。見本のスキルを入れ替えたので、モデルに見せるスキルの一覧が変わった。測り直すかは、オーナーが決める（料金がかかる）。

## 分かっている制約

- **オーナーのイメージ（`data-agent:local`）は、作り直すまで前のまま。** 前の `src/data_agent/evals/` と `skills/python-testing/` が入っている。作り直す（`docker compose up --build`）と、点検の道具はイメージから消え、スキルの見本も入れ替わる。
- **前の ADR の本文にある `python -m data_agent.evals…` は、当時の記録として残した。** いまのコマンドは `python -m evals…`。
- **`README.md` は、ADR-0037 で 49 行にしたものが、表を足して 89 行になった。**

## 次にやること

- [ ] イメージを作り直す（オーナー。`docker compose up --build`）
- [ ] 必要なら、本物のモデルで測り直す（オーナー。料金がかかる）

## 関連

- 変えたファイル: `README.md`、`docs/architecture.md`、`docs/architecture-map.html`、`AGENTS.md`、`SECURITY.md`、`docs/development.md`、`docs/safety.md`、`docs/tutorials/06-verify.md`、`pyproject.toml`、`src/data_agent/ui/app.py`・`replies.py`（新しい）・`approval_dialog.py`（新しい）・`chat_commands.py`・`login_guard.py`・`connection_guard.py`、`src/data_agent/safety/grounding.py`・`search_screen.py`・`watch_terms.py`、`src/data_agent/features/schedule_runner.py`、`evals/`（`src/data_agent/evals/` から移した）、`skills/data-summary/SKILL.md`（新しい。`skills/python-testing/` は消した）、`tests/test_login_guard.py`・`test_redteam.py`・`test_factcheck.py`・`test_approvals.py`・`test_skill_mining.py`、`tests/e2e/check_login.py`
- 関係する ADR: ADR-0028（役割ごとのフォルダ）、ADR-0034（点検・整形・リファクタリング）、ADR-0037（README を短くする）、ADR-0038（ログインの失敗の回数の制限）
