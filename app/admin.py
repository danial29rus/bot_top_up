"""Small local utility for administering the SQLite database."""
from __future__ import annotations

import argparse
import asyncio
import os

from .database import Database


async def run() -> None:
    parser = argparse.ArgumentParser(description="Manage bot administrators")
    parser.add_argument("action", choices=["add"])
    parser.add_argument("telegram_id", type=int)
    parser.add_argument("--db", default=os.getenv("DATABASE_PATH", "/data/bot.sqlite3"))
    args = parser.parse_args()
    database = Database(args.db)
    await database.init()
    await database.add_admin(args.telegram_id)
    print(f"Administrator {args.telegram_id} added to {args.db}")


if __name__ == "__main__":
    asyncio.run(run())
