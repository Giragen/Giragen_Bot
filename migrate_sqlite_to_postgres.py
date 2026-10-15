r"""Copy Unrite's SQLite tables to PostgreSQL without deleting the source DB.

Usage:
  Windows PowerShell:
    $env:DATABASE_URL = "postgresql://..."
    .\.venv\Scripts\python.exe .\migrate_sqlite_to_postgres.py --sqlite .\data\unrite.sqlite3

Run this before switching the bot to PostgreSQL. Conflicting rows in PostgreSQL
are left untouched (ON CONFLICT DO NOTHING); the script never deletes source rows.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
from pathlib import Path

import asyncpg

from unrite_bot.database import Database

TABLES = ("guild_settings", "permissions", "tier_roles", "rank_roles", "archives")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate Unrite data from SQLite to PostgreSQL")
    parser.add_argument(
        "--sqlite",
        default="data/unrite.sqlite3",
        help="Path to the existing SQLite DB (default: data/unrite.sqlite3)",
    )
    return parser.parse_args()


async def migrate(sqlite_path: Path, database_url: str) -> None:
    if not sqlite_path.is_file():
        raise FileNotFoundError(
            f"SQLite database not found: {sqlite_path.resolve()}\n"
            "Copy the old data/unrite.sqlite3 file here first, if you need to preserve it."
        )

    # Create PostgreSQL tables before transferring rows.
    schema_db = Database(database_url)
    await schema_db.initialize()
    await schema_db.close()

    with sqlite3.connect(str(sqlite_path)) as sqlite_db:
        sqlite_db.row_factory = sqlite3.Row
        existing_tables = {
            row[0]
            for row in sqlite_db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        missing = set(TABLES) - existing_tables
        if missing:
            raise RuntimeError(f"SQLite database is missing expected tables: {', '.join(sorted(missing))}")
        source_rows = {
            table: sqlite_db.execute(f"SELECT * FROM {table}").fetchall()
            for table in TABLES
        }

    dsn = database_url
    if dsn.startswith("postgres://"):
        dsn = "postgresql://" + dsn[len("postgres://"):]
    elif dsn.startswith("postgresql+asyncpg://"):
        dsn = "postgresql://" + dsn[len("postgresql+asyncpg://"):]

    connection = await asyncpg.connect(dsn)
    try:
        totals: dict[str, int] = {}
        async with connection.transaction():
            for table in TABLES:
                rows = source_rows[table]
                if not rows:
                    totals[table] = 0
                    continue
                columns = list(rows[0].keys())
                column_sql = ", ".join(columns)
                placeholders = ", ".join(f"${i}" for i in range(1, len(columns) + 1))
                statement = (
                    f"INSERT INTO {table} ({column_sql}) VALUES ({placeholders}) "
                    "ON CONFLICT DO NOTHING"
                )
                values = [tuple(row[column] for column in columns) for row in rows]
                await connection.executemany(statement, values)
                totals[table] = len(rows)

        print("Migration finished. Source SQLite database was not modified.")
        for table, count in totals.items():
            print(f"  {table}: {count} source rows processed")
        print("Rows that conflict with existing PostgreSQL primary/unique keys were skipped.")
    finally:
        await connection.close()


async def main() -> None:
    args = parse_args()
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("Set DATABASE_URL to the PostgreSQL connection string before running this script.")
    await migrate(Path(args.sqlite).expanduser().resolve(), database_url)


if __name__ == "__main__":
    asyncio.run(main())
