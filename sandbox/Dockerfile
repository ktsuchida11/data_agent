# コード実行専用のコンテナ。アプリ本体（../Dockerfile）とは別に動かす。
# 秘密情報も、アプリのコードも入れない。入っているのは、実行用のサーバーとライブラリだけ。

FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /bin/uv

# グラフに日本語を描くためのフォント
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-ipaexfont-gothic \
    && rm -rf /var/lib/apt/lists/*

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SANDBOX_KILL_LEFTOVERS=1

WORKDIR /app

# 実行されるコードが使うライブラリ。版とハッシュを requirements.txt で固定している
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=requirements.txt,target=requirements.txt \
    uv pip install --system --require-hashes -r requirements.txt

COPY server.py matplotlibrc ./

# 一般ユーザーで動かす。書き込めるのは、起動する側が用意する一時フォルダ（/work と /tmp）だけ
RUN useradd --no-create-home --uid 10001 sandbox \
    && mkdir /work && chown sandbox:sandbox /work \
    && rm /bin/uv
USER sandbox

EXPOSE 8080

CMD ["python", "server.py"]
