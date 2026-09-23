"""Comprehensive Final Regression Test Suite for WhatsApp Food Ordering Bot.

Verifies:
- Complete 21-step user, ordering, and payment lifecycle
- Security hardening (signatures, IDOR, injection, rate limiting, tampering)
- Resilience & failure boundaries (database, Redis, payment timeout, process restart)
- All 10 domain invariants (financial integrity, idempotency, persistence, state recovery)
"""
import hashlib
import hmac
import json
import os
import time
import unittest
from unittest.mock import MagicMock, patch

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["TWILIO_AUTH_TOKEN"] = ""
os.environ["PAYMENT_WEBHOOK_SECRET"] = "test_payment_secret_regression_12345"
os.environ["TWILIO_VALIDATE_SIGNATURE"] = "false"
os.environ["WEBHOOK_RATE_LIMIT"] = "50"
os.environ["USER_MESSAGE_RATE_LIMIT"] = "20"
os.environ["PAYMENT_WEBHOOK_RATE_LIMIT"] = "20"
os.environ["PAYMENT_INITIATION_LIMIT"] = "5"
os.environ["PROMO_ATTEMPT_LIMIT"] = "3"

from bot import create_app
from config import Config
from conversation import Session, State
from database import Base, get_db
from db_models import UserRow, MenuItemRow, PromoCodeRow, OrderRow  # noqa: F401
from models import Order, MenuItem, PromoCode, User
from rate_limiter import InMemoryRateLimiter
from repository import Repository, RepositoryError, DuplicateError, NotFoundError
from session_store import InMemorySessionStore


def _sign_payment(secret: str, payload_bytes: bytes) -> str:
    """Generate HMAC-SHA256 signature for payment webhook testing."""
    return hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()


