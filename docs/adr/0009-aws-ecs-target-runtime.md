# ADR-0009: 最終的な実行環境は AWS の ECS とし、自前で構成する

- **状態:** 採用
- **日付:** 2026-11-13
- **決定者:** プロジェクトオーナー
- **対象:** 共通（data_agent と調査・分析エージェント）

## 状況

- 現行の data_agent は、手元の Mac で1人で使う前提で作っています（ADR-0002）。
- 実行基盤として Managed Deep Agents（以下 MDA）を LangSmith Cloud で試すと決めました（ADR-0001）。
- そのうえで、オーナーから「最終的には AWS の ECS などで動かしたい」という意向が出ました。最初の設計とは前提が変わります。
- 調査・分析エージェント（ADR-0008）は、社内のデータベース、RAG、有料データを扱います。自社の AWS の中で動かせれば、データを外に出さずに済みます。
- オーナーは、LangSmith の Enterprise プランを契約しないと決めました。

## 決定

- **最終的な実行環境は AWS の ECS にします。**
- **構成は選択肢 A（自前で ECS に載せる）にします。** 選択肢 B と C は Enterprise プランが必要なので採りません。
- **エージェントの中身は、実行環境に依存しない形に保ちます。** 対象は、指示文、スキル、ツール、権限ルール、Deep Agents ライブラリで組んだ部分です。

## 検討した選択肢

| | A. 自前で ECS に載せる（採用） | B. LangSmith の Agent Server を ECS で動かす | C. MDA を BYOC で使う |
|---|---|---|---|
| **仕組み** | Chainlit とエージェントをコンテナにして ECS で動かす。周辺は AWS のサービスで組む | `langgraph build` で作ったイメージを ECS で動かす。PostgreSQL と Redis は自分で用意する | 自社の AWS アカウントの中に、LangChain が実行環境とデータ置き場を用意して運用する |
| **契約** | 不要 | Enterprise プランのライセンスキーが必要 | Enterprise プランが必要 |
| **会話の保存** | 自分で作る（PostgreSQL に保存） | 付属 | 付属 |
| **定時実行** | 自分で作る（EventBridge Scheduler から ECS タスクを起動） | 付属（cron ジョブ） | 付属（`schedules/`） |
| **コード実行** | 自分で用意する | 自分で用意する | 付属（サンドボックス） |
| **画面** | Chainlit をそのまま使える | 別に用意する | 要確認 |
| **運用の手間** | 大 | 中 | 小 |
| **確認の状況** | 設計のみ | 公式ドキュメントに単体サーバーの手順がある。ECS 専用の手順は見つかっていない | AWS の BYOC は一般提供。その上の MDA はパブリックベータ（CLI と SDK は v0.10 以降） |

## 最初の設計から変わる点

| 項目 | 今（Mac 上） | AWS では |
|---|---|---|
| **利用者** | 1人。`127.0.0.1` で待ち受け、パスワードでログイン | 複数人を想定する。SSO などのログインと、利用者ごとの分離が必要 |
| **会話の保存** | メモリ上。再起動で消える | PostgreSQL（RDS）に保存する |
| **ファイル** | Mac のフォルダ（`workspace/`、`memories/`、`skills/`） | コンテナのディスクは消えるので、EFS か、データベース上の保存先（`StoreBackend`）に置く |
| **コード実行** | Docker サンドボックスを作る計画（フェーズ2） | Fargate ではコンテナの中から Docker を起動できない。実行用のコンテナを、別のサービスとして常駐させる（ADR-0016） |
| **定時実行** | アプリの起動中だけ動くスケジューラ案 | EventBridge Scheduler から ECS タスクを起動する |
| **秘密情報** | `.env` | Secrets Manager |
| **通信** | 手元だけ | ロードバランサー経由の HTTPS。Chainlit は WebSocket を使う |
| **LLM** | Anthropic API | Anthropic API のまま、または Amazon Bedrock 経由（要確認） |

## 変わらない点

- エージェントの組み立て（`agent.py` の `create_deep_agent`）
- 指示文（`prompts/SYSTEM.md`）とスキル
- 権限ルールと承認の流れ
- Chainlit の画面のコード

## 他の ADR への影響

- **ADR-0001（MDA の試行）:** MDA の定時実行、Context Hub、サンドボックスは、この構成にはそのまま移せません。そのため、試行は行わないと決めました。
- **ADR-0002（Docker サンドボックス）:** フェーズ2の設計は作り直します。
- **ADR-0003（セッション）:** 会話の保存と、利用者ごとの分離が必須になります。
- **ADR-0006（定時実行）:** EventBridge Scheduler から ECS タスクを起動する方式を第一候補にします。
- **ADR-0007（スキルの自動化）:** 操作ログの保存先を、ファイルからデータベースやログのサービスに変えます。

