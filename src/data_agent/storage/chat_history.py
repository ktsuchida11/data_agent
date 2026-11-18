"""過去のチャットの保存（Chainlit のデータ層）。

画面の左側の「過去のチャット」一覧、選んで再開、名前の変更、削除を有効にする。
保存先は、会話の保存（persistence.py）と同じ PostgreSQL のデータベース。

- Chainlit 標準の ChainlitDataLayer を使う。SQL が明示的で、一覧も必要な分だけ読むため。
- テーブル定義は Chainlit の配布物に含まれないので、ここに持つ（SCHEMA）。
  chainlit 2.12.0 の chainlit/data/chainlit_data_layer.py が読み書きする列に合わせてある。
  Chainlit を上げるときは、その SQL と見比べること。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import asyncpg
from chainlit.data.chainlit_data_layer import ChainlitDataLayer
from chainlit.step import StepDict
from chainlit.types import PageInfo, PaginatedResponse, Pagination, ThreadDict, ThreadFilter

from data_agent.storage.persistence import connection_kwargs

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

    from data_agent.settings import Settings

# 1文ずつ実行する。すべて「なければ作る」なので、何度実行してもよい
SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS "User" (
        id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        identifier  TEXT NOT NULL UNIQUE,
        metadata    JSONB NOT NULL DEFAULT '{}',
        "createdAt" TIMESTAMP NOT NULL DEFAULT now(),
        "updatedAt" TIMESTAMP NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS "Thread" (
        id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        "createdAt" TIMESTAMP NOT NULL DEFAULT now(),
        "updatedAt" TIMESTAMP NOT NULL DEFAULT now(),
        "deletedAt" TIMESTAMP,
        name        TEXT,
        metadata    JSONB NOT NULL DEFAULT '{}',
        "userId"    UUID REFERENCES "User" (id) ON DELETE CASCADE,
        tags        TEXT[] NOT NULL DEFAULT '{}'
    )
    """,
    # type は Chainlit 側の種類（user_message、assistant_message、tool、run など）。
    # 新しい種類が増えても保存に失敗しないよう、列挙型ではなく文字列で持つ
    """
    CREATE TABLE IF NOT EXISTS "Step" (
        id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        "createdAt" TIMESTAMP NOT NULL DEFAULT now(),
        "updatedAt" TIMESTAMP NOT NULL DEFAULT now(),
        "parentId"  UUID REFERENCES "Step" (id) ON DELETE CASCADE,
        "threadId"  UUID REFERENCES "Thread" (id) ON DELETE CASCADE,
        input       TEXT,
        metadata    JSONB NOT NULL DEFAULT '{}',
        name        TEXT,
        output      TEXT,
        type        TEXT NOT NULL,
        "showInput" TEXT DEFAULT 'json',
        "isError"   BOOLEAN DEFAULT false,
        "startTime" TIMESTAMP NOT NULL,
        "endTime"   TIMESTAMP NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS "Element" (
        id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        "createdAt"   TIMESTAMP NOT NULL DEFAULT now(),
        "updatedAt"   TIMESTAMP NOT NULL DEFAULT now(),
        "threadId"    UUID REFERENCES "Thread" (id) ON DELETE CASCADE,
        "stepId"      UUID NOT NULL REFERENCES "Step" (id) ON DELETE CASCADE,
        metadata      JSONB NOT NULL DEFAULT '{}',
        mime          TEXT,
        name          TEXT NOT NULL,
        "objectKey"   TEXT,
        url           TEXT,
        "chainlitKey" TEXT,
        display       TEXT,
        size          TEXT,
        language      TEXT,
        page          INTEGER,
        props         JSONB
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS "Feedback" (
        id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        "createdAt" TIMESTAMP NOT NULL DEFAULT now(),
        "updatedAt" TIMESTAMP NOT NULL DEFAULT now(),
        "stepId"    UUID REFERENCES "Step" (id) ON DELETE CASCADE,
        name        TEXT NOT NULL,
        value       DOUBLE PRECISION NOT NULL,
        comment     TEXT
    )
    """,
    # アーカイブとグループ分け（Chainlit にはない、このアプリ独自の列）。
    # Chainlit も読み書きする metadata には入れない。開いたままの画面が持つ古い値で上書きされるのを避けるため
    'ALTER TABLE "Thread" ADD COLUMN IF NOT EXISTS "archivedAt" TIMESTAMP',
    'ALTER TABLE "Thread" ADD COLUMN IF NOT EXISTS "groupName" TEXT',
    # チャットごとに選んだモデル（画面の設定欄）。開き直したときに同じモデルで続けるために持つ
    'ALTER TABLE "Thread" ADD COLUMN IF NOT EXISTS "modelName" TEXT',
    'CREATE INDEX IF NOT EXISTS "Thread_userId_updatedAt_idx" ON "Thread" ("userId", "updatedAt" DESC)',
    'CREATE INDEX IF NOT EXISTS "Step_threadId_startTime_idx" ON "Step" ("threadId", "startTime")',
    'CREATE INDEX IF NOT EXISTS "Step_parentId_idx" ON "Step" ("parentId")',
    'CREATE INDEX IF NOT EXISTS "Element_threadId_idx" ON "Element" ("threadId")',
    'CREATE INDEX IF NOT EXISTS "Element_stepId_idx" ON "Element" ("stepId")',
    'CREATE INDEX IF NOT EXISTS "Feedback_stepId_idx" ON "Feedback" ("stepId")',
)


