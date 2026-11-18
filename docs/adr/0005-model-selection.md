# ADR-0005: モデルは `.env` で指定し、起動中は固定する

- **状態:** 一部置き換え済み（→ ADR-0014）。画面の設定欄から、チャットごとに切り替えられるようになった
- **日付:** 2026-11-13（設計はフェーズ1の時点のもの）
- **決定者:** プロジェクトオーナー

## 状況

「モデルを切り替える機能はあるのか」という質問が出たので、現行の指定方法を記録します。

## 決定（現行の設計）

- **モデルは `.env` で指定します。** 変更するときは `.env` を書き換えてアプリを再起動します。

| 設定 | 役割 | 省略時の値 |
|---|---|---|
| `MAIN_MODEL` | メインのエージェント | `claude-opus-5-5` |
| `FAST_MODEL` | 調査用サブエージェント（`explorer`） | `claude-haiku-5-5` |
| `MAIN_EFFORT` | メインのエージェントの推論の深さ | `high` |

- `ENABLE_REFUSAL_FALLBACK` を有効にすると、安全分類器に断られたときにサーバー側で別のモデルに切り替えて続けます。対象は Opus 5.5 と Sonnet 5.5 です。

## 影響

- セッションの途中でモデルを変えることはできません。
- 費用を抑えたいときは、`MAIN_MODEL=claude-sonnet-5-5` のように `.env` で変更します。

## 追記（2026-11-13）

画面の設定欄から、チャットごとにモデルを切り替えられるようにしました（ADR-0014）。
`.env` の `MAIN_MODEL` は、新しいチャットが始まるときのモデルになります。選べるモデルは `MODEL_CHOICES` で決めます。

## 未決事項

- 画面からモデルを切り替えられるようにするか。ADR-0014 で対応済みです。以下は、検討していたときの記録です。Chainlit のチャットプロファイルまたは設定画面と、モデル呼び出しごとにモデルを差し替えるミドルウェア（`request.override(model=...)`）を組み合わせれば実現できます。必要な API があることは確認済みで、実装はしていません。
- 参考: dcode には `/model` コマンドがあります。Managed Deep Agents では `define_deep_agent(model=...)` で指定します。

## 関連

- [example.env](../../example.env)
- [src/data_agent/settings.py](../../src/data_agent/settings.py) の `Settings`
- [src/data_agent/agent.py](../../src/data_agent/agent.py) の `build_model`
