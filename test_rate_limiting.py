"""Rate Limiting and Abuse Protection Test Suite.

Covers:
- Normal traffic within limits
- Repeated requests (IP & User thresholds)
- Burst traffic sliding window behavior
- Multi-user rate limit isolation
- Promo code brute-force protection
- Payment initiation limits & retry protection
- Preventing re-charging of already paid orders
- Duplicate callback idempotency under load
- RedisRateLimiter and graceful in-memory fallback
"""
import hashlib
import hmac
import json
import os
import time
import unittest
from unittest.mock import MagicMock

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["TWILIO_AUTH_TOKEN"] = ""
os.environ["PAYMENT_WEBHOOK_SECRET"] = "test_payment_secret_123"
os.environ["TWILIO_VALIDATE_SIGNATURE"] = "false"
os.environ["WEBHOOK_RATE_LIMIT"] = "10"
os.environ["USER_MESSAGE_RATE_LIMIT"] = "5"
os.environ["PAYMENT_WEBHOOK_RATE_LIMIT"] = "10"
os.environ["PAYMENT_INITIATION_LIMIT"] = "3"
os.environ["PROMO_ATTEMPT_LIMIT"] = "3"

from bot import create_app
from conversation import Session, State
from database import Base, get_db
from db_models import UserRow, MenuItemRow, PromoCodeRow, OrderRow  # noqa: F401
from models import Order, MenuItem, PromoCode, User
from rate_limiter import InMemoryRateLimiter, RedisRateLimiter, get_rate_limiter
from repository import Repository
from session_store import InMemorySessionStore


def _sign_payment(secret: str, payload_bytes: bytes) -> str:
    """Generate HMAC-SHA256 signature for payment callbacks."""
    return hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()


class TestRateLimiterCore(unittest.TestCase):
    """Unit tests for InMemoryRateLimiter and RedisRateLimiter sliding window mechanics."""

    def test_in_memory_sliding_window_allows_within_limit(self):
        limiter = InMemoryRateLimiter()
        key = "test_user_1"
        for _ in range(5):
            allowed, retry_after = limiter.is_allowed(key, max_requests=5, window_seconds=60)
            self.assertTrue(allowed)
            self.assertEqual(retry_after, 0)

    def test_in_memory_sliding_window_blocks_over_limit(self):
        limiter = InMemoryRateLimiter()
        key = "test_user_burst"
        for _ in range(5):
            allowed, _ = limiter.is_allowed(key, max_requests=5, window_seconds=60)
            self.assertTrue(allowed)

        # 6th request blocked
        allowed, retry_after = limiter.is_allowed(key, max_requests=5, window_seconds=60)
        self.assertFalse(allowed)
        self.assertGreater(retry_after, 0)

    def test_in_memory_reset_and_clear(self):
        limiter = InMemoryRateLimiter()
        key = "test_reset"
        for _ in range(3):
            limiter.is_allowed(key, max_requests=3, window_seconds=60)

        self.assertFalse(limiter.is_allowed(key, max_requests=3, window_seconds=60)[0])
        limiter.reset(key)
        self.assertTrue(limiter.is_allowed(key, max_requests=3, window_seconds=60)[0])

    def test_redis_rate_limiter_normal_flow(self):
        mock_redis = MagicMock()
        pipeline_mock = MagicMock()
        pipeline_mock.execute.return_value = [0, 1, 3, True, [(b"12345", 12345.0)]]
        mock_redis.pipeline.return_value = pipeline_mock

        limiter = RedisRateLimiter(mock_redis)
        allowed, retry = limiter.is_allowed("test_redis_key", max_requests=5, window_seconds=60)
        self.assertTrue(allowed)
        self.assertEqual(retry, 0)

    def test_redis_rate_limiter_exceeded(self):
        mock_redis = MagicMock()
        now = time.time()
        pipeline_mock = MagicMock()
        pipeline_mock.execute.return_value = [0, 1, 6, True, [(str(now - 10).encode(), now - 10)]]
        mock_redis.pipeline.return_value = pipeline_mock

        limiter = RedisRateLimiter(mock_redis)
        allowed, retry = limiter.is_allowed("test_redis_exceeded", max_requests=5, window_seconds=60)
        self.assertFalse(allowed)
        self.assertGreater(retry, 0)
        self.assertTrue(mock_redis.zrem.called)

    def test_redis_rate_limiter_exception_graceful_fallback(self):
        mock_redis = MagicMock()
        mock_redis.pipeline.side_effect = ConnectionError("Redis server unavailable")

        limiter = RedisRateLimiter(mock_redis)
        allowed, retry = limiter.is_allowed("fallback_test", max_requests=2, window_seconds=60)
        self.assertTrue(allowed)

        allowed2, _ = limiter.is_allowed("fallback_test", max_requests=2, window_seconds=60)
        self.assertTrue(allowed2)

        allowed3, _ = limiter.is_allowed("fallback_test", max_requests=2, window_seconds=60)
        self.assertFalse(allowed3)