async def ensure_schema(pool: AsyncConnectionPool, extra: tuple[str, ...] = ()) -> None:
    """過去のチャット用のテーブルがなければ作る。extra は、ほかの機能が使うテーブルの定義。"""
    async with pool.connection() as conn:
        for statement in (*SCHEMA, *extra):
            await conn.execute(statement)


def database_url(settings: Settings) -> str | None:
    """asyncpg に渡す接続 URL を返す。DB_HOST が未設定なら None。"""
    conn = connection_kwargs(settings)
    if conn is None:
        return None
    user, password, dbname = (quote(str(conn[k]), safe="") for k in ("user", "password", "dbname"))
    return f"postgresql://{user}:{password}@{conn['host']}:{conn['port']}/{dbname}?sslmode={conn['sslmode']}"


def display_name(name: str | None, group: str | None) -> str | None:
    """左の一覧に出す名前。グループに入っているチャットは、先頭に [グループ名] を付ける。"""
    if not group:
        return name
    return f"[{group}] {name or ''}".rstrip()


def strip_group_prefix(name: str, group: str | None) -> str:
    """一覧用に付けた [グループ名] を外す。画面で名前を変更したときに、二重に付くのを防ぐ。"""
    prefix = f"[{group}] " if group else ""
    return name[len(prefix) :] if prefix and name.startswith(prefix) else name