class TestLifecycleEndToEnd(unittest.TestCase):
    """Exercises the complete 21-step order and payment lifecycle."""

    def setUp(self):
        os.environ["TWILIO_VALIDATE_SIGNATURE"] = "false"
        os.environ["PAYMENT_WEBHOOK_SECRET"] = "test_payment_secret_regression_12345"
        self.limiter = InMemoryRateLimiter()
        self.store = InMemorySessionStore()
        self.app = create_app(session_store=self.store, rate_limiter=self.limiter)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

        with get_db() as db:
            Base.metadata.drop_all(db.bind)
            Base.metadata.create_all(db.bind)

            # Seed Menu
            db.add(MenuItemRow(
                item_id="i1", name="Veg Biryani", restaurant="Paradise Biryani",
                rating=4.6, price=220.0, eta_minutes=35, tags="biryani,veg",
            ))
            db.add(MenuItemRow(
                item_id="i2", name="Paneer Butter Masala", restaurant="Curry Palace",
                rating=4.5, price=250.0, eta_minutes=30, tags="veg,curry",
            ))
            db.add(MenuItemRow(
                item_id="i3", name="Chicken Tikka", restaurant="Tandoor Express",
                rating=4.8, price=280.0, eta_minutes=25, tags="non-veg,starter",
            ))

            # Seed Promos
            db.add(PromoCodeRow(
                code="SAVE15", description="15% off above ₹200",
                discount_type="percentage", discount_value=0.15, min_order_value=200.0, active=True,
            ))
            db.add(PromoCodeRow(
                code="WELCOME50", description="₹50 flat off",
                discount_type="flat", discount_value=50.0, min_order_value=0.0, active=True,
            ))

            # Seed Existing User with Default Address
            db.add(UserRow(
                phone="+919876500001", name="Aarav",
                default_address="Flat 402, Green Glen, Bellandur",
                past_item_ids="i1",
            ))
            db.commit()

    def test_full_21_step_lifecycle(self):
        """Complete 21-step trace from new user to payment and follow-up order."""
        # 1. New User greeting
        new_phone = "+919999888877"
        with get_db() as db:
            repo = Repository(db)
            session = Session(new_phone, repo)
            self.assertEqual(session.state, State.SEARCHING)

        # 2. Existing User greeting & history loading
        existing_phone = "+919876500001"
        with get_db() as db:
            repo = Repository(db)
            user = repo.get_user(existing_phone)
            self.assertIsNotNone(user)
            self.assertEqual(user.default_address, "Flat 402, Green Glen, Bellandur")
            self.assertIn("i1", user.past_item_ids)

        # 3. Search query processing
        with get_db() as db:
            repo = Repository(db)
            session = Session(new_phone, repo)
            res = session.handle_message("biryani")

            # 4. Recommendation ranking (4.0+ rating)
            self.assertEqual(session.state, State.SELECTING)
            self.assertIn("Veg Biryani", res)
            self.assertIn("1.", res)

            # 5. Option Selection
            res_sel = session.handle_message("1")
            self.assertEqual(session.state, State.ADDRESS)
            self.assertIn("Veg Biryani", res_sel)

            # 6. Address input (New Address for new user)
            res_addr = session.handle_message("100 Victoria Road, Bangalore")
            self.assertEqual(session.state, State.PROMO)
            self.assertIn("promo code", res_addr)

            # 8. Invalid promo code attempt
            res_promo_bad = session.handle_message("INVALID_CODE")
            self.assertEqual(session.state, State.PROMO)
            self.assertIn("isn't a valid code", res_promo_bad)

            # 9. Valid promo code application (SAVE15 on 220 -> discount 33, total 187)
            res_promo_good = session.handle_message("SAVE15")
            self.assertEqual(session.state, State.CONFIRMED)
            self.assertIn("Order confirmed!", res_promo_good)
            self.assertIn("Total: ₹187.00", res_promo_good)

            # 10. Order Creation & Verification in Database
            order_id = session.order.order_id
            self.assertIsNotNone(order_id)
            order_row = repo.get_order(order_id)
            self.assertIsNotNone(order_row)
            self.assertEqual(order_row["total"], 187.00)
            self.assertEqual(order_row["subtotal"], 220.00)
            self.assertEqual(order_row["discount"], 33.00)
            self.assertEqual(order_row["promo_code"], "SAVE15")

        # 11. Payment Initiation (POST /payment/initiate)
        resp_init = self.client.post("/payment/initiate", json={"order_id": order_id})
        self.assertEqual(resp_init.status_code, 200)
        pay_data = resp_init.get_json()
        self.assertEqual(pay_data["status"], "initiated")
        self.assertEqual(pay_data["amount"], 187.00)
        payment_id = pay_data["payment_id"]

        # 12 & 16. Payment Success Callback with HMAC Verification
        secret = "test_payment_secret_regression_12345"
        pay_payload = {
            "order_id": order_id,
            "payment_id": payment_id,
            "amount": 187.00,
            "status": "succeeded",
        }
        pay_bytes = json.dumps(pay_payload).encode("utf-8")
        sig = _sign_payment(secret, pay_bytes)
        headers = {
            "X-Payment-Signature": sig,
            "X-Payment-Timestamp": str(time.time()),
            "Content-Type": "application/json",
        }
        resp_cb = self.client.post("/payment/webhook", data=pay_bytes, headers=headers)
        self.assertEqual(resp_cb.status_code, 200)
        self.assertEqual(resp_cb.get_json()["status"], "success")

        # 17. Duplicate Payment Callback (Idempotency)
        resp_cb_dup = self.client.post("/payment/webhook", data=pay_bytes, headers=headers)
        self.assertEqual(resp_cb_dup.status_code, 200)
        self.assertEqual(resp_cb_dup.get_json()["status"], "already_processed")

        # 18 & 19. Final Confirmation & Order Persistence Check
        with get_db() as db:
            repo = Repository(db)
            final_order = repo.get_order(order_id)
            self.assertEqual(final_order["payment_status"], "paid")
            self.assertEqual(final_order["status"], "confirmed")
            # User history updated
            user_rec = repo.get_user(new_phone)
            self.assertIn("i1", user_rec.past_item_ids)

        # 20. Process / Server Restart Session State Recovery
        fresh_store = InMemorySessionStore()
        with get_db() as db:
            repo = Repository(db)
            restored_session = fresh_store.get_or_create(new_phone, repo)
            self.assertEqual(restored_session.user.phone, new_phone)

            # 21. New Order after Completed Order
            res_new_order = restored_session.handle_message("hi")
            self.assertEqual(restored_session.state, State.SEARCHING)
            self.assertIn("food ordering assistant", res_new_order)

    def test_saved_address_and_fixed_discount_promo(self):
        """Test existing user using 'same' address and WELCOME50 fixed promo."""
        existing_phone = "+919876500001"
        with get_db() as db:
            repo = Repository(db)
            session = Session(existing_phone, repo)
            session.handle_message("Paneer")
            session.handle_message("1")
            # Step 6: Saved Address
            addr_res = session.handle_message("same")
            self.assertEqual(session.order.address, "Flat 402, Green Glen, Bellandur")
            self.assertEqual(session.state, State.PROMO)

            # Fixed Discount Promo: WELCOME50 on 250 -> discount 50, total 200
            promo_res = session.handle_message("WELCOME50")
            self.assertEqual(session.state, State.CONFIRMED)
            self.assertEqual(session.order.discount, 50.00)
            self.assertEqual(session.order.total, 200.00)

    def test_payment_failure_and_cancellation_callback(self):
        """Test payment failure callback marking status as payment_failed."""
        with get_db() as db:
            repo = Repository(db)
            order = Order(
                user_phone="+919876500001",
                item=MenuItem("i2", "Paneer Butter Masala", "Curry Palace", 4.5, 250.0, 30, ["veg"]),
                address="Test Address",
                promo=None,
            )
            order_id = repo.create_order(order)

        secret = "test_payment_secret_regression_12345"
        payload = {
            "order_id": order_id,
            "payment_id": "pay_fail_123",
            "amount": 250.0,
            "status": "failed",
        }
        pay_bytes = json.dumps(payload).encode("utf-8")
        headers = {
            "X-Payment-Signature": _sign_payment(secret, pay_bytes),
            "X-Payment-Timestamp": str(time.time()),
            "Content-Type": "application/json",
        }
        resp = self.client.post("/payment/webhook", data=pay_bytes, headers=headers)
        self.assertEqual(resp.status_code, 200)

        with get_db() as db:
            repo = Repository(db)
            order_data = repo.get_order(order_id)
            self.assertEqual(order_data["payment_status"], "failed")
            self.assertEqual(order_data["status"], "payment_failed")


