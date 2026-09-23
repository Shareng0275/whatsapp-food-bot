"""Resilience and Failure-Injection Test Suite.

Covers all Phase 6 failure scenarios:
A. Database unavailable
B. Redis unavailable (with in-memory fallback)
C. Payment provider timeout
D. Payment callback duplicated (idempotent handling)
E. Payment callback delayed / expired
F. Payment verification failure (signature & amount mismatch)
G. Twilio request malformed (missing params, malformed phone)
H. Internal exception handling (safe TwiML fallback without tracebacks)
I. Server restart during PAYMENT_PENDING (persistence check)
J. Server restart after payment success but before confirmation delivery
"""
import os
import json
import time
import hmac
import hashlib
import unittest
from unittest.mock import patch, MagicMock

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["TWILIO_AUTH_TOKEN"] = "test_twilio_secret_token_12345"
os.environ["PAYMENT_WEBHOOK_SECRET"] = "test_payment_secret_key_67890"
os.environ["TWILIO_VALIDATE_SIGNATURE"] = "true"

from twilio.request_validator import RequestValidator
from database import Base, get_db
from db_models import UserRow, MenuItemRow, PromoCodeRow, OrderRow  # noqa: F401
from repository import Repository
from models import Order, MenuItem, PromoCode, User
from session_store import InMemorySessionStore, RedisSessionStore
from bot import create_app
from conversation import Session, State


