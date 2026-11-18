# ADR-0001: 実行基盤として Managed Deep Agents を試す

- **状態:** 置き換え済み（→ ADR-0009）。試行は行わずに取りやめた
- **日付:** 2026-11-13
- **決定者:** プロジェクトオーナー

## 状況

data_agent は、Deep Agents ライブラリ（`create_deep_agent`）と Chainlit で自作し、手元の Mac で動かしています（フェーズ1まで完了）。
これに次の機能を足したい、という要望が出ました。

- 指定した日時にスキルを動かす（cron のような機能）
- 何度も行っている操作を自動でスキルにする
- モデルの切り替えや会話の保存など、Claude Code にあるような機能

その土台として、LangChain が提供する3つの選択肢を比べました。3つとも中身は同じ Deep Agents です。

## 決定

**Managed Deep Agents（以下 MDA）を試します。**

- 現行のローカル版（このフォルダ）は、試行の結果が出るまでそのまま残します。この点はオーナーに未確認の前提です。
- 全面的に移行するかどうかは、下の「試行で確かめること」の結果を見て改めて決め、新しい ADR に記録します。

## 検討した選択肢

| | A. Deep Agents ライブラリで自作（現行） | B. Deep Agents Code（dcode） | C. Managed Deep Agents |
|---|---|---|---|
| **何か** | エージェントを自作するための部品 | 完成品のターミナル用コーディングエージェント | エージェントをファイル構成で定義し、LangSmith Cloud にデプロイする仕組み |
| **動く場所** | 手元の Mac | 手元の Mac | LangSmith Cloud（米国リージョン） |
| **画面** | 自由に作れる（今は Chainlit） | ターミナル | LangSmith Studio、Slack など。独自の画面は要確認 |
| **定時実行** | 自分で作る | 専用の機能は見つかっていない | `schedules/` に書くと cron として動く |
| **コード実行** | 自分で作る（フェーズ2） | あり | サンドボックスあり（既定ではスレッドごとに1つ） |
| **会話の保存** | 自分で作る | あり（`/threads`） | あり（スレッドが保存される） |
| **安全対策** | すべて自分で設計できる | 承認モード3種から選ぶ | ミドルウェアは書ける。現行の権限・承認を再現できるかは要確認 |
| **データの置き場所** | 手元（LLM への送信を除く） | 手元（LLM への送信を除く） | コード・会話・ファイルがクラウドに置かれる |

MDA のファイル構成は `agent.py`、`instructions.md`、`skills/`、`tools/`、`middleware/`、`sandbox/`、`memory.py`、`channels/`、`schedules/`、`evals/` です。
`mda init` で雛形を作り、`mda dev` で手元で確認し、`mda deploy` でデプロイします。

## トレードオフ

**C（MDA）で得られるもの**
- 定時実行、コード実行、会話の保存を自作しなくて済みます。現行の計画ではフェーズ2以降で作る予定だった部分です。
- 指示文・スキル・メモリは Context Hub に置かれ、再デプロイなしで編集できます。

**C（MDA）で失うもの、リスク**
- **データが手元の外に出ます。** 「手元で安全に動かす」というこれまでの方針と食い違うので、クラウドに置いてよいコードと情報の範囲を決める必要があります。
- **ベータ版です。** 2026-11 時点でパブリックベータ、LangSmith Cloud の米国リージョンのみです。仕様が変わる可能性があります。
- **Chainlit の画面とログインはそのまま持ち込めません。** 公式ドキュメントは、独自のルート・認証・保存先の制御が必要な場合は LangSmith Deployment を直接使うよう案内しています。
- **現行の安全対策が同じ形で再現できるかは未確認です。** 対象は、秘密ファイルの読み書き禁止、`/skills/` への書き込み禁止、削除とメモリ書き込みの承認です。
- **費用がかかります。** LangSmith の利用料に加えて、サンドボックスは従量課金です。

**B（dcode）を選ばなかった理由**
- ターミナル専用で、ブラウザの画面と独自の安全対策という要件に合いません。
- 日常のコーディング用や、モデル切替・セッション一覧などを実装するときの手本としては使えます。

**議論の経過**
- 当初の推奨は A の継続でした。理由は、ブラウザの画面、独自の安全対策、独自機能の3点です。
- そのうえで、オーナーが C を試すと決めました。

## 影響

- **楽になること:** 定時実行（ADR-0006）、コード実行（ADR-0002 のフェーズ2）、会話の保存（ADR-0003）。
- **難しくなること:** 画面とログイン、手元だけで完結する運用、権限ルールの細かい制御。
- **見直すこと:** フェーズ2〜5 の計画。MDA が担う部分は自作しなくてよくなります。

## 試行で確かめること

- [ ] LangSmith のアカウント（米国リージョン）と API キーを用意する
- [ ] `mda init` で雛形を作り、`mda dev` で手元で動かす
- [ ] 現行の指示文（`prompts/SYSTEM.md`）、スキル、メモリを移して、同じ依頼に答えられるか
- [ ] `schedules/` で、指定した時刻にスキルが動くか（ADR-0006）
- [ ] 承認が必要な操作（削除、メモリへの書き込み）を止められるか
- [ ] 秘密ファイルの読み書き禁止など、権限ルールを再現できるか
- [ ] 画面をどうするか（LangSmith Studio、Slack、Chainlit から呼び出す、のどれか）
- [ ] 1か月あたりの費用の見込み

## 追記（2026-11-13）

最終的な実行環境は、自前で構成する AWS の ECS に決まりました（ADR-0009）。
LangSmith の Enterprise プランは契約しないので、MDA を自社の AWS で使う方法（BYOC）は採れません。

MDA の定時実行、Context Hub、サンドボックスは、自前の ECS 構成にはそのまま移せません。
試行は最終的な構成につながらないので、行わないとオーナーが決めました。「試行で確かめること」のチェックリストは実施しません。
以後は、ローカル版をコンテナにして AWS に載せる作業を進めます（ADR-0009）。

## 参考

- [Managed Deep Agents is now in public beta（LangChain Blog）](https://www.langchain.com/blog/managed-deep-agents-is-now-in-public-beta)
- [Managed Deep Agents overview（LangChain Docs）](https://docs.langchain.com/langsmith/managed-deep-agents-overview)
- [Managed Deep Agents runtime（LangChain Docs）](https://docs.langchain.com/langsmith/python/managed-deep-agents-runtime)
- [Managed Deep Agents schedules（LangChain Docs）](https://docs.langchain.com/langsmith/managed-deep-agents-schedules)
- [LangSmith Plans and Pricing](https://www.langchain.com/pricing)
- 手元の調査資料: `../../../01_DeepAgents利用法とClaudeCode風エージェントの作り方.md` の 3章（dcode）と 6.1節（MDA）
