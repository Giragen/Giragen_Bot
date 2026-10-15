from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiosqlite


ACTION_FEATURES = (
    "delete_channel",
    "promote_rank",
    "demote_rank",
    "curator_assign",
    "curator_unassign",
    "tier_1",
    "tier_2",
    "tier_3",
)


POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id BIGINT PRIMARY KEY,
    category_id BIGINT,
    panel_channel_id BIGINT,
    curator_role_id BIGINT
);
CREATE TABLE IF NOT EXISTS permissions (
    guild_id BIGINT NOT NULL,
    role_id BIGINT NOT NULL,
    feature TEXT NOT NULL,
    allowed INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, role_id, feature)
);
CREATE TABLE IF NOT EXISTS tier_roles (
    guild_id BIGINT NOT NULL,
    tier INTEGER NOT NULL,
    role_id BIGINT NOT NULL,
    PRIMARY KEY (guild_id, tier)
);
CREATE TABLE IF NOT EXISTS rank_roles (
    guild_id BIGINT NOT NULL,
    position INTEGER NOT NULL,
    role_id BIGINT NOT NULL,
    PRIMARY KEY (guild_id, position)
);
CREATE TABLE IF NOT EXISTS archives (
    guild_id BIGINT NOT NULL,
    owner_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL UNIQUE,
    rank_position INTEGER NOT NULL DEFAULT 0,
    tier INTEGER NOT NULL DEFAULT 0,
    curator_assigned INTEGER NOT NULL DEFAULT 0,
    thread_ids TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (guild_id, owner_id)
);
"""


class _PostgresCursor:
    """Small adapter that preserves the execute/fetch API used by the SQLite backend."""

    def __init__(self, rows: list[Any] | None = None, rowcount: int = -1) -> None:
        self._rows = rows or []
        self.rowcount = rowcount

    async def fetchone(self) -> Any | None:
        return self._rows[0] if self._rows else None

    async def fetchall(self) -> list[Any]:
        return list(self._rows)


class _PostgresConnection:
    def __init__(self, connection: Any, pool: Any) -> None:
        self._connection = connection
        self._pool = pool
        self._closed = False

    @staticmethod
    def _translate(query: str) -> tuple[str, bool]:
        """Translate the small SQLite SQL subset used by this project to PostgreSQL."""
        ignore_conflicts = bool(
            re.match(r"^\s*INSERT\s+OR\s+IGNORE\s+INTO\b", query, flags=re.IGNORECASE)
        )
        if ignore_conflicts:
            query = re.sub(
                r"^\s*INSERT\s+OR\s+IGNORE\s+INTO\b",
                "INSERT INTO",
                query,
                count=1,
                flags=re.IGNORECASE,
            )

        index = 0

        def replace_placeholder(_: re.Match[str]) -> str:
            nonlocal index
            index += 1
            return f"${index}"

        query = re.sub(r"\?", replace_placeholder, query)
        if ignore_conflicts:
            query = query.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
        return query, ignore_conflicts

    async def execute(self, query: str, parameters: tuple[Any, ...] | list[Any] = ()) -> _PostgresCursor:
        sql, _ = self._translate(query)
        parameters = tuple(parameters or ())
        first_word = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
        if first_word in {"SELECT", "WITH"}:
            # The current application only uses SELECT CTE-free queries. fetch() also
            # supports ordinary SELECT statements through asyncpg's prepared execution.
            rows = await self._connection.fetch(sql, *parameters)
            return _PostgresCursor(rows, len(rows))

        status = await self._connection.execute(sql, *parameters)
        match = re.search(r"(\d+)\s*$", status)
        rowcount = int(match.group(1)) if match else -1
        return _PostgresCursor(rowcount=rowcount)

    async def commit(self) -> None:
        # asyncpg statements run in autocommit unless explicitly wrapped in a transaction.
        return None

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            await self._pool.release(self._connection)


class Database:
    def __init__(self, target: str) -> None:
        self.target = target
        self.is_postgres = target.strip().lower().startswith(("postgres://", "postgresql://", "postgresql+asyncpg://"))
        self.path = target
        self._pool: Any | None = None

    @property
    def backend_label(self) -> str:
        if self.is_postgres:
            parsed = urlsplit(self.target)
            host = parsed.hostname or "PostgreSQL"
            return f"PostgreSQL ({host})"
        return f"SQLite ({Path(self.path).resolve()})"

    async def initialize(self) -> None:
        if self.is_postgres:
            await self._initialize_postgres()
            return

        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA foreign_keys=ON")
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id INTEGER PRIMARY KEY,
                    category_id INTEGER,
                    panel_channel_id INTEGER,
                    curator_role_id INTEGER
                );

                CREATE TABLE IF NOT EXISTS permissions (
                    guild_id INTEGER NOT NULL,
                    role_id INTEGER NOT NULL,
                    feature TEXT NOT NULL,
                    allowed INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (guild_id, role_id, feature)
                );

                CREATE TABLE IF NOT EXISTS tier_roles (
                    guild_id INTEGER NOT NULL,
                    tier INTEGER NOT NULL,
                    role_id INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, tier)
                );

                CREATE TABLE IF NOT EXISTS rank_roles (
                    guild_id INTEGER NOT NULL,
                    position INTEGER NOT NULL,
                    role_id INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, position)
                );

                CREATE TABLE IF NOT EXISTS archives (
                    guild_id INTEGER NOT NULL,
                    owner_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL UNIQUE,
                    rank_position INTEGER NOT NULL DEFAULT 0,
                    tier INTEGER NOT NULL DEFAULT 0,
                    curator_assigned INTEGER NOT NULL DEFAULT 0,
                    thread_ids TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (guild_id, owner_id)
                );
                """
            )
            await db.commit()

    async def _initialize_postgres(self) -> None:
        try:
            import asyncpg
        except ImportError as exc:  # pragma: no cover - deployment configuration error
            raise RuntimeError(
                "DATABASE_URL задан для PostgreSQL, но пакет asyncpg не установлен. "
                "Установите зависимости из requirements.txt."
            ) from exc

        dsn = self.target
        if dsn.startswith("postgres://"):
            dsn = "postgresql://" + dsn[len("postgres://"):]
        elif dsn.startswith("postgresql+asyncpg://"):
            dsn = "postgresql://" + dsn[len("postgresql+asyncpg://"):]
        # Railway databases may take a short time to become ready on a cold deployment.
        last_error: Exception | None = None
        for attempt in range(1, 6):
            pool = None
            try:
                pool = await asyncpg.create_pool(
                    dsn=dsn,
                    min_size=1,
                    max_size=5,
                    command_timeout=30,
                )
                async with pool.acquire() as connection:
                    for statement in POSTGRES_SCHEMA.split(";"):
                        if statement.strip():
                            await connection.execute(statement)
                self._pool = pool
                return
            except Exception as exc:
                last_error = exc
                if pool is not None:
                    try:
                        await pool.close()
                    except Exception:
                        pass
                if attempt < 5:
                    await asyncio.sleep(3)
        raise RuntimeError(
            "Не удалось подключиться к PostgreSQL. Проверьте DATABASE_URL и доступность "
            "PostgreSQL-сервиса Railway."
        ) from last_error

    async def _connect(self) -> Any:
        if self.is_postgres:
            if self._pool is None:
                raise RuntimeError("PostgreSQL pool is not initialized")
            connection = await self._pool.acquire()
            return _PostgresConnection(connection, self._pool)

        db = await aiosqlite.connect(self.path)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys=ON")
        return db

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def get_guild_settings(self, guild_id: int) -> dict[str, Any]:
        db = await self._connect()
        try:
            cursor = await db.execute(
                "SELECT * FROM guild_settings WHERE guild_id = ?", (guild_id,)
            )
            row = await cursor.fetchone()
            return dict(row) if row else {
                "guild_id": guild_id,
                "category_id": None,
                "panel_channel_id": None,
                "curator_role_id": None,
            }
        finally:
            await db.close()

    async def set_guild_setting(self, guild_id: int, field: str, value: int | None) -> None:
        allowed_fields = {"category_id", "panel_channel_id", "curator_role_id"}
        if field not in allowed_fields:
            raise ValueError(f"Unsupported guild setting: {field}")
        db = await self._connect()
        try:
            await db.execute(
                "INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (guild_id,)
            )
            await db.execute(
                f"UPDATE guild_settings SET {field} = ? WHERE guild_id = ?",
                (value, guild_id),
            )
            await db.commit()
        finally:
            await db.close()

    async def set_permission(
        self, guild_id: int, role_id: int, feature: str, allowed: bool
    ) -> None:
        db = await self._connect()
        try:
            await db.execute(
                """INSERT INTO permissions (guild_id, role_id, feature, allowed)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(guild_id, role_id, feature)
                   DO UPDATE SET allowed = excluded.allowed""",
                (guild_id, role_id, feature, int(allowed)),
            )
            await db.commit()
        finally:
            await db.close()

    async def member_has_feature(
        self, guild_id: int, role_ids: list[int], feature: str
    ) -> bool:
        if not role_ids:
            return False
        placeholders = ",".join("?" for _ in role_ids)
        db = await self._connect()
        try:
            cursor = await db.execute(
                f"""SELECT 1 FROM permissions
                    WHERE guild_id = ? AND feature = ? AND allowed = 1
                    AND role_id IN ({placeholders}) LIMIT 1""",
                (guild_id, feature, *role_ids),
            )
            return await cursor.fetchone() is not None
        finally:
            await db.close()

    async def role_has_any_action_permission(self, guild_id: int, role_id: int) -> bool:
        placeholders = ",".join("?" for _ in ACTION_FEATURES)
        db = await self._connect()
        try:
            cursor = await db.execute(
                f"""SELECT 1 FROM permissions
                    WHERE guild_id = ? AND role_id = ? AND allowed = 1
                    AND feature IN ({placeholders}) LIMIT 1""",
                (guild_id, role_id, *ACTION_FEATURES),
            )
            return await cursor.fetchone() is not None
        finally:
            await db.close()

    async def manager_role_ids(self, guild_id: int) -> list[int]:
        placeholders = ",".join("?" for _ in ACTION_FEATURES)
        db = await self._connect()
        try:
            cursor = await db.execute(
                f"""SELECT DISTINCT role_id FROM permissions
                    WHERE guild_id = ? AND allowed = 1
                    AND feature IN ({placeholders})""",
                (guild_id, *ACTION_FEATURES),
            )
            return [int(row[0]) for row in await cursor.fetchall()]
        finally:
            await db.close()

    async def list_permissions(self, guild_id: int) -> list[dict[str, Any]]:
        db = await self._connect()
        try:
            cursor = await db.execute(
                "SELECT role_id, feature, allowed FROM permissions WHERE guild_id = ? ORDER BY role_id, feature",
                (guild_id,),
            )
            return [dict(row) for row in await cursor.fetchall()]
        finally:
            await db.close()

    async def set_tier_role(self, guild_id: int, tier: int, role_id: int) -> None:
        db = await self._connect()
        try:
            await db.execute(
                """INSERT INTO tier_roles (guild_id, tier, role_id) VALUES (?, ?, ?)
                   ON CONFLICT(guild_id, tier) DO UPDATE SET role_id = excluded.role_id""",
                (guild_id, tier, role_id),
            )
            await db.commit()
        finally:
            await db.close()

    async def get_tier_role(self, guild_id: int, tier: int) -> int | None:
        db = await self._connect()
        try:
            cursor = await db.execute(
                "SELECT role_id FROM tier_roles WHERE guild_id = ? AND tier = ?",
                (guild_id, tier),
            )
            row = await cursor.fetchone()
            return int(row[0]) if row else None
        finally:
            await db.close()

    async def list_tier_roles(self, guild_id: int) -> dict[int, int]:
        db = await self._connect()
        try:
            cursor = await db.execute(
                "SELECT tier, role_id FROM tier_roles WHERE guild_id = ? ORDER BY tier",
                (guild_id,),
            )
            return {int(row[0]): int(row[1]) for row in await cursor.fetchall()}
        finally:
            await db.close()

    async def set_rank_role(self, guild_id: int, position: int, role_id: int) -> None:
        db = await self._connect()
        try:
            await db.execute(
                """INSERT INTO rank_roles (guild_id, position, role_id) VALUES (?, ?, ?)
                   ON CONFLICT(guild_id, position) DO UPDATE SET role_id = excluded.role_id""",
                (guild_id, position, role_id),
            )
            await db.commit()
        finally:
            await db.close()

    async def list_rank_roles(self, guild_id: int) -> dict[int, int]:
        db = await self._connect()
        try:
            cursor = await db.execute(
                "SELECT position, role_id FROM rank_roles WHERE guild_id = ? ORDER BY position",
                (guild_id,),
            )
            return {int(row[0]): int(row[1]) for row in await cursor.fetchall()}
        finally:
            await db.close()

    async def create_archive(
        self, guild_id: int, owner_id: int, channel_id: int, thread_ids: dict[str, int]
    ) -> None:
        db = await self._connect()
        try:
            await db.execute(
                """INSERT INTO archives (guild_id, owner_id, channel_id, thread_ids)
                   VALUES (?, ?, ?, ?)""",
                (guild_id, owner_id, channel_id, json.dumps(thread_ids, ensure_ascii=False)),
            )
            await db.commit()
        finally:
            await db.close()

    async def get_archive_by_owner(self, guild_id: int, owner_id: int) -> dict[str, Any] | None:
        db = await self._connect()
        try:
            cursor = await db.execute(
                "SELECT * FROM archives WHERE guild_id = ? AND owner_id = ?",
                (guild_id, owner_id),
            )
            row = await cursor.fetchone()
            return self._archive_row(row) if row else None
        finally:
            await db.close()

    async def get_archive_by_channel(self, channel_id: int) -> dict[str, Any] | None:
        db = await self._connect()
        try:
            cursor = await db.execute(
                "SELECT * FROM archives WHERE channel_id = ?", (channel_id,)
            )
            row = await cursor.fetchone()
            return self._archive_row(row) if row else None
        finally:
            await db.close()

    async def rebind_archive_channel(
        self, guild_id: int, owner_id: int, channel_id: int, thread_ids: dict[str, int]
    ) -> None:
        """Repair an owner's archive pointer when the old channel was deleted or DB path changed."""
        db = await self._connect()
        try:
            cursor = await db.execute(
                """UPDATE archives SET channel_id = ?, thread_ids = ?
                   WHERE guild_id = ? AND owner_id = ?""",
                (channel_id, json.dumps(thread_ids, ensure_ascii=False), guild_id, owner_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("No existing archive row found for this owner")
            await db.commit()
        finally:
            await db.close()

    async def list_archives(self, guild_id: int) -> list[dict[str, Any]]:
        db = await self._connect()
        try:
            cursor = await db.execute(
                "SELECT * FROM archives WHERE guild_id = ? ORDER BY created_at", (guild_id,)
            )
            return [self._archive_row(row) for row in await cursor.fetchall()]
        finally:
            await db.close()

    async def update_archive(self, channel_id: int, **fields: Any) -> None:
        allowed_fields = {"rank_position", "tier", "curator_assigned", "thread_ids"}
        if not fields or not set(fields).issubset(allowed_fields):
            raise ValueError("Invalid archive update fields")
        if "thread_ids" in fields:
            fields["thread_ids"] = json.dumps(fields["thread_ids"], ensure_ascii=False)
        assignments = ", ".join(f"{field} = ?" for field in fields)
        values = list(fields.values()) + [channel_id]
        db = await self._connect()
        try:
            await db.execute(f"UPDATE archives SET {assignments} WHERE channel_id = ?", values)
            await db.commit()
        finally:
            await db.close()

    async def delete_archive(self, channel_id: int) -> None:
        db = await self._connect()
        try:
            await db.execute("DELETE FROM archives WHERE channel_id = ?", (channel_id,))
            await db.commit()
        finally:
            await db.close()

    @staticmethod
    def _archive_row(row: Any) -> dict[str, Any]:
        result = dict(row)
        try:
            result["thread_ids"] = json.loads(result.get("thread_ids") or "{}")
        except (json.JSONDecodeError, TypeError):
            result["thread_ids"] = {}
        return result
