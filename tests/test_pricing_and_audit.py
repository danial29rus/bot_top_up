import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from app.database import Database
from app.pricing import PricingService
from app.resell import ResellClient


class PricingAndAuditTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = Database(str(Path(self.directory.name) / "test.sqlite3"))
        await self.database.init()

    async def asyncTearDown(self):
        self.directory.cleanup()

    async def test_order_keeps_quote_and_audit_chain(self):
        await self.database.upsert_user(10, "buyer", "Buyer")
        order_id = await self.database.create_order(
            10, "buyer", "stars", {"telegram_username": "buyer", "quantity": 100},
            "1.0100", "100.0000", "10", 120, "resell_telegram_stars",
        )
        self.assertTrue(await self.database.mark_payment_review(order_id, 10))
        order = await self.database.claim_for_creation(order_id, 99)
        self.assertEqual(order["status"], "creating")
        await self.database.set_supplier_result(order_id, 77, "processing", "1.0100")
        await self.database.update_supplier_status(order_id, "completed")
        saved = await self.database.get_order(order_id)
        self.assertEqual(saved["price_rub"], 120)
        self.assertEqual(saved["supplier_cost_usd"], "1.0100")
        actions = [event["action"] for event in await self.database.audit_events(order_id)]
        self.assertEqual(actions, [
            "order_created", "payment_marked_by_customer", "payment_confirmed_by_admin",
            "supplier_request_started", "supplier_order_created", "supplier_status_changed",
        ])

    def test_rounds_rub_price_up_to_next_ten(self):
        service = PricingService(None, markup_percent=10, rounding=10)
        quote = service.retail_quote(Decimal("1.01"), Decimal("100"), "test")
        self.assertEqual(quote.price_rub, 120)

    async def test_stars_unit_price_is_multiplied_by_quantity(self):
        class TestResell(ResellClient):
            async def _request(self, method, path, payload=None):
                return {"object": "telegram_stars_pricing", "price_per_star": "0.0152250", "min": 50, "max": 10000}

        self.assertEqual(await TestResell("unused").telegram_stars_price(50), Decimal("0.7612500"))

    async def test_support_ticket_keeps_conversation_in_sqlite(self):
        ticket_id = await self.database.create_support_ticket(10, "Где моя заявка?")
        self.assertTrue(await self.database.add_ticket_message(ticket_id, 99, "admin", "Проверяем, скоро ответим."))
        messages = await self.database.ticket_messages(ticket_id)
        self.assertEqual([message["sender_role"] for message in messages], ["customer", "admin"])
        self.assertTrue(await self.database.close_ticket(ticket_id))
        self.assertFalse(await self.database.add_ticket_message(ticket_id, 10, "customer", "Спасибо"))