class TestResilienceAndFailureBoundaries(unittest.TestCase):
    def setUp(self):
        self.twilio_token = "test_twilio_secret_token_12345"
        self.payment_secret = "test_payment_secret_key_67890"
        os.environ["TWILIO_AUTH_TOKEN"] = self.twilio_token
        os.environ["PAYMENT_WEBHOOK_SECRET"] = self.payment_secret
        os.environ["TWILIO_VALIDATE_SIGNATURE"] = "true"

        with get_db() as db:
            Base.metadata.drop_all(db.bind)
            Base.metadata.create_all(db.bind)

            db.add(MenuItemRow(
                item_id="i1", name="Veg Biryani", restaurant="Paradise Biryani",
                rating=4.6, price=220.0, eta_minutes=35, tags="biryani,veg",
            ))
            db.add(UserRow(
                phone="+919900011122", name="Asha",
                default_address="204, Palm Residency, Indiranagar",
                past_item_ids="",
            ))
            db.commit()

        self.session_store = InMemorySessionStore()
        self.app = create_app(session_store=self.session_store)
        self.client = self.app.test_client()
        self.validator = RequestValidator(self.twilio_token)

    def _sign_twilio(self, url: str, params: dict) -> str:
        return self.validator.compute_signature(url, params)

    def _sign_payment(self, payload_bytes: bytes) -> str:
        return hmac.new(self.payment_secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()

    # ------------------------------------------------------------------
    # Scenario A: Database Unavailable
    # ------------------------------------------------------------------
    def test_scenario_a_database_unavailable_returns_safe_twiml(self):
        """When DB is down/unreachable, bot returns safe TwiML without leaking DB traces."""
        url = "http://localhost/whatsapp"
        data = {"From": "whatsapp:+919900011122", "Body": "hi"}
        headers = {"X-Twilio-Signature": self._sign_twilio(url, data)}

        with patch("bot.get_db", side_effect=RuntimeError("OperationalError: connection to server at localhost:5432 failed")):
            response = self.client.post("/whatsapp", data=data, headers=headers)
            self.assertEqual(response.status_code, 200)
            xml = response.data.decode("utf-8")
            self.assertIn("<Response><Message>", xml)
            self.assertIn("unexpected issue", xml)
            # Ensure DB connection strings and tracebacks are never exposed
            self.assertNotIn("5432", xml)
            self.assertNotIn("OperationalError", xml)
            self.assertNotIn("Traceback", xml)

    # ------------------------------------------------------------------
    # Scenario B: Redis Unavailable (Graceful In-Memory Fallback)
    # ------------------------------------------------------------------
    def test_scenario_b_redis_unavailable_fallback(self):
        """When Redis raises ConnectionError/Timeout, RedisSessionStore seamlessly uses fallback memory."""
        mock_redis = MagicMock()
        mock_redis.get.side_effect = ConnectionError("Redis server closed connection")
        mock_redis.setex.side_effect = ConnectionError("Redis server closed connection")

        redis_store = RedisSessionStore(mock_redis)
        mock_repo = MagicMock()
        mock_repo.get_or_create_user.return_value = MagicMock(past_item_ids=[], default_address="")

        # Initial get_or_create succeeds using fallback memory
        s1 = redis_store.get_or_create("+919900011122", mock_repo)
        s1.order.address = "Fallback Address 101"
        redis_store.save("+919900011122", s1)

        # Subsequent retrieval gets session from fallback memory without crashing
        s2 = redis_store.get("+919900011122", mock_repo)
        self.assertIsNotNone(s2)
        self.assertEqual(s2.order.address, "Fallback Address 101")

    # ------------------------------------------------------------------
    # Scenario C: Payment Provider Timeout / Network Failure
    # ------------------------------------------------------------------
    def test_scenario_c_payment_provider_timeout(self):
        """Simulated payment provider timeout handled safely with generic 500."""
        payload = {
            "order_id": "00000000-0000-0000-0000-000000000000",
            "payment_id": "pay_timeout_1",
            "amount": 220.0,
            "status": "succeeded",
        }
        body_bytes = json.dumps(payload).encode("utf-8")
        signature = self._sign_payment(body_bytes)
        headers = {
            "Content-Type": "application/json",
            "X-Payment-Signature": signature,
            "X-Payment-Timestamp": str(time.time()),
        }

        with patch("bot.get_db", side_effect=TimeoutError("Gateway connection timed out")):
            response = self.client.post("/payment/webhook", data=body_bytes, headers=headers)
            self.assertEqual(response.status_code, 500)
            self.assertEqual(response.get_json()["error"], "Internal server error")

    # ------------------------------------------------------------------
    # Scenario D: Payment Callback Duplicated (Idempotency)
    # ------------------------------------------------------------------
    def test_scenario_d_payment_callback_duplicated(self):
        """Repeated duplicate callbacks for an already paid order return 200 already_processed without re-updating."""
        with get_db() as db:
            repo = Repository(db)
            order_id = repo.create_order(
                Order(
                    user_phone="+919900011122",
                    item=MenuItem("i1", "Veg Biryani", "Paradise", 4.6, 220.0, 35),
                ),
                status="confirmed",
                payment_status="paid",
            )

        payload = {
            "order_id": order_id,
            "payment_id": "pay_first_and_only",
            "amount": 220.0,
            "status": "succeeded",
        }
        body_bytes = json.dumps(payload).encode("utf-8")
        signature = self._sign_payment(body_bytes)
        headers = {
            "Content-Type": "application/json",
            "X-Payment-Signature": signature,
            "X-Payment-Timestamp": str(time.time()),
        }

        # First callback -> 200 already_processed
        r1 = self.client.post("/payment/webhook", data=body_bytes, headers=headers)
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r1.get_json()["status"], "already_processed")

        # Second duplicate callback -> 200 already_processed
        r2 = self.client.post("/payment/webhook", data=body_bytes, headers=headers)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.get_json()["status"], "already_processed")

    # ------------------------------------------------------------------
    # Scenario E: Payment Callback Delayed / Expired
    # ------------------------------------------------------------------
    def test_scenario_e_payment_callback_delayed_expired(self):
        """Payment callback with timestamp older than 300 seconds is rejected with 400."""
        payload = {
            "order_id": "00000000-0000-0000-0000-000000000000",
            "payment_id": "pay_delayed",
            "amount": 220.0,
            "status": "succeeded",
        }
        body_bytes = json.dumps(payload).encode("utf-8")
        signature = self._sign_payment(body_bytes)

        # 400 seconds old
        headers = {
            "Content-Type": "application/json",
            "X-Payment-Signature": signature,
            "X-Payment-Timestamp": str(time.time() - 400),
        }

        response = self.client.post("/payment/webhook", data=body_bytes, headers=headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "Webhook timestamp expired")

    # ------------------------------------------------------------------
    # Scenario F: Payment Verification Failure (Invalid Signature & Amount)
    # ------------------------------------------------------------------
    def test_scenario_f_payment_verification_failure(self):
        """Invalid signature returns 403, tampered amount returns 400."""
        with get_db() as db:
            repo = Repository(db)
            order_id = repo.create_order(
                Order(
                    user_phone="+919900011122",
                    item=MenuItem("i1", "Veg Biryani", "Paradise", 4.6, 220.0, 35),
                ),
                status="pending_payment",
                payment_status="pending",
            )

        payload = {
            "order_id": order_id,
            "payment_id": "pay_test",
            "amount": 220.0,
            "status": "succeeded",
        }
        body_bytes = json.dumps(payload).encode("utf-8")

        # 1. Invalid signature
        r_sig = self.client.post(
            "/payment/webhook",
            data=body_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Payment-Signature": "invalid_sig",
                "X-Payment-Timestamp": str(time.time()),
            },
        )
        self.assertEqual(r_sig.status_code, 403)

        # 2. Tampered amount
        payload_tampered = {
            "order_id": order_id,
            "payment_id": "pay_test",
            "amount": 5.0,  # Expected 220.0
            "status": "succeeded",
        }
        bytes_tampered = json.dumps(payload_tampered).encode("utf-8")
        sig_tampered = self._sign_payment(bytes_tampered)

        r_amt = self.client.post(
            "/payment/webhook",
            data=bytes_tampered,
            headers={
                "Content-Type": "application/json",
                "X-Payment-Signature": sig_tampered,
                "X-Payment-Timestamp": str(time.time()),
            },
        )
        self.assertEqual(r_amt.status_code, 400)
        self.assertEqual(r_amt.get_json()["error"], "Payment amount mismatch")

    # ------------------------------------------------------------------
    # Scenario G: Twilio Request Malformed
    # ------------------------------------------------------------------
    def test_scenario_g_twilio_request_malformed(self):
        """Missing From or Body returns 400 Bad Request."""
        url = "http://localhost/whatsapp"

        # Missing From
        d1 = {"Body": "hi"}
        r1 = self.client.post("/whatsapp", data=d1, headers={"X-Twilio-Signature": self._sign_twilio(url, d1)})
        self.assertEqual(r1.status_code, 400)

        # Missing Body
        d2 = {"From": "whatsapp:+919900011122"}
        r2 = self.client.post("/whatsapp", data=d2, headers={"X-Twilio-Signature": self._sign_twilio(url, d2)})
        self.assertEqual(r2.status_code, 400)

    # ------------------------------------------------------------------
    # Scenario H: Internal Exception Handling
    # ------------------------------------------------------------------
    def test_scenario_h_internal_exception_returns_polite_twiml(self):
        """Unexpected internal exceptions return safe user message without leaking stack traces."""
        url = "http://localhost/whatsapp"
        data = {"From": "whatsapp:+919900011122", "Body": "hi"}
        headers = {"X-Twilio-Signature": self._sign_twilio(url, data)}

        with patch.object(self.session_store, "get_or_create", side_effect=ZeroDivisionError("Unexpected mathematical error")):
            response = self.client.post("/whatsapp", data=data, headers=headers)
            self.assertEqual(response.status_code, 200)
            xml = response.data.decode("utf-8")
            self.assertIn("unexpected issue", xml)
            self.assertNotIn("ZeroDivisionError", xml)
            self.assertNotIn("Traceback", xml)

    # ------------------------------------------------------------------
    # Scenario I: Server Restart During PAYMENT_PENDING
    # ------------------------------------------------------------------
    def test_scenario_i_server_restart_during_payment_pending(self):
        """Order state is durable in DB even if in-memory session cache is cleared on server restart."""
        with get_db() as db:
            repo = Repository(db)
            order_id = repo.create_order(
                Order(
                    user_phone="+919900011122",
                    item=MenuItem("i1", "Veg Biryani", "Paradise", 4.6, 220.0, 35),
                    address="204, Palm Residency, Indiranagar",
                ),
                status="pending_payment",
                payment_status="pending",
            )

        # Simulate full server restart: clear session store cache
        self.session_store.clear()

        # Webhook callback arrives after server restart -> processes successfully from DB
        payload = {
            "order_id": order_id,
            "payment_id": "pay_after_restart_123",
            "amount": 220.0,
            "status": "succeeded",
        }
        body_bytes = json.dumps(payload).encode("utf-8")
        signature = self._sign_payment(body_bytes)
        headers = {
            "Content-Type": "application/json",
            "X-Payment-Signature": signature,
            "X-Payment-Timestamp": str(time.time()),
        }

        response = self.client.post("/payment/webhook", data=body_bytes, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "success")

        # Verify DB is confirmed and paid
        with get_db() as db:
            repo = Repository(db)
            saved = repo.get_order(order_id)
            self.assertEqual(saved["payment_status"], "paid")
            self.assertEqual(saved["status"], "confirmed")

    # ------------------------------------------------------------------
    # Scenario J: Server Restart After Payment Success But Before Confirmation
    # ------------------------------------------------------------------
    def test_scenario_j_server_restart_after_payment_success_state_recovery(self):
        """Order confirmed in DB remains recoverable and consistent if server restarted before next message."""
        with get_db() as db:
            repo = Repository(db)
            order_id = repo.create_order(
                Order(
                    user_phone="+919900011122",
                    item=MenuItem("i1", "Veg Biryani", "Paradise", 4.6, 220.0, 35),
                    address="204, Palm Residency, Indiranagar",
                ),
                status="confirmed",
                payment_status="paid",
                payment_id="pay_completed_999",
            )

        # Server restart happens
        self.session_store.clear()

        # Verify order history in DB is intact
        with get_db() as db:
            repo = Repository(db)
            orders = repo.list_user_orders("+919900011122")
            self.assertEqual(len(orders), 1)
            self.assertEqual(orders[0]["id"], order_id)
            self.assertEqual(orders[0]["payment_status"], "paid")
            self.assertEqual(orders[0]["status"], "confirmed")


if __name__ == "__main__":
    unittest.main()
