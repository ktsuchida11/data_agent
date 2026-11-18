"""会話の保存先の選び方（persistence）が意図どおりか。

PostgreSQL には接続しない。接続情報の組み立てと、DB_HOST がないときの動きだけを確かめる。
settings.py は .env を読むので import せず、同じ項目を持つ入れ物を渡す。
"""

import asyncio
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import SecretStr

from data_agent.storage.persistence import connection_kwargs, open_checkpointer


def _settings(**overrides) -> SimpleNamespace:
    base = {
        "db_host": None,
        "db_port": 5432,
        "db_name": "coding_agent",
        "db_user": "coding_agent",
        "db_password": None,
        "db_sslmode": "prefer",
        "db_pool_max": 5,
    }
    return SimpleNamespace(**(base | overrides))


def test_no_db_host_uses_memory() -> None:
    assert connection_kwargs(_settings()) is None
    saver, pool = asyncio.run(open_checkpointer(_settings()))
    assert isinstance(saver, InMemorySaver)
    assert pool is None


def test_db_host_without_password_is_an_error() -> None:
    with pytest.raises(RuntimeError, match="DB_PASSWORD"):
        connection_kwargs(_settings(db_host="127.0.0.1"))


def test_connection_kwargs_are_built_from_settings() -> None:
    kwargs = connection_kwargs(
        _settings(db_host="db.example", db_port=6543, db_password=SecretStr("s3cret"), db_sslmode="require")
    )
    assert kwargs == {
        "host": "db.example",
        "port": 6543,
        "dbname": "coding_agent",
        "user": "coding_agent",
        "password": "s3cret",
        "sslmode": "require",
        "application_name": "data-agent",
    }
