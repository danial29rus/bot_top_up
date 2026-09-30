from __future__ import annotations

import aiosqlite
import json
from pathlib import Path
import secrets
from datetime import datetime, timezone
from typing import Any


class Database:
    def __init__(self, path: str):
        self.path = path

    async def init(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS admins (
                    user_id INTEGER PRIMARY KEY,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    terms_accepted_at TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    public_id TEXT,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    product TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    price_usd TEXT,
                    status TEXT NOT NULL DEFAULT 'awaiting_payment',
                    supplier_order_id INTEGER,
                    supplier_status TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER,
                    user_id INTEGER,
                    actor_user_id INTEGER,
                    action TEXT NOT NULL,
                    details TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(order_id) REFERENCES orders(id)
                );
                CREATE TABLE IF NOT EXISTS support_tickets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    closed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS support_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id INTEGER NOT NULL,
                    sender_id INTEGER NOT NULL,
                    sender_role TEXT NOT NULL CHECK(sender_role IN ('customer', 'admin')),
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(ticket_id) REFERENCES support_tickets(id)
                );
                CREATE INDEX IF NOT EXISTS orders_user_idx ON orders(user_id, id DESC);
                CREATE INDEX IF NOT EXISTS orders_status_idx ON orders(status, id DESC);
                CREATE INDEX IF NOT EXISTS audit_order_idx ON audit_events(order_id, id ASC);
                CREATE INDEX IF NOT EXISTS tickets_status_idx ON support_tickets(status, id ASC);
                CREATE INDEX IF NOT EXISTS ticket_messages_idx ON support_messages(ticket_id, id ASC);
                """
            )
            await self._ensure_order_columns(db)
            await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS orders_public_id_idx ON orders(public_id)")
            await self._backfill_public_ids(db)
            await db.commit()

    @staticmethod
    async def _ensure_order_columns(db: aiosqlite.Connection) -> None:
        """Small SQLite migration for installations created by earlier bot versions."""
        existing = {row[1] for row in await (await db.execute("PRAGMA table_info(orders)")).fetchall()}
        additions = {
            "supplier_cost_usd": "TEXT",
            "usd_rub_rate": "TEXT",
            "markup_percent": "TEXT",
            "price_rub": "INTEGER",
            "price_source": "TEXT",
            "public_id": "TEXT",
        }
        for column, definition in additions.items():
            if column not in existing:
                await db.execute(f"ALTER TABLE orders ADD COLUMN {column} {definition}")

    @staticmethod
    def _public_id() -> str:
        now = datetime.now(timezone.utc)
        suffix = secrets.token_urlsafe(4).upper().replace("-", "X").replace("_", "Y")[:5]
        return f"NT-{now:%y%m%d-%H%M}-{suffix}"

    async def _backfill_public_ids(self, db: aiosqlite.Connection) -> None:
        rows = await (await db.execute("SELECT id FROM orders WHERE public_id IS NULL OR public_id=''" )).fetchall()
        for (order_id,) in rows:
            for _ in range(3):
                try:
                    await db.execute("UPDATE orders SET public_id=? WHERE id=?", (self._public_id(), order_id))
                    break
                except aiosqlite.IntegrityError:
                    continue
            else:
                raise RuntimeError("Не удалось обновить публичный номер заявки")

    async def upsert_user(self, user_id: int, username: str | None, first_name: str | None) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """INSERT INTO users(user_id, username, first_name) VALUES (?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name, updated_at=CURRENT_TIMESTAMP""",
                (user_id, username, first_name),
            )
            await db.commit()

    async def accepted_terms(self, user_id: int) -> bool:
        async with aiosqlite.connect(self.path) as db:
            row = await (await db.execute("SELECT terms_accepted_at FROM users WHERE user_id=?", (user_id,))).fetchone()
        return bool(row and row[0])

    async def accept_terms(self, user_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE users SET terms_accepted_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE user_id=?", (user_id,))
            await self._event(db, None, user_id, user_id, "terms_accepted", {})
            await db.commit()

    @staticmethod
    async def _event(
        db: aiosqlite.Connection,
        order_id: int | None,
        user_id: int | None,
        actor_user_id: int | None,
        action: str,
        details: dict[str, Any],
    ) -> None:
        await db.execute(
            "INSERT INTO audit_events(order_id, user_id, actor_user_id, action, details) VALUES (?, ?, ?, ?, ?)",
            (order_id, user_id, actor_user_id, action, json.dumps(details, ensure_ascii=False)),
        )

    async def add_admin(self, user_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("INSERT OR IGNORE INTO admins(user_id) VALUES (?)", (user_id,))
            await db.commit()

    async def is_admin(self, user_id: int) -> bool:
        async with aiosqlite.connect(self.path) as db:
            row = await (await db.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,))).fetchone()
        return row is not None

    async def admin_ids(self) -> list[int]:
        async with aiosqlite.connect(self.path) as db:
            rows = await (await db.execute("SELECT user_id FROM admins")).fetchall()
        return [row[0] for row in rows]

    async def create_order(
        self,
        user_id: int,
        username: str | None,
        product: str,
        payload: dict[str, Any],
        supplier_cost_usd: str | None,
        usd_rub_rate: str | None,
        markup_percent: str | None,
        price_rub: int | None,
        price_source: str | None,
        status: str = "awaiting_payment",
    ) -> int:
        async with aiosqlite.connect(self.path) as db:
            for _ in range(3):
                public_id = self._public_id()
                try:
                    cursor = await db.execute(
                        """INSERT INTO orders(public_id, user_id, username, product, payload, supplier_cost_usd, usd_rub_rate,
                           markup_percent, price_rub, price_source, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (public_id, user_id, username, product, json.dumps(payload, ensure_ascii=False), supplier_cost_usd, usd_rub_rate, markup_percent, price_rub, price_source, status),
                    )
                    break
                except aiosqlite.IntegrityError:
                    continue
            else:
                raise RuntimeError("Не удалось сгенерировать уникальный номер заявки")
            await self._event(db, int(cursor.lastrowid), user_id, user_id, "order_created", {
                "product": product, "payload": payload, "supplier_cost_usd": supplier_cost_usd,
                "usd_rub_rate": usd_rub_rate, "markup_percent": markup_percent, "price_rub": price_rub,
                "price_source": price_source, "public_id": public_id, "status": status,
            })
            await db.commit()
            return int(cursor.lastrowid)

    @staticmethod
    def _to_dict(row: aiosqlite.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        value = dict(row)
        value["payload"] = json.loads(value["payload"])
        return value

    async def get_order(self, order_id: int) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute("SELECT * FROM orders WHERE id = ?", (order_id,))).fetchone()
        return self._to_dict(row)

    async def audit_events(self, order_id: int, limit: int = 50) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(
                "SELECT * FROM audit_events WHERE order_id=? ORDER BY id ASC LIMIT ?", (order_id, limit)
            )).fetchall()
        events = []
        for row in rows:
            event = dict(row)
            event["details"] = json.loads(event["details"])
            events.append(event)
        return events

    async def user_orders(self, user_id: int, limit: int = 10) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute("SELECT * FROM orders WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit))).fetchall()
        return [self._to_dict(row) for row in rows]

    async def user_orders_by_product(self, user_id: int, products: tuple[str, ...], limit: int = 10) -> list[dict[str, Any]]:
        placeholders = ",".join("?" * len(products))
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(
                f"SELECT * FROM orders WHERE user_id=? AND product IN ({placeholders}) ORDER BY id DESC LIMIT ?",
                (user_id, *products, limit),
            )).fetchall()
        return [self._to_dict(row) for row in rows]

    async def user_stats(self, user_id: int) -> dict[str, int]:
        async with aiosqlite.connect(self.path) as db:
            row = await (await db.execute(
                """SELECT COUNT(*) AS total,
                   COALESCE(SUM(CASE WHEN status IN ('awaiting_price','awaiting_payment','payment_review','creating','created','processing') THEN 1 ELSE 0 END), 0) AS active,
                   COALESCE(SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END), 0) AS completed
                   FROM orders WHERE user_id=?""",
                (user_id,),
            )).fetchone()
        return {"total": row[0], "active": row[1], "completed": row[2]}

    async def create_support_ticket(self, user_id: int, body: str) -> int:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("INSERT INTO support_tickets(user_id) VALUES (?)", (user_id,))
            ticket_id = int(cursor.lastrowid)
            await db.execute(
                "INSERT INTO support_messages(ticket_id, sender_id, sender_role, body) VALUES (?, ?, 'customer', ?)",
                (ticket_id, user_id, body),
            )
            await db.commit()
            return ticket_id

    async def get_ticket(self, ticket_id: int) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute("SELECT * FROM support_tickets WHERE id=?", (ticket_id,))).fetchone()
        return dict(row) if row else None

    async def open_tickets(self, limit: int = 20) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(
                "SELECT * FROM support_tickets WHERE status='open' ORDER BY id ASC LIMIT ?", (limit,)
            )).fetchall()
        return [dict(row) for row in rows]

    async def user_tickets(self, user_id: int, limit: int = 10) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(
                "SELECT * FROM support_tickets WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit)
            )).fetchall()
        return [dict(row) for row in rows]

    async def ticket_messages(self, ticket_id: int) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(
                "SELECT * FROM support_messages WHERE ticket_id=? ORDER BY id ASC", (ticket_id,)
            )).fetchall()
        return [dict(row) for row in rows]

    async def add_ticket_message(self, ticket_id: int, sender_id: int, sender_role: str, body: str) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "INSERT INTO support_messages(ticket_id, sender_id, sender_role, body) SELECT ?, ?, ?, ? WHERE EXISTS (SELECT 1 FROM support_tickets WHERE id=? AND status='open')",
                (ticket_id, sender_id, sender_role, body, ticket_id),
            )
            if cursor.rowcount == 1:
                await db.execute("UPDATE support_tickets SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (ticket_id,))
            await db.commit()
            return cursor.rowcount == 1

    async def close_ticket(self, ticket_id: int) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "UPDATE support_tickets SET status='closed', closed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='open'", (ticket_id,)
            )
            await db.commit()
            return cursor.rowcount == 1

    async def orders_for_admin(self, statuses: tuple[str, ...], limit: int = 20) -> list[dict[str, Any]]:
        placeholders = ",".join("?" * len(statuses))
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(
                f"SELECT * FROM orders WHERE status IN ({placeholders}) ORDER BY id ASC LIMIT ?",
                (*statuses, limit),
            )).fetchall()
        return [self._to_dict(row) for row in rows]

    async def mark_payment_review(self, order_id: int, user_id: int) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "UPDATE orders SET status='payment_review', updated_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=? AND status='awaiting_payment'",
                (order_id, user_id),
            )
            if cur.rowcount == 1:
                await self._event(db, order_id, user_id, user_id, "payment_marked_by_customer", {})
            await db.commit()
            return cur.rowcount == 1

    async def claim_for_creation(self, order_id: int, actor_user_id: int) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "UPDATE orders SET status='creating', updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='payment_review'",
                (order_id,),
            )
            if cur.rowcount != 1:
                await db.rollback()
                return None
            row = await (await db.execute("SELECT * FROM orders WHERE id=?", (order_id,))).fetchone()
            await self._event(db, order_id, row["user_id"], actor_user_id, "payment_confirmed_by_admin", {})
            await self._event(db, order_id, row["user_id"], actor_user_id, "supplier_request_started", {"product": row["product"]})
            await db.commit()
        return self._to_dict(row)

    async def set_price(self, order_id: int, price_rub: int, actor_user_id: int) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "UPDATE orders SET price_rub=?, status='awaiting_payment', updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='awaiting_price'",
                (price_rub, order_id),
            )
            if cur.rowcount == 1:
                row = await (await db.execute("SELECT user_id FROM orders WHERE id=?", (order_id,))).fetchone()
                await self._event(db, order_id, row[0], actor_user_id, "price_set_by_admin", {"price_rub": price_rub})
            await db.commit()
            return cur.rowcount == 1

    async def set_supplier_result(self, order_id: int, supplier_id: int, supplier_status: str, charged_usd: str | None = None) -> None:
        status = supplier_status.lower()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE orders SET supplier_order_id=?, supplier_status=?, status=?, supplier_cost_usd=COALESCE(?, supplier_cost_usd), updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (supplier_id, supplier_status, status, charged_usd, order_id),
            )
            row = await (await db.execute("SELECT user_id FROM orders WHERE id=?", (order_id,))).fetchone()
            await self._event(db, order_id, row[0], None, "supplier_order_created", {"supplier_order_id": supplier_id, "supplier_status": supplier_status, "charged_usd": charged_usd})
            await db.commit()

    async def set_error(self, order_id: int, error: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE orders SET status='supplier_error', error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (error[:1000], order_id),
            )
            row = await (await db.execute("SELECT user_id FROM orders WHERE id=?", (order_id,))).fetchone()
            await self._event(db, order_id, row[0], None, "supplier_request_error", {"error": error[:1000]})
            await db.commit()

    async def pollable_orders(self) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(
                "SELECT * FROM orders WHERE status IN ('created','processing') AND supplier_order_id IS NOT NULL"
            )).fetchall()
        return [self._to_dict(row) for row in rows]

    async def update_supplier_status(self, order_id: int, supplier_status: str, error: str | None = None) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE orders SET status=?, supplier_status=?, error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (supplier_status.lower(), supplier_status, error, order_id),
            )
            row = await (await db.execute("SELECT user_id FROM orders WHERE id=?", (order_id,))).fetchone()
            await self._event(db, order_id, row[0], None, "supplier_status_changed", {"status": supplier_status, "error": error})
            await db.commit()