## 未確認の点

- ECS で実際に動かしてはいません。上の内容は、現行のコードと公式ドキュメントからの判断です。
- Chainlit を複数台で動かすときの条件。同じ利用者の通信を同じ台に送り続ける設定が必要になる見込みです。
- Amazon Bedrock 経由で、現行のモデルと機能（サーバー側のフォールバックなど）が使えるか。

## 手元のコンテナで確かめたこと（2026-11-13）

`Dockerfile` と `compose.yaml` を追加し、手元の Docker で動かしました。確認には仮のログイン情報と一時フォルダを使い、本物の `.env` と API キーは使っていません。

**確認できたこと**
- イメージをビルドできる（613MB）。イメージの中に `.env` は入っていない。
- 一般ユーザー（uid 10001）で起動する。書き込めるのは `workspace/`、`memories/`、一時フォルダだけで、`src/` や `config/` には書き込めない。
- `compose.yaml` の設定（ルートの書き込み禁止、権限の削減、`127.0.0.1:8000` だけに公開）が、起動したコンテナに反映されている。
- ログインできる。誤ったパスワードと、ログインなしのチャット接続は拒否される。
- ログイン後にチャット用の接続が張られ、アプリの最初の挨拶が届く。

**確認していないこと**
- モデルを呼び出す実際のやりとり。API キーを使っていないためです。

**分かったこと**
- **ロックファイル:** `uv.lock` は「公開から7日以内のパッケージを使わない」という手元の uv の設定で作られています。その設定がないコンテナの中では `uv sync --locked` が止まるので、Dockerfile では `--frozen` を使っています。
- **`chainlit.md`:** ないと Chainlit が起動時に作ろうとし、書き込み禁止の環境では起動に失敗します。プロジェクトに追加しました。
- **接続元の制限:** `.chainlit/config.toml` の `allow_origins` は、チャット用の接続（WebSocket）には効いていません。別の接続元を名乗ったスクリプトからでも、ログイン済みのクッキーがあれば接続できました。他サイトからの接続を防いでいるのは、クッキーの `SameSite=lax` の設定です。AWS で公開する前に、接続元の検査を足すかどうかを決める必要があります。→ 足すことに決めた（オーナー。2026-11-15。ADR-0027）。チャットの接続も、ふつうの API も、`allow_origins` にない接続元からは断る。
- **クッキー:** 手元（HTTP）ではログインのクッキーに `Secure` 属性が付きません。HTTPS で公開したときに付くかを確認する必要があります。→ Chainlit は `SameSite=none` のときしか `Secure` を付けないので、そのままでは付かなかった（2026-11-15 にコードを読んで確かめた）。HTTPS で届いた通信では付けるようにした（ADR-0027）。

## 次にやること

- [x] ローカル版に Dockerfile を足し、手元でコンテナとして動かす（2026-11-13 に実施）
- [x] チャット用の接続に、接続元の検査を足すかどうかを決める（足すことにした。オーナー。2026-11-15。ADR-0027）
- [ ] HTTPS で公開したときに、ログインのクッキーに `Secure` 属性が付くかを確認する
- [ ] コンテナの終了時に出る "Application shutdown failed" のログをなくす（ADR-0011 の「分かっている制約」）
- [ ] 利用者の人数とログイン方法を決める
- [x] コード実行が必要かどうかを決める（2026-11-14。必要。データの可視化と、Excel・PowerPoint の出力に使う。ADR-0016）
- [ ] 実行用のコンテナを、別のサービスとして動かす構成を決める。アプリと同じタスクに入れると、ネットワークを共有してしまう（ADR-0016）
- [x] 会話の保存先を PostgreSQL に変える（2026-11-13 に実施。ADR-0010）
- [ ] ファイルの置き場所（EFS か `StoreBackend`）を決める

## 参考

- [Self-host standalone servers（LangChain Docs）](https://docs.langchain.com/langsmith/deploy-standalone-server)
- [LangSmith Deployment（LangChain Docs）](https://docs.langchain.com/langsmith/deployment)
- [Bring Your Own Cloud (BYOC)（LangChain Docs）](https://docs.langchain.com/langsmith/byoc)
- [LangSmith BYOC is now available on AWS, Azure, and Google Cloud（LangChain Blog）](https://www.langchain.com/blog/langsmith-byoc-aws-gc-azure)
