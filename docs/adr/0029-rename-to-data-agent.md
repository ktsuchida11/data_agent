# ADR-0029: 名前を coding_agent から data_agent（データ分析エージェント）に変え、役割の指示をデータの分析に合わせる

- **状態:** 採用（実装済み）。オーナーが決めた（2026-11-16。使い方は「データの分析・まとめ」、変える範囲は「フォルダの名前まで全部」）。フォルダの名前も変えた（2026-11-16）。作り直しと、本物のモデルでの測り直しは、オーナーの作業
- **日付:** 2026-11-16
- **決定者:** プロジェクトオーナー
- **対象:** data_agent（旧 coding_agent）

## 状況

- オーナーの質問（2026-11-16）: 「coding_agent って名前だけど、Deep Agents Code は使っていなくて、Deep Agents で顧客のニーズを分析しているだけだよね？名前を変えた方が良いんじゃない？」
- 確かめたこと:
  - **Deep Agents Code（dcode）は使っていない。** 使っているのは Deep Agents のライブラリ（`create_deep_agent`）で、それを組み合わせて自分で作っている。dcode はターミナル専用なので、最初に見送った（ADR-0001）。
  - **顧客のニーズを分析する仕組みではない。** それは `archive/customer_needs_agent/`（動かしていない）のほう。このアプリの道具（ファイル、コードの実行、データベース、Web 検索）は、どれも汎用。
  - **ただし、Claude への指示（`prompts/SYSTEM.md`）は「ソフトウェアを作るコーディングエージェント」だった。** オーナーの実際の使い方（社内のデータを調べて、分析し、まとめる）と合っていなかった。この一文は、返事の仕方そのものに効く。
- オーナーの答え: 主な使い方は「データの分析・まとめ」。名前は「フォルダの名前まで全部」変える。

## 決定

1. **名前を変える。**

   | もの | これまで | これから |
   |---|---|---|
   | フォルダ | `LangChain/coding_agent/` | `LangChain/data_agent/` |
   | Python のパッケージ | `src/coding_agent/`（`import coding_agent`） | `src/data_agent/`（`import data_agent`） |
   | プロジェクトの名前（`pyproject.toml`） | `coding-agent` | `data-agent` |
   | 画面に出る名前 | Coding Agent | データ分析エージェント |
   | イメージ | `coding-agent:local`、`coding-agent-sandbox:local` | `data-agent:local`、`data-agent-sandbox:local` |
   | コンテナ | `coding_agent-coding-agent-1`、`coding_agent-sandbox-1` | `data_agent-data-agent-1`、`data_agent-sandbox-1` |
   | 点検のコマンド | `python -m coding_agent.evals.redteam` など | `python -m data_agent.evals.redteam` など |
   | Langfuse の記録の印 | `coding-agent` | `data-agent` |
   | 定時実行の返事の書き手 | Coding Agent | データ分析エージェント |
   | 依存の点検の登録名（launchd） | `local.coding-agent.audit-deps` | `local.data-agent.audit-deps`（まだ登録されていないので、影響なし） |

2. **Claude への指示の「役割」と「進め方」を、データの分析に合わせて書き直す**（`prompts/SYSTEM.md`）。
   - 役割: 「社内のデータを調べて分析し、まとめるデータ分析エージェント」。ファイル（CSV や Excel）と、読み取り専用のデータベースを扱い、集計や表・グラフ作りのためにコードも実行する。
   - 進め方: データの確かめ方（`list_database_tables` など）、集計はデータベースかコードで行う、出した数をもとのデータと照らす、結論から根拠を添えて報告する。プログラムを作る・直す依頼の進め方も、1 行残す。
   - 「守ること」と「数と出典」は、一文字も変えない。
3. **データベースとロールの名前は残す。** `coding_agent`（会話・予定・依頼の記録）、`coding_agent_sample`、`coding_agent_reader`、`coding_agent_screener` と、それに合わせた設定ファイルの名前（`config/hidden_columns.coding_agent_sample.txt` など）。変えると、保存した会話が読めなくなり、`.env` も書き直しが要るため。
4. **過去の ADR の中の名前は、置き換える。** ただし、過去の事実を書いた行（そのときのイメージの名前、そのときの記録の印）は、そのときの名前のまま残す。

## 検討した案

| 案 | 内容 | 判断 |
|---|---|---|
| 変えない | 説明書に「何に使えるか」を書き足すだけ | 不採用。指示の一文が、使い方と合わないまま残る |
| 画面の名前と指示だけ | フォルダやプログラムの名前はそのまま | 不採用（勧めたのはこれ）。オーナーは、フォルダの名前まで揃えることを選んだ |
| **フォルダの名前まで全部** | 約 630 か所。データベースの名前は残す | **採用**（オーナー） |

## 確かめたこと（2026-11-16）

- **単体テスト:** 463 件すべて通った。
- **点検の自己点検（偽のモデル。料金なし）:** 乗っ取りの点検も作り話の点検も、名前を変える前の結果と、出力が 1 文字も変わらなかった（指示の文を変えても、偽のモデルの動きは変わらない）。
- **画面の回帰確認:** 下の「結果」。

### 結果

- **画面の回帰確認:** 17 種類、356 項目がすべて通った（`logs/e2e/20261116-105410`。イメージは、名前を変える前のもの（`coding-agent:local`）に、いまの `src` を重ねて流した）。
- **フォルダの名前:** オーナーがアプリを止めたあとで、`LangChain/coding_agent/` を `LangChain/data_agent/` に変えた（2026-11-16）。
  - 手元の Python の環境（`.venv`）の起動用のファイル 29 個が、古い場所を指していたので、書き換えた。
  - 新しい場所で、単体テスト 463 件と、本番と同じ読み方での起動の確認が通った。

## 分かっている制約

- **指示を変えたので、本物のモデルで測り直した**（2026-11-16。どちらも合格。上の「次にやること」）。
- **Langfuse の過去の記録は、`coding-agent` の印のまま残る。** 新しい記録は `data-agent`。
- **古いイメージ（`coding-agent:local` など）は、残っている。** 作り直したあと、要らなければ消せる。

## 次にやること

- [x] オーナーが、動いているアプリを止める。そのあとで、フォルダの名前を `data_agent` に変える（2026-11-16）
- [x] オーナーが、新しいフォルダで作り直す（`docker compose up --build`）。新しい名前のイメージとコンテナができる（2026-11-16。`data_agent-data-agent-1` と `data_agent-sandbox-1` が、`data-agent:local` と `data-agent-sandbox:local` で動いているのを確かめた）
- [x] オーナーが、本物のモデルで測り直す（2026-11-16。乗っ取りの点検は 3 つのモデルとも、従った 0/21・貫通 0/21。作り話の点検は Opus 5.5 で、正答 6/6・「ない」6/6・作り話 0/12。合わせて約 1.33 ドル。ADR-0020、ADR-0025）

## 関連

- [packs/data/SYSTEM.md](../../packs/data/SYSTEM.md)（2026-11-17 に `src/data_agent/prompts/SYSTEM.md` から移した。→ ADR-0032）（役割の指示）
- ADR-0001（dcode を選ばなかった理由）、ADR-0010（会話の保存先のデータベース）、ADR-0028（フォルダ分け）