class ChatHistoryDataLayer(ChainlitDataLayer):
    """Chainlit 標準のデータ層に、接続数の上限・削除の連動・アーカイブ・グループ分けを足したもの。"""

    def __init__(
        self,
        url: str,
        *,
        pool_max: int,
        on_thread_deleted: Callable[[str], Awaitable[None]] | None = None,
        mask: Callable[[str], str] | None = None,
    ) -> None:
        super().__init__(database_url=url, storage_client=None)
        self._pool_max = pool_max
        self._on_thread_deleted = on_thread_deleted
        self._mask = mask  # 保存する前に、文章をマスクする関数（ガードレール）。None なら、そのまま保存する

    async def connect(self) -> None:
        # 標準では接続を常に10本開く。共有している PostgreSQL の接続数を使いすぎないよう、上限を絞る
        if not self.pool:
            self.pool = await asyncpg.create_pool(self.database_url, min_size=1, max_size=self._pool_max)

    async def create_step(self, step_dict: StepDict) -> None:
        # 利用者のメッセージは、マスクする前の文章のまま届く。保存する前にマスクする（保存のあとで置き換えるのを待たない）
        await super().create_step(self._masked_step(step_dict))

    async def update_step(self, step_dict: StepDict) -> None:
        await super().update_step(self._masked_step(step_dict))

    def _masked_step(self, step_dict: StepDict) -> StepDict:
        if self._mask is None:
            return step_dict
        masked = dict(step_dict)
        for key in ("input", "output"):
            value = masked.get(key)
            if isinstance(value, str) and value:
                masked[key] = self._mask(value)
        return masked  # type: ignore[return-value]

    async def create_element(self, element: Any) -> None:
        """画面に出した画像やファイルは、履歴には保存しない。

        ファイルの置き場所（S3 など）を用意していないので、保存しても、開き直したときに中身を出せない。
        コードが作ったファイルは作業フォルダに残っていて、保存先のパスはメッセージの文章に書いてある。
        """
        return

    async def delete_thread(self, thread_id: str) -> None:
        await super().delete_thread(thread_id)
        # 画面の履歴だけでなく、エージェント側に保存した会話（チェックポイント）も消す
        if self._on_thread_deleted is not None:
            await self._on_thread_deleted(thread_id)

    # ---------- 一覧（アーカイブしたチャットは出さない。グループ名を名前の先頭に付ける） ----------
    async def list_threads(self, pagination: Pagination, filters: ThreadFilter) -> PaginatedResponse[ThreadDict]:
        rows: list[dict[str, Any]] = []
        if filters.userId:  # 持ち主が分からないときは何も返さない
            rows = await self._select_threads(
                user_id=filters.userId,
                archived=False,
                search=filters.search,
                cursor=pagination.cursor,
                limit=pagination.first + 1,
            )
        threads = [self._to_thread_dict(row) for row in rows[: pagination.first]]
        return PaginatedResponse(
            pageInfo=PageInfo(
                hasNextPage=len(rows) > pagination.first,
                startCursor=threads[0]["id"] if threads else None,
                endCursor=threads[-1]["id"] if threads else None,
            ),
            data=threads,
        )

    async def list_archived_threads(self, user_id: str, limit: int = 20) -> list[ThreadDict]:
        rows = await self._select_threads(user_id=user_id, archived=True, limit=limit)
        return [self._to_thread_dict(row) for row in rows]

    async def _select_threads(
        self,
        *,
        user_id: str,
        archived: bool,
        limit: int,
        search: str | None = None,
        cursor: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}

        def bind(key: str, value: Any) -> str:
            params[key] = value
            return f"${len(params)}"

        where = [
            't."deletedAt" IS NULL',
            f't."archivedAt" IS {"NOT NULL" if archived else "NULL"}',
            f't."userId" = {bind("user_id", user_id)}',
        ]
        if search:
            pattern = bind("search", f"%{search}%")
            where.append(f'(t.name ILIKE {pattern} OR t."groupName" ILIKE {pattern})')
        if cursor:
            where.append(f't."updatedAt" < (SELECT "updatedAt" FROM "Thread" WHERE id = {bind("cursor", cursor)})')  # noqa: S608
        # 値は bind() で $1, $2… として渡す。文の中に入れるのは、決まった語句と $n だけ（S608 は誤検知）
        query = f"""
            SELECT t.id, t.name, t."groupName", t."userId", t.metadata, t."updatedAt",
                   u.identifier AS user_identifier
            FROM "Thread" t
            LEFT JOIN "User" u ON t."userId" = u.id
            WHERE {" AND ".join(where)}
            ORDER BY t."updatedAt" DESC
            LIMIT {bind("limit", limit)}
        """  # noqa: S608
        return await self.execute_query(query, params)

    @staticmethod
    def _to_thread_dict(row: dict[str, Any]) -> ThreadDict:
        return ThreadDict(
            id=str(row["id"]),
            createdAt=row["updatedAt"].isoformat(),
            name=display_name(row["name"], row["groupName"]),
            userId=str(row["userId"]) if row["userId"] else None,
            userIdentifier=row["user_identifier"],
            metadata=json.loads(row["metadata"]),
            steps=[],
            elements=[],
            tags=[],
        )

    # ---------- アーカイブとグループ分け（本人のチャットだけを更新する） ----------
    async def set_archived(self, thread_id: str, user_id: str, archived: bool) -> dict[str, Any] | None:
        """アーカイブする、または一覧に戻す。本人のチャットでなければ何もせず None を返す。"""
        rows = await self.execute_query(
            """
            UPDATE "Thread"
            SET "archivedAt" = CASE WHEN $3::boolean THEN LOCALTIMESTAMP ELSE NULL END
            WHERE id = $1 AND "userId" = $2
            RETURNING name, "groupName"
            """,
            {"thread_id": thread_id, "user_id": user_id, "archived": archived},
        )
        return rows[0] if rows else None

    async def set_group(self, thread_id: str, user_id: str, group: str | None) -> dict[str, Any] | None:
        """グループ名を付ける（None なら外す）。本人のチャットでなければ何もせず None を返す。"""
        rows = await self.execute_query(
            """
            UPDATE "Thread" SET "groupName" = $3
            WHERE id = $1 AND "userId" = $2
            RETURNING name, "groupName"
            """,
            {"thread_id": thread_id, "user_id": user_id, "group": group},
        )
        return rows[0] if rows else None

    # ---------- 画面を通さない書き込み（定時実行が使う） ----------
    # Chainlit の保存機能は、画面とつながっているときしか使えない。無人で実行した結果を
    # 左の一覧から開けるように、チャットとメッセージを直接書き込む
    async def create_thread(
        self, thread_id: str, user_id: str, name: str, *, group: str | None = None, model: str | None = None
    ) -> None:
        await self.execute_query(
            """
            INSERT INTO "Thread" (id, "userId", name, "groupName", "modelName")
            VALUES ($1, $2, $3, $4, $5)
            """,
            {"id": thread_id, "user_id": user_id, "name": name[:255], "group": group, "model": model},
        )

    async def add_message(self, thread_id: str, step_type: str, author: str, content: str) -> None:
        """メッセージを1つ足す。step_type は user_message か assistant_message。"""
        await self.execute_query(
            """
            INSERT INTO "Step" ("threadId", type, name, output, "startTime", "endTime")
            VALUES ($1, $2, $3, $4, LOCALTIMESTAMP, LOCALTIMESTAMP)
            """,
            {"thread_id": thread_id, "type": step_type, "author": author, "content": content},
        )
        await self.execute_query('UPDATE "Thread" SET "updatedAt" = LOCALTIMESTAMP WHERE id = $1', {"thread_id": thread_id})

    async def get_user_identifier(self, user_id: str) -> str | None:
        rows = await self.execute_query('SELECT identifier FROM "User" WHERE id = $1', {"user_id": user_id})
        return rows[0]["identifier"] if rows else None

    # ---------- チャットごとのモデル ----------
    async def set_model(self, thread_id: str, user_id: str, model: str) -> dict[str, Any] | None:
        """このチャットで選んだモデルを覚える。本人のチャットでなければ何もせず None を返す。"""
        rows = await self.execute_query(
            'UPDATE "Thread" SET "modelName" = $3 WHERE id = $1 AND "userId" = $2 RETURNING "modelName"',
            {"thread_id": thread_id, "user_id": user_id, "model": model},
        )
        return rows[0] if rows else None

    async def get_model(self, thread_id: str) -> str | None:
        rows = await self.execute_query('SELECT "modelName" FROM "Thread" WHERE id = $1', {"thread_id": thread_id})
        return rows[0]["modelName"] if rows else None

    async def scrub_user_input(self, thread_id: str, step_id: str, raw: str, masked: str) -> None:
        """ガードレールがマスクした入力について、保存済みの元の文章を、マスクしたものに置き換える。

        Chainlit は、利用者のメッセージと、最初のメッセージから付けるチャットの名前を、
        こちらの処理とは別に裏で保存する。その保存が終わったあとに呼ぶこと。
        """
        await self.execute_query(
            'UPDATE "Step" SET output = $2 WHERE id = $1 AND "threadId" = $3',
            {"step_id": step_id, "masked": masked, "thread_id": thread_id},
        )
        # 「処理の記録」（on_message の行）にも、入力の文章がそのまま入っていることがある
        await self.execute_query(
            'UPDATE "Step" SET input = $2 WHERE "threadId" = $1 AND input = $3',
            {"thread_id": thread_id, "masked": masked, "raw": raw},
        )
        rows = await self.execute_query('SELECT name FROM "Thread" WHERE id = $1', {"thread_id": thread_id})
        name = rows[0]["name"] if rows else None
        # 名前が元の文章の先頭と同じなら、このメッセージから付いた名前なので、マスクしたものに付け替える
        if name and raw.startswith(name):
            await self.execute_query(
                'UPDATE "Thread" SET name = $2 WHERE id = $1',
                {"thread_id": thread_id, "name": masked[:255]},
            )

    async def update_thread(
        self,
        thread_id: str,
        name: str | None = None,
        user_id: str | None = None,
        metadata: dict | None = None,
        tags: list[str] | None = None,
    ) -> None:
        if name is not None and self._mask is not None:
            name = self._mask(name)  # 最初のメッセージから付く名前にも、マスクする前の文章が入る
        if name is not None:
            # 一覧では [グループ名] を付けて見せているので、画面で名前を変更すると、それが付いたまま送られてくる
            rows = await self.execute_query('SELECT "groupName" FROM "Thread" WHERE id = $1', {"thread_id": thread_id})
            if rows:
                name = strip_group_prefix(name, rows[0]["groupName"])
        await super().update_thread(thread_id, name=name, user_id=user_id, metadata=metadata, tags=tags)


def build_data_layer(
    settings: Settings,
    on_thread_deleted: Callable[[str], Awaitable[None]] | None = None,
    mask: Callable[[str], str] | None = None,
) -> ChatHistoryDataLayer:
    url = database_url(settings)
    if url is None:
        raise RuntimeError("過去のチャットの保存には DB_HOST の設定が必要です。")
    return ChatHistoryDataLayer(url, pool_max=settings.db_pool_max, on_thread_deleted=on_thread_deleted, mask=mask)
