# 進捗

作り始めたときに決めたフェーズと、いまの進み具合です。いまの状況と次の一歩は [AGENTS.md](../AGENTS.md) の「いまの状況」にあります。

[README に戻る](../README.md)

- [x] フェーズ1: 骨組み＋チャット（ファイル操作のみ・コード実行なし）
- [x] フェーズ2: 専用のコンテナでのコード実行（Python だけ。承認は設定で付けられる。`docs/adr/0016-code-execution-in-sandbox-container.md`）
- [x] フェーズ3: 入口・出口のガードレール（ルールによる検出）と監査ログ。当初案の Presidio と LLM Guard は入れていない（`docs/adr/0012-guardrails-and-audit-log.md`）
- [ ] フェーズ4: MCP・Web アクセス（許可リスト）・Playwright。このうち、Web 検索（`docs/adr/0018-web-search-via-claude-api.md`）と、データベースの読み取り（`docs/adr/0017-database-read-tool.md`）と、MCP（HTTPS の社内 API。`docs/adr/0033-mcp-internal-api-over-https.md`）は実装済み。用途パック（`docs/adr/0032-use-case-packs-and-tool-registry.md`）で、別の用途にも使えるようにした
- [ ] フェーズ5: 仕上げ（eval・設定の見直し）。このうち、OWASP LLM Top 10 の項目での点検（`docs/adr/0020-owasp-llm-top10-review.md`）と、操作の前の確認（`docs/adr/0021-approvals-human-in-the-loop.md`）は実装済み。本物のモデルでの攻撃への強さと誤情報も、オーナーが測った（ADR-0020、ADR-0025）

最終的な実行環境は AWS の ECS に決まりました（`docs/adr/0009-aws-ecs-target-runtime.md`）。フェーズ2以降の内容は、それに合わせて見直します。