class TestWhatsAppRateLimiting(unittest.TestCase):
    """Integration tests for WhatsApp Webhook Rate Limiting and Multi-User isolation."""

    def setUp(self):
        os.environ["TWILIO_VALIDATE_SIGNATURE"] = "false"
        os.environ["TWILIO_AUTH_TOKEN"] = ""
        os.environ["PAYMENT_WEBHOOK_SECRET"] = "test_payment_secret_123"
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
            db.add(PromoCodeRow(
                code="SAVE15", description="15% off above ₹200",
                discount_type="percentage", discount_value=0.15, min_order_value=200.0, active=True,
            ))
            db.commit()

    def test_normal_traffic_within_limits(self):
        for i in range(3):
            resp = self.client.post("/whatsapp", data={
                "From": "whatsapp:+919876543210",
                "Body": f"hi {i}",
            })
            self.assertEqual(resp.status_code, 200)
            self.assertIn("<Response>", resp.get_data(as_text=True))

    def test_user_rate_limit_exceeded_returns_polite_twiml(self):
        phone = "whatsapp:+919876543210"
        for _ in range(5):
            resp = self.client.post("/whatsapp", data={"From": phone, "Body": "hi"})
            self.assertEqual(resp.status_code, 200)
            self.assertNotIn("You're sending messages too fast", resp.get_data(as_text=True))

        resp = self.client.post("/whatsapp", data={"From": phone, "Body": "hi"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("You're sending messages too fast. Please wait a moment before replying.", resp.get_data(as_text=True))

    def test_multi_user_rate_limit_isolation(self):
        user_a = "whatsapp:+919876543210"
        user_b = "whatsapp:+919876543211"

        for _ in range(5):
            self.client.post("/whatsapp", data={"From": user_a, "Body": "hi"})

        resp_a = self.client.post("/whatsapp", data={"From": user_a, "Body": "hi"})
        self.assertIn("You're sending messages too fast", resp_a.get_data(as_text=True))

        resp_b = self.client.post("/whatsapp", data={"From": user_b, "Body": "hi"})
        self.assertEqual(resp_b.status_code, 200)
        self.assertNotIn("You're sending messages too fast", resp_b.get_data(as_text=True))
        self.assertIn("Hi! I'm your food ordering assistant", resp_b.get_data(as_text=True))

    def test_ip_level_flood_protection_returns_429(self):
        os.environ["WEBHOOK_RATE_LIMIT"] = "4"
        try:
            for i in range(4):
                resp = self.client.post("/whatsapp", data={
                    "From": f"whatsapp:+91987654320{i}",
                    "Body": "hi",
                })
                self.assertEqual(resp.status_code, 200)

            resp = self.client.post("/whatsapp", data={
                "From": "whatsapp:+919876543299",
                "Body": "hi",
            })
            self.assertEqual(resp.status_code, 429)
            self.assertEqual(resp.get_data(as_text=True), "Too Many Requests")
            self.assertIn("Retry-After", resp.headers)
        finally:
            os.environ["WEBHOOK_RATE_LIMIT"] = "10"


class TestPromoCodeBruteForceProtection(unittest.TestCase):
    """Tests for promo code attempt limits in the conversation state machine."""

    def setUp(self):
        with get_db() as db:
            Base.metadata.drop_all(db.bind)
            Base.metadata.create_all(db.bind)
            db.add(MenuItemRow(
                item_id="i1", name="Veg Biryani", restaurant="Paradise Biryani",
                rating=4.6, price=220.0, eta_minutes=35, tags="biryani,veg",
            ))
            db.add(PromoCodeRow(
                code="SAVE15", description="15% off above ₹200",
                discount_type="percentage", discount_value=0.15, min_order_value=200.0, active=True,
            ))
            db.commit()

    def test_promo_attempt_limit_stops_brute_force(self):
        with get_db() as db:
            repo = Repository(db)
            conv = Session("+919876543210", repo=repo)

            conv.handle_message("biryani")  # SELECTING
            conv.handle_message("1")        # ADDRESS
            conv.handle_message("123 Main St") # PROMO
            self.assertEqual(conv.state, State.PROMO)

            # Attempt 1: invalid
            r1 = conv.handle_message("FAKE1")
            self.assertIn("isn't a valid code. Try again", r1)
            self.assertEqual(conv.state, State.PROMO)

            # Attempt 2: invalid
            r2 = conv.handle_message("FAKE2")
            self.assertIn("isn't a valid code. Try again", r2)
            self.assertEqual(conv.state, State.PROMO)

            # Attempt 3: invalid -> limit reached (PROMO_ATTEMPT_LIMIT=3)
            r3 = conv.handle_message("FAKE3")
            self.assertIn("Maximum attempts reached. Please reply 'skip' to continue.", r3)
            self.assertEqual(conv.state, State.PROMO)

            # Attempt 4: locked out
            r4 = conv.handle_message("SAVE15")
            self.assertIn("Too many invalid promo attempts. Please reply 'skip'", r4)
            self.assertEqual(conv.state, State.PROMO)

            # Skip succeeds and confirms order
            r5 = conv.handle_message("skip")
            self.assertEqual(conv.state, State.CONFIRMED)
            self.assertIn("Order confirmed!", r5)


class TestPaymentProtectionAndAbuse(unittest.TestCase):
    """Tests for payment initiation limits, payment retry protection, and duplicate callbacks."""

    def setUp(self):
        os.environ["TWILIO_VALIDATE_SIGNATURE"] = "false"
        os.environ["PAYMENT_WEBHOOK_SECRET"] = "test_payment_secret_123"
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

    def _seed_order(self, repo: Repository, phone: str = "+919876543210", total: float = 250.0) -> str:
        user = repo.get_or_create_user(phone)
        order = Order(
            user_phone=phone,
            item=MenuItem(
                item_id="item-1",
                name="Paneer Butter Masala",
                restaurant="Curry Palace",
                rating=4.5,
                price=total,
                eta_minutes=30,
                tags=["veg", "curry"],
            ),
            address="456 Park Avenue",
            promo=None,
        )
        return repo.create_order(order)

    def test_payment_initiation_success(self):
        with get_db() as db:
            repo = Repository(db)
            order_id = self._seed_order(repo)

        resp = self.client.post("/payment/initiate", json={"order_id": order_id})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["status"], "initiated")
        self.assertEqual(data["order_id"], order_id)
        self.assertIn("payment_id", data)
        self.assertEqual(data["amount"], 250.0)

    def test_payment_initiation_retry_limit_exceeded(self):
        with get_db() as db:
            repo = Repository(db)
            order_id = self._seed_order(repo)

        # PAYMENT_INITIATION_LIMIT is 3
        for _ in range(3):
            resp = self.client.post("/payment/initiate", json={"order_id": order_id})
            self.assertEqual(resp.status_code, 200)

        # 4th initiation attempt hits limit
        resp = self.client.post("/payment/initiate", json={"order_id": order_id})
        self.assertEqual(resp.status_code, 429)
        data = resp.get_json()
        self.assertIn("Payment initiation limit exceeded", data["error"])

    def test_prevent_recharging_already_paid_order(self):
        with get_db() as db:
            repo = Repository(db)
            order_id = self._seed_order(repo)
            repo.update_order_payment(order_id, payment_id="pay_init_1", payment_status="paid", order_status="confirmed")

        resp = self.client.post("/payment/initiate", json={"order_id": order_id})
        self.assertEqual(resp.status_code, 400)
        data = resp.get_json()
        self.assertIn("Order already paid", data["error"])

    def test_duplicate_payment_callbacks_remain_idempotent(self):
        with get_db() as db:
            repo = Repository(db)
            order_id = self._seed_order(repo, total=250.0)

        secret = "test_payment_secret_123"
        payload = {
            "order_id": order_id,
            "payment_id": "pay_txn_999",
            "amount": 250.0,
            "status": "succeeded",
        }
        payload_bytes = json.dumps(payload).encode("utf-8")
        sig = _sign_payment(secret, payload_bytes)
        now_ts = str(time.time())

        headers = {
            "X-Payment-Signature": sig,
            "X-Payment-Timestamp": now_ts,
            "Content-Type": "application/json",
        }

        # First callback: processed successfully
        resp1 = self.client.post("/payment/webhook", data=payload_bytes, headers=headers)
        self.assertEqual(resp1.status_code, 200)
        self.assertEqual(resp1.get_json()["status"], "success")

        # Second (duplicate) callback: idempotent 200 with already_processed
        resp2 = self.client.post("/payment/webhook", data=payload_bytes, headers=headers)
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp2.get_json()["status"], "already_processed")
        self.assertEqual(resp2.get_json()["order_id"], order_id)

        # Third duplicate callback: still safely idempotent
        resp3 = self.client.post("/payment/webhook", data=payload_bytes, headers=headers)
        self.assertEqual(resp3.status_code, 200)
        self.assertEqual(resp3.get_json()["status"], "already_processed")


if __name__ == "__main__":
    unittest.main()
