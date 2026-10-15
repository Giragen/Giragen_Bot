from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

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


class Database:
    def __init__(self, path: str) -> None:
        self.path = path

    async def initialize(self) -> None:
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

    async def _connect(self) -> aiosqlite.Connection:
        db = await aiosqlite.connect(self.path)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys=ON")
        return db

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
    def _archive_row(row: aiosqlite.Row) -> dict[str, Any]:
        result = dict(row)
        try:
            result["thread_ids"] = json.loads(result.get("thread_ids") or "{}")
        except (json.JSONDecodeError, TypeError):
            result["thread_ids"] = {}
        return result
