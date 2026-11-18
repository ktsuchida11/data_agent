"""会話の保存先（チェックポインタ）の用意。

- DB_HOST が設定されていれば PostgreSQL に保存する。再起動しても会話が残る。
- 未設定ならメモリ上に保存する（再起動で消える）。

PostgreSQL につながらないときは、メモリ上の保存に切り替えずに起動を止める。
「保存されているつもりで消えていた」を防ぐため。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

    from data_agent.settings import Settings

_CONNECT_TIMEOUT_SEC = 15


def connection_kwargs(settings: Settings) -> dict[str, Any] | None:
    """psycopg に渡す接続情報を返す。DB_HOST が未設定なら None（メモリ上に保存する）。"""
    if not settings.db_host:
        return None
    if settings.db_password is None:
        raise RuntimeError("DB_HOST を設定した場合は DB_PASSWORD も設定してください。")
    return {
        "host": settings.db_host,
        "port": settings.db_port,
        "dbname": settings.db_name,
        "user": settings.db_user,
        "password": settings.db_password.get_secret_value(),
        "sslmode": settings.db_sslmode,
        "application_name": "data-agent",
    }


async def open_checkpointer(
    settings: Settings | None = None,
) -> tuple[BaseCheckpointSaver, AsyncConnectionPool | None]:
    """チェックポインタと、終了時に閉じる接続プール（PostgreSQL のときだけ）を返す。"""
    if settings is None:
        from data_agent.settings import get_settings

        settings = get_settings()

    conn = connection_kwargs(settings)
    if conn is None:
        return InMemorySaver(), None

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool, PoolTimeout

    pool = AsyncConnectionPool(
        # autocommit・prepare_threshold・row_factory は AsyncPostgresSaver が前提とする設定
        kwargs={**conn, "autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        min_size=1,
        max_size=settings.db_pool_max,
        open=False,
    )
    try:
        await pool.open(wait=True, timeout=_CONNECT_TIMEOUT_SEC)
        saver = AsyncPostgresSaver(pool)
        await saver.setup()  # テーブルがなければ作る。何度呼んでもよい
    except PoolTimeout as exc:
        await pool.close()
        raise RuntimeError(
            f"PostgreSQL（{conn['host']}:{conn['port']} の {conn['dbname']}）に "
            f"{_CONNECT_TIMEOUT_SEC} 秒以内に接続できませんでした。原因は直前の psycopg.pool の警告に出ています。"
        ) from exc
    except BaseException:
        await pool.close()
        raise
    return saver, pool
