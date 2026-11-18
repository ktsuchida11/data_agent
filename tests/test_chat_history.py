"""過去のチャットの保存（chat_history）の接続 URL の組み立て。PostgreSQL には接続しない。"""

from types import SimpleNamespace

from pydantic import SecretStr

from data_agent.storage.chat_history import SCHEMA, database_url, display_name, strip_group_prefix


def _settings(**overrides) -> SimpleNamespace:
    base = {
        "db_host": "db.example",
        "db_port": 5432,
        "db_name": "coding_agent",
        "db_user": "coding_agent",
        "db_password": SecretStr("s3cret"),
        "db_sslmode": "prefer",
        "db_pool_max": 5,
    }
    return SimpleNamespace(**(base | overrides))


def test_no_db_host_means_no_url() -> None:
    assert database_url(_settings(db_host=None)) is None


def test_url_is_built_from_settings() -> None:
    assert (
        database_url(_settings(db_port=6543, db_sslmode="require"))
        == "postgresql://coding_agent:s3cret@db.example:6543/coding_agent?sslmode=require"
    )


def test_special_characters_in_password_are_escaped() -> None:
    url = database_url(_settings(db_password=SecretStr("p@ss/w:rd?")))
    assert url == "postgresql://coding_agent:p%40ss%2Fw%3Ard%3F@db.example:5432/coding_agent?sslmode=prefer"


def test_schema_only_creates_if_missing() -> None:
    # 起動のたびに実行するので、すべて「なければ作る」になっていること
    assert all("IF NOT EXISTS" in statement for statement in SCHEMA)


def test_group_is_shown_as_a_prefix_in_the_list() -> None:
    assert display_name("設計の相談", "案件A") == "[案件A] 設計の相談"
    assert display_name("設計の相談", None) == "設計の相談"
    assert display_name(None, "案件A") == "[案件A]"
    assert display_name(None, None) is None


def test_renaming_from_the_list_does_not_double_the_prefix() -> None:
    # 画面で名前を変更すると、一覧に出ていた [グループ名] 付きの名前が送られてくる
    assert strip_group_prefix("[案件A] 新しい名前", "案件A") == "新しい名前"
    # グループが違う、またはグループなしのときは、そのまま
    assert strip_group_prefix("[案件B] 新しい名前", "案件A") == "[案件B] 新しい名前"
    assert strip_group_prefix("[案件A] 新しい名前", None) == "[案件A] 新しい名前"
    # 外した結果をもう一度一覧に出しても、元の見た目に戻る
    assert display_name(strip_group_prefix("[案件A] 新しい名前", "案件A"), "案件A") == "[案件A] 新しい名前"