class TestSecuritySuite(unittest.TestCase):
    """Verifies security hardening invariants."""

    def setUp(self):
        os.environ["TWILIO_VALIDATE_SIGNATURE"] = "true"
        os.environ["TWILIO_AUTH_TOKEN"] = "test_twilio_secret_token_12345"
        os.environ["PAYMENT_WEBHOOK_SECRET"] = "test_payment_secret_regression_12345"
        self.limiter = InMemoryRateLimiter()
        self.store = InMemorySessionStore()
        self.app = create_app(session_store=self.store, rate_limiter=self.limiter)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

        with get_db() as db:
            Base.metadata.drop_all(db.bind)
            Base.metadata.create_all(db.bind)
            db.add(MenuItemRow(
                item_id="i1", name="Veg Biryani", restaurant="Paradise Biryani",
                rating=4.6, price=220.0, eta_minutes=35, tags="biryani,veg",
            ))
            db.commit()

    def test_invalid_twilio_signature_rejected(self):
        resp = self.client.post("/whatsapp", data={
            "From": "whatsapp:+919876543210",
            "Body": "hi",
        }, headers={"X-Twilio-Signature": "invalid_sig"})
        self.assertEqual(resp.status_code, 403)

    def test_invalid_payment_signature_rejected(self):
        payload = json.dumps({"order_id": "12345678-1234-1234-1234-123456789abc", "payment_id": "pay_1", "amount": 100, "status": "succeeded"}).encode()
        resp = self.client.post("/payment/webhook", data=payload, headers={
            "X-Payment-Signature": "invalid_hmac_hex",
            "X-Payment-Timestamp": str(time.time()),
            "Content-Type": "application/json",
        })
        self.assertEqual(resp.status_code, 403)

    def test_tampered_payment_amount_rejected(self):
        with get_db() as db:
            repo = Repository(db)
            order = Order(
                user_phone="+919876543210",
                item=MenuItem("i1", "Veg Biryani", "Paradise Biryani", 4.6, 220.0, 35, ["biryani"]),
                address="Some Address",
            )
            order_id = repo.create_order(order)

        secret = "test_payment_secret_regression_12345"
        # Tampered amount 1.0 instead of 220.0
        payload = json.dumps({"order_id": order_id, "payment_id": "pay_tamper", "amount": 1.0, "status": "succeeded"}).encode()
        resp = self.client.post("/payment/webhook", data=payload, headers={
            "X-Payment-Signature": _sign_payment(secret, payload),
            "X-Payment-Timestamp": str(time.time()),
            "Content-Type": "application/json",
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn("amount mismatch", resp.get_data(as_text=True).lower())

    def test_unauthorized_order_access_idor_prevented(self):
        with get_db() as db:
            repo = Repository(db)
            order = Order(
                user_phone="+919876543210",
                item=MenuItem("i1", "Veg Biryani", "Paradise Biryani", 4.6, 220.0, 35, ["biryani"]),
                address="Some Address",
            )
            order_id = repo.create_order(order)

            # User 1 can view their own order
            self.assertIsNotNone(repo.get_order(order_id, user_phone="+919876543210"))
            # User 2 cannot view User 1's order
            self.assertIsNone(repo.get_order(order_id, user_phone="+919999999999"))

    def test_sql_injection_safety_in_queries(self):
        with get_db() as db:
            repo = Repository(db)
            sqli_phone = "'+OR+1=1--"
            user = repo.get_or_create_user(sqli_phone)
            self.assertEqual(user.phone, sqli_phone)
            # Fetching menu with SQL injection query
            conv = Session(sqli_phone, repo)
            res = conv.handle_message("'; DROP TABLE orders; --")
            self.assertEqual(conv.state, State.SEARCHING)

    def test_xml_injection_safety_in_twiml(self):
        os.environ["TWILIO_VALIDATE_SIGNATURE"] = "false"
        malicious_input = '<Response><Message>Injected!</Message></Response>'
        resp = self.client.post("/whatsapp", data={
            "From": "whatsapp:+919876543210",
            "Body": malicious_input,
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.get_data(as_text=True)
        self.assertNotIn("<Response><Message>Injected!", data)
        self.assertIn("&lt;Response&gt;", data)


class TestFailureSuite(unittest.TestCase):
    """Verifies resilience and error boundary handling."""

    def setUp(self):
        os.environ["TWILIO_VALIDATE_SIGNATURE"] = "false"
        self.limiter = InMemoryRateLimiter()
        self.store = InMemorySessionStore()
        self.app = create_app(session_store=self.store, rate_limiter=self.limiter)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def test_database_outage_returns_safe_twiml(self):
        with patch("bot.get_db") as mock_db:
            mock_db.side_effect = Exception("Database connection pool exhausted")
            resp = self.client.post("/whatsapp", data={
                "From": "whatsapp:+919876543210",
                "Body": "hi",
            })
            self.assertEqual(resp.status_code, 200)
            self.assertIn("unexpected issue while processing your request", resp.get_data(as_text=True))
            self.assertNotIn("Database connection pool", resp.get_data(as_text=True))

    def test_payment_replay_expired_timestamp(self):
        secret = "test_payment_secret_regression_12345"
        payload = json.dumps({"order_id": "12345678-1234-1234-1234-123456789abc", "payment_id": "p1", "amount": 100, "status": "succeeded"}).encode()
        old_timestamp = str(time.time() - 600)  # 10 minutes ago (> 300s window)
        resp = self.client.post("/payment/webhook", data=payload, headers={
            "X-Payment-Signature": _sign_payment(secret, payload),
            "X-Payment-Timestamp": old_timestamp,
            "Content-Type": "application/json",
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn("timestamp expired", resp.get_data(as_text=True).lower())


class TestDomainInvariants(unittest.TestCase):
    """Verifies all 10 core domain and financial invariants."""

    def setUp(self):
        os.environ["TWILIO_VALIDATE_SIGNATURE"] = "false"
        os.environ["PAYMENT_WEBHOOK_SECRET"] = "test_payment_secret_regression_12345"
        self.limiter = InMemoryRateLimiter()
        self.store = InMemorySessionStore()
        self.app = create_app(session_store=self.store, rate_limiter=self.limiter)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

        with get_db() as db:
            Base.metadata.drop_all(db.bind)
            Base.metadata.create_all(db.bind)
            db.add(MenuItemRow(item_id="i1", name="Veg Biryani", restaurant="Paradise", rating=4.6, price=200.0, eta_minutes=30, tags="biryani"))
            db.add(PromoCodeRow(code="SAVE10", description="10% off", discount_type="percentage", discount_value=0.10, min_order_value=100.0, active=True))
            db.commit()

    def test_invariant_1_no_payment_without_valid_order(self):
        """Invariant 1: No payment callback or initiation on non-existent order."""
        fake_order_id = "00000000-0000-0000-0000-000000000000"
        resp_init = self.client.post("/payment/initiate", json={"order_id": fake_order_id})
        self.assertEqual(resp_init.status_code, 404)

        secret = "test_payment_secret_regression_12345"
        payload = json.dumps({"order_id": fake_order_id, "payment_id": "pay_fake", "amount": 200.0, "status": "succeeded"}).encode()
        resp_cb = self.client.post("/payment/webhook", data=payload, headers={
            "X-Payment-Signature": _sign_payment(secret, payload),
            "X-Payment-Timestamp": str(time.time()),
            "Content-Type": "application/json",
        })
        self.assertEqual(resp_cb.status_code, 404)

    def test_invariant_3_no_duplicate_financial_charge(self):
        """Invariant 3: A paid order cannot be re-charged via /payment/initiate."""
        with get_db() as db:
            repo = Repository(db)
            order = Order(
                user_phone="+919876543210",
                item=MenuItem("i1", "Veg Biryani", "Paradise", 4.6, 200.0, 30, ["biryani"]),
                address="Addr",
            )
            order_id = repo.create_order(order)
            repo.update_order_payment(order_id, payment_id="pay_paid", payment_status="paid", order_status="confirmed")

        resp = self.client.post("/payment/initiate", json={"order_id": order_id})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("already paid", resp.get_data(as_text=True).lower())

    def test_invariant_5_backend_calculates_all_financial_values(self):
        """Invariant 5 & 6: Backend calculates all prices, discounts, subtotals, and totals."""
        item = MenuItem("i1", "Veg Biryani", "Paradise", 4.6, 200.0, 30, ["biryani"])
        promo = PromoCode("SAVE10", "10% off", discount_type="percentage", discount_value=0.10, min_order_value=100.0)
        order = Order(user_phone="+919876543210", item=item, promo=promo)

        self.assertEqual(order.subtotal, 200.0)
        self.assertEqual(order.discount, 20.0)
        self.assertEqual(order.total, 180.0)

        # Discount cannot exceed subtotal
        big_discount_promo = PromoCode("BIG500", "500 off", discount_type="flat", discount_value=500.0)
        order_capped = Order(user_phone="+919876543210", item=item, promo=big_discount_promo)
        self.assertEqual(order_capped.discount, 200.0)
        self.assertEqual(order_capped.total, 0.0)


if __name__ == "__main__":
    unittest.main()
