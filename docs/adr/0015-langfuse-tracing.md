# ADR-0015: モデルとのやりとりを Langfuse に送る。送る内容はガードレールを通す

- **状態:** 採用（実装済み）。オーナーが Langfuse にプロジェクト（`t_claw`）と鍵を作った（2026-11-13）。記録が届いていることを確かめた（2026-11-16。下の「次にやること」）。残す期間は、既定 90 日で、設定で変えられるようにした（2026-11-16。ADR-0031）。→ 2026-11-18: 送る直前のマスクの穴を直した。日本語のすぐ後に書いた鍵や電話番号を見逃していた（Langfuse が日本語を \uXXXX に書き換えるため）。ツールの結果（ToolMessage）の中身もマスクしていなかった（ADR-0034）
- **日付:** 2026-11-13
- **決定者:** プロジェクトオーナー
- **対象:** data_agent

## 状況

- モデルに何を渡し、何が返り、どのツールを呼んだかを、あとから画面で追いたい、という要望があります。
- 手元では Langfuse がコンテナで動いています（サーバーは 3.172.1）。brew_app と studyops も、この Langfuse を使っています。
- 監査ログ（ADR-0012）は「誰が・いつ・何をしたか」だけを残し、会話の中身は記録しません。中身を追うには、別の仕組みが必要です。

## 決定

- **モデルとのやりとりとツールの呼び出しを、Langfuse に送ります。** LangChain のコールバックを使います。
- **`.env` に Langfuse の鍵が2つとも設定されているときだけ送ります。** 設定がなければ、何も送りません。
- **送る内容は、ガードレールでマスクしてから送ります**（ADR-0012 と同じ検出ルール）。
- **利用者、チャットの ID、モデル名を付けて送ります。** Langfuse の画面で、チャットごと、利用者ごとにまとめて見られます。

## 仕組み

| 項目 | 内容 |
|---|---|
| **SDK** | Langfuse の Python SDK の v4 系（4.18.0）。公式の対応表では、サーバーが 3.x の場合に使えるのは v4 までで、v5 は使えない |
| **設定** | `.env` の `LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY`、`LANGFUSE_BASE_URL` |
| **コンテナからの送信先** | `http://langfuse-web:3000`（`compose.yaml` が設定する。Langfuse と同じネットワーク内での名前） |
| **コード** | `src/data_agent/storage/tracing.py` |

### マスクは送信の手前で2回行う

ツールの実行の記録には、会話に入る前の結果（マスクする前のファイルの中身など）が含まれます。そのため、会話の中でのマスク（ADR-0012）とは別に、送信の手前でもマスクします。

| 場所 | 役割 |
|---|---|
| SDK が内容を受け取ったとき（`mask`） | 入力や出力として渡された内容をマスクする |
| 送信の直前（`mask_otel_spans`） | 送る記録のすべての項目を見て、残っていればマスクする。ここで失敗すると、Langfuse の SDK はそのまとまりを送らずに捨てる |

## 確かめたこと（2026-11-13）

本物の Langfuse には送らず、送られてきた内容を記録するだけの受け皿を立てて確かめました。偽のモデルを使っています。

**確認できたこと**
- 鍵を設定すると、受け皿に記録が届く（11回、合計約1.8MB。認証の情報付き）。
- 届いた記録のどこにも、利用者が貼り付けたキー、ファイルの中のキー、モデルが書こうとしたキー、カード番号は含まれない。
- 届いた記録に、マスクした跡が含まれる（995か所）。
- 届いた記録に、ツール名、モデル名、利用者、タグが含まれる。
- 鍵を設定しなければ、送信の仕組みは作られない（単体テスト）。

**確認していないこと**
- 本物の Langfuse の画面での見え方。プロジェクトと鍵がまだないためです。
- 本物のモデルでの内容。
- Langfuse が止まっているときの動き。SDK は裏で送る作りですが、試していません。

## 残っている点

- **メールアドレスと電話番号は、そのまま送られます。** ガードレールの既定では「記録だけ」の種類だからです（受け皿にも届いていました）。送りたくない場合は、`config/security.yaml` で `mask` にします。会話の中でもマスクされるようになります。
- **形が決まっていない秘密情報は、マスクされずに送られます**（ADR-0012 の限界と同じ）。
- **Langfuse に送った内容を、いつまで残すかは決めていません。** → 2026-11-16 に決めた。既定は 90 日で、`config/security.yaml` の `tracing.retention_days` で変えられる。アプリが毎日、古い記録を消す（ADR-0031）。
- **Langfuse は、brew_app と studyops も使っています。** プロジェクトを分けることで、記録は混ざりません。

## オーナーにお願いすること

Langfuse のプロジェクトと鍵は、Langfuse の画面にログインして作る必要があります。ログインの情報がないので、こちらでは作っていません。

1. Langfuse の画面（`http://localhost:3000`）で、新しいプロジェクトを作る（例: `data-agent`）。
2. そのプロジェクトの Settings → API Keys で、鍵を作る。
3. `.env` に次の3行を足す。
   ```
   LANGFUSE_PUBLIC_KEY=pk-lf-...
   LANGFUSE_SECRET_KEY=sk-lf-...
   LANGFUSE_BASE_URL=http://localhost:3000
   ```
4. アプリを起動し直す。

## 次にやること

- [x] オーナーがプロジェクトと鍵を作り、Langfuse に記録が届くことを確かめる（鍵は 2026-11-13 に作成。2026-11-16 に、Langfuse のデータベースを読むだけで確かめた: プロジェクト `t_claw` に、タグ `coding-agent` の記録が 6 件。日本時間 11/13 21:56〜11/15 16:11。鍵の値は見ていない）
- [x] Langfuse に送った内容を残す期間を決める（2026-11-16。オーナー。既定 90 日、設定で変えられる。ADR-0031）

## 関連

- [src/data_agent/storage/tracing.py](../../src/data_agent/storage/tracing.py)
- [src/data_agent/ui/app.py](../../src/data_agent/ui/app.py) の `_config`
- ADR-0012（ガードレールと監査ログ）
- [Langfuse の対応表（SDK とサーバーの版）](https://github.com/langfuse/langfuse-docs/blob/main/content/self-hosting/upgrade/versioning.mdx)
- [Langfuse: マスクの仕組み](https://github.com/langfuse/langfuse-docs/blob/main/content/docs/observability/features/masking.mdx)
- [Langfuse: LangChain との連携](https://github.com/langfuse/langfuse-docs/blob/main/content/integrations/frameworks/langchain.mdx)
