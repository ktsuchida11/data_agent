# data_agent をコンテナで動かすための定義。
# 秘密情報（.env）はイメージに入れない。起動時に環境変数として渡す。

FROM python:3.12-slim

# uv: ロックファイル（uv.lock）どおりに依存を入れるために使う。手元と同じ版に固定する
COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# 依存だけを先に入れる（コードを変えても、この層は作り直されない）
# --frozen: uv.lock に書かれた版とハッシュのとおりに入れ、ロックファイルの作り直しはしない。
# uv.lock は「公開から7日以内のパッケージを使わない」という手元の uv の設定で作られていて、
# その設定がないコンテナの中で --locked を使うと「更新が必要」と判定されて止まるため。
# pyproject.toml と uv.lock がずれていないかは、手元の `uv sync --locked` で確かめる。
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-dev --no-install-project

# アプリ本体。settings.py が src/ の2つ上をプロジェクトのルートとして扱うので、
# 手元と同じフォルダ構成のまま /app に置く
COPY pyproject.toml uv.lock README.md ./
# chainlit.md: 画面に出す説明文。ないと Chainlit が起動時に作ろうとして、書き込み禁止の環境では止まる
COPY chainlit.md ./
COPY src/ src/
COPY config/ config/
# 用途パック（役割の文・使うツール・MCP サーバー。どれを使うかは .env の AGENT_PACK。ADR-0032）
COPY packs/ packs/
COPY skills/ skills/
COPY memories/ memories/
COPY .chainlit/ .chainlit/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# 一般ユーザーで動かす。書き込めるのは workspace・memories・.files だけにする
# （.files は Chainlit が起動時に作る一時フォルダ）
RUN useradd --create-home --uid 10001 app \
    && mkdir -p workspace .files \
    && chown -R app:app workspace memories .files
USER app

EXPOSE 8000

# コンテナの中では 0.0.0.0 で待ち受ける。外からどこまで開けるかは、起動する側（compose.yaml や ECS）で決める
CMD ["chainlit", "run", "src/data_agent/ui/app.py", "--host", "0.0.0.0", "--port", "8000", "--headless"]
