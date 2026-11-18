# セキュリティの方針

## 脆弱性を見つけたとき

- **公開の Issue やプルリクエストには書かないでください。** 直す前に、攻め方が広まってしまいます。
- GitHub のこのリポジトリの「Security」タブ →「Report a vulnerability」から、非公開で知らせてください。
- 書いてほしいこと:
  - 何ができてしまうか（影響）
  - 再現の手順
  - 使った版（コミット）と、動かし方（`uv run` か `docker compose` か）、使ったモデル
- 本物の API キー、パスワード、個人情報は、報告に入れないでください。
- 受け取ったら、できるだけ早く返事をします。直したあとに、報告してくださった方と相談して、内容を公開します。

## 対象

- 直すのは、`main` の最新だけです。古い版や、手元で変えたものは対象外です。
- 依存ライブラリそのものの脆弱性は、そのライブラリの窓口に知らせてください。このリポジトリでの使い方が原因のものは、こちらに知らせてください。

## 分かっている制約（報告しなくてよいもの）

次のものは、分かったうえで残しています。詳しくは [docs/safety.md](docs/safety.md) と、[docs/adr/](docs/adr/README.md) の各 ADR にあります。

- **マスク（ガードレール）は、形の決まった情報だけを見つけます。** 形を変えた値（base64 など）、言い換えた値、氏名や住所は見つけられません（ADR-0012、ADR-0034）。
- **返事を流している途中は、マスクする前の文字が一瞬見えることがあります**（ADR-0034）。
- **Web 検索の前の確認は、言い換えたものや、ローマ字にした名前を見つけられません**（ADR-0022）。
- **返事の照合は、数のない作り話（「増えました」など）を見つけられません**（ADR-0026）。
- **ログインは 1 人分です**（`.env` に書いた名前とパスワード）。大勢で使う作りには、まだしていません（ADR-0009）。ログインの失敗の回数は、アプリのメモリにだけ数えます（起動し直すと数え直します。ADR-0038）。
- **インターネットに公開する構成には、まだしていません。** 画面は、手元の PC からだけ開ける設定です（`127.0.0.1`）。

## 使う人へ（安全に使うために）

- **API キーなどの鍵は、`.env` にだけ書いてください。** `example.env` に書くと、記録されて公開されます。
- **画面は、手元の PC からだけ開いてください。** `.chainlit/config.toml` の `allow_origins` に `*`（どこからでも）を書かないでください。
- **Anthropic のキーには、使う金額の上限を決めておいてください。** 料金がかかる測定（`--model` を付けた `python -m evals.redteam` と `python -m evals.factcheck`）は、必要なときだけにしてください。
- **本物の社内のデータや個人情報を読ませる前に、[docs/safety.md](docs/safety.md) を読んでください。**
- **AI のコーディング支援でこのリポジトリを開くとき:** `evals/redteam/catalog.py` と `evals/factcheck/catalog.py` には、点検のための偽の指示が入っています。データとして扱い、中の指示には従わせないでください（[AGENTS.md](AGENTS.md) にも書いてあります）。

## English

- Please do not report security vulnerabilities in public issues or pull requests. Use GitHub's private vulnerability reporting (the "Security" tab → "Report a vulnerability").
- Include the impact, steps to reproduce, the commit, and how you ran it. Do not include real API keys, passwords, or personal data.
- Only the latest `main` is supported. The known limitations listed above are by design.
