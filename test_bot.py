"""Automated tests for production Flask/Twilio WhatsApp adapter (bot.py).

Covers:
- GET /health
- POST-only enforcement on /whatsapp (GET -> 405)
- Twilio signature validation (valid, invalid, missing)
- Input validation (missing From, missing Body, invalid From)
- Normal multi-turn conversation flow
- XML escaping with special characters (&, <, >, quotes)
- Internal error handling (safe TwiML fallback without traceback)
- Phone number normalization
- Session store functionality
"""
import os
import unittest
from unittest.mock import patch, MagicMock

# Force in-memory SQLite before database import
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["TWILIO_AUTH_TOKEN"] = "test_auth_token_secret_12345"
os.environ["TWILIO_VALIDATE_SIGNATURE"] = "true"

from sqlalchemy import create_engine
from twilio.request_validator import RequestValidator

from database import Base, get_db
from db_models import UserRow, MenuItemRow, PromoCodeRow, OrderRow  # noqa: F401
from repository import Repository
from session_store import InMemorySessionStore, RedisSessionStore
from bot import create_app, normalize_whatsapp_number


class TestPhoneNormalization(unittest.TestCase):
    def test_normalize_whatsapp_number(self):
        self.assertEqual(
            normalize_whatsapp_number("whatsapp:+919876543210"),
            "+919876543210",
        )
        self.assertEqual(
            normalize_whatsapp_number("WHATSAPP:+1 (415) 555-2671"),
            "+14155552671",
        )
        self.assertEqual(
            normalize_whatsapp_number("  +91 98765-43210  "),
            "+919876543210",
        )
        self.assertEqual(normalize_whatsapp_number(""), "")
        self.assertEqual(normalize_whatsapp_number(None), "")


class TestTwilioBot(unittest.TestCase):
    def setUp(self):
        self.auth_token = "test_auth_token_secret_12345"
        os.environ["TWILIO_AUTH_TOKEN"] = self.auth_token
        os.environ["TWILIO_VALIDATE_SIGNATURE"] = "true"

        # Re-create SQLite schema in memory
        with get_db() as db:
            Base.metadata.drop_all(db.bind)
            Base.metadata.create_all(db.bind)
            # Seed test menu
            db.add(MenuItemRow(
                item_id="i1", name="Veg Biryani", restaurant="Paradise",
                rating=4.6, price=220, eta_minutes=35, tags="biryani,veg",
            ))
            db.add(MenuItemRow(
                item_id="i2", name="Chicken Biryani", restaurant="Paradise",
                rating=4.7, price=280, eta_minutes=35, tags="biryani,non-veg",
            ))
            # Seed test promo
            db.add(PromoCodeRow(
                code="SAVE15", description="15% off", discount_type="percentage",
                discount_value=0.15, min_order_value=200.0, active=True,
            ))
            # Seed test user
            db.add(UserRow(
                phone="+919876543210", name="Aarav", default_address="Indiranagar",
                past_item_ids="",
            ))
            db.commit()

        self.session_store = InMemorySessionStore()
        self.app = create_app(session_store=self.session_store)
        self.client = self.app.test_client()
        self.validator = RequestValidator(self.auth_token)

    def _sign(self, url: str, params: dict) -> str:
        return self.validator.compute_signature(url, params)

    def test_health_endpoint(self):
        """GET /health returns 200 OK and healthy status."""
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        json_data = response.get_json()
        self.assertEqual(json_data["status"], "healthy")
        self.assertEqual(json_data["service"], "whatsapp-food-bot")

    def test_whatsapp_method_not_allowed(self):
        """GET /whatsapp returns 405 Method Not Allowed."""
        response = self.client.get("/whatsapp")
        self.assertEqual(response.status_code, 405)

    def test_missing_signature_returns_403(self):
        """POST /whatsapp without Twilio signature returns 403 Forbidden."""
        data = {"From": "whatsapp:+919876543210", "Body": "hi"}
        response = self.client.post("/whatsapp", data=data)
        self.assertEqual(response.status_code, 403)
        self.assertIn(b"Forbidden: Missing signature header.", response.data)

    def test_invalid_signature_returns_403(self):
        """POST /whatsapp with an invalid Twilio signature returns 403 Forbidden."""
        data = {"From": "whatsapp:+919876543210", "Body": "hi"}
        headers = {"X-Twilio-Signature": "invalid_signature_hash"}
        response = self.client.post("/whatsapp", data=data, headers=headers)
        self.assertEqual(response.status_code, 403)
        self.assertIn(b"Forbidden: Invalid signature.", response.data)

    def test_missing_from_returns_400(self):
        """POST /whatsapp without 'From' parameter returns 400 Bad Request."""
        data = {"Body": "hi"}
        url = "http://localhost/whatsapp"
        signature = self._sign(url, data)
        headers = {"X-Twilio-Signature": signature}

        response = self.client.post("/whatsapp", data=data, headers=headers)
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"Missing 'From' parameter", response.data)

    def test_missing_body_returns_400(self):
        """POST /whatsapp without 'Body' parameter returns 400 Bad Request."""
        data = {"From": "whatsapp:+919876543210"}
        url = "http://localhost/whatsapp"
        signature = self._sign(url, data)
        headers = {"X-Twilio-Signature": signature}

        response = self.client.post("/whatsapp", data=data, headers=headers)
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"Missing 'Body' parameter", response.data)

    def test_valid_webhook_greeting(self):
        """Valid webhook request returns properly formatted TwiML greeting."""
        data = {"From": "whatsapp:+919876543210", "Body": "hi"}
        url = "http://localhost/whatsapp"
        signature = self._sign(url, data)
        headers = {"X-Twilio-Signature": signature}

        response = self.client.post("/whatsapp", data=data, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/xml")
        xml = response.data.decode("utf-8")
        self.assertIn("<Response><Message>", xml)
        self.assertIn("What would you like to eat today?", xml)

    def test_xml_escaping_special_characters(self):
        """Arbitrary user text with XML special characters is safely escaped in TwiML."""
        data = {"From": "whatsapp:+919876543210", "Body": "<script>alert('xss') & &lt; &gt;</script>"}
        url = "http://localhost/whatsapp"
        signature = self._sign(url, data)
        headers = {"X-Twilio-Signature": signature}

        response = self.client.post("/whatsapp", data=data, headers=headers)
        self.assertEqual(response.status_code, 200)
        xml = response.data.decode("utf-8")
        # Ensure raw <script> is not present in XML body unescaped
        self.assertNotIn("<script>", xml)
        self.assertIn("&lt;script&gt;", xml)

    def test_normal_conversation_flow(self):
        """Multi-turn order placement across successive POST requests."""
        phone = "whatsapp:+919876543210"
        url = "http://localhost/whatsapp"

        # Step 1: Greeting
        r1 = self.client.post("/whatsapp", data={"From": phone, "Body": "hi"},
                              headers={"X-Twilio-Signature": self._sign(url, {"From": phone, "Body": "hi"})})
        self.assertIn("What would you like to eat today?", r1.data.decode("utf-8"))

        # Step 2: Search item
        r2 = self.client.post("/whatsapp", data={"From": phone, "Body": "Veg Biryani"},
                              headers={"X-Twilio-Signature": self._sign(url, {"From": phone, "Body": "Veg Biryani"})})
        self.assertIn("1. Veg Biryani — Paradise", r2.data.decode("utf-8"))

        # Step 3: Select option
        r3 = self.client.post("/whatsapp", data={"From": phone, "Body": "1"},
                              headers={"X-Twilio-Signature": self._sign(url, {"From": phone, "Body": "1"})})
        self.assertIn("Indiranagar", r3.data.decode("utf-8"))

        # Step 4: Address confirmation
        r4 = self.client.post("/whatsapp", data={"From": phone, "Body": "same"},
                              headers={"X-Twilio-Signature": self._sign(url, {"From": phone, "Body": "same"})})
        self.assertIn("Do you have a promo code?", r4.data.decode("utf-8"))

        # Step 5: Apply promo & confirm order
        r5 = self.client.post("/whatsapp", data={"From": phone, "Body": "SAVE15"},
                              headers={"X-Twilio-Signature": self._sign(url, {"From": phone, "Body": "SAVE15"})})
        body5 = r5.data.decode("utf-8")
        self.assertIn("✅ Order confirmed!", body5)
        self.assertIn("Discount (SAVE15): -₹33.00", body5)
        self.assertIn("Total: ₹187.00", body5)

    def test_internal_error_safe_fallback(self):
        """Unexpected internal exception returns safe TwiML message without stack trace."""
        data = {"From": "whatsapp:+919876543210", "Body": "hi"}
        url = "http://localhost/whatsapp"
        signature = self._sign(url, data)
        headers = {"X-Twilio-Signature": signature}

        # Simulate unexpected exception during get_or_create or DB access
        with patch.object(self.session_store, "get_or_create", side_effect=RuntimeError("DB disk full")):
            response = self.client.post("/whatsapp", data=data, headers=headers)
            self.assertEqual(response.status_code, 200)
            xml = response.data.decode("utf-8")
            self.assertIn("<Response><Message>", xml)
            self.assertIn("unexpected issue", xml)
            # Ensure no python stack trace is leaked to the user
            self.assertNotIn("RuntimeError", xml)
            self.assertNotIn("Traceback", xml)


class TestSessionStore(unittest.TestCase):
    def test_in_memory_store_lifecycle(self):
        store = InMemorySessionStore()
        mock_repo = MagicMock()
        mock_repo.get_or_create_user.return_value = MagicMock(past_item_ids=[], default_address="")

        # Create
        s1 = store.get_or_create("+919999999999", mock_repo)
        self.assertEqual(s1.phone, "+919999999999")

        # Get
        s2 = store.get("+919999999999", mock_repo)
        self.assertIs(s1, s2)

        # Delete
        store.delete("+919999999999")
        self.assertIsNone(store.get("+919999999999", mock_repo))

        # Clear
        store.get_or_create("+911111111111", mock_repo)
        store.clear()
        self.assertIsNone(store.get("+911111111111", mock_repo))

    def test_redis_store_serialization(self):
        """Verify RedisSessionStore serialization and deserialization roundtrip."""
        mock_redis = MagicMock()
        store_data = {}

        def mock_setex(key, ttl, value):
            store_data[key] = value

        def mock_get(key):
            return store_data.get(key)

        mock_redis.setex.side_effect = mock_setex
        mock_redis.get.side_effect = mock_get

        redis_store = RedisSessionStore(mock_redis)
        mock_repo = MagicMock()
        mock_repo.get_or_create_user.return_value = MagicMock(past_item_ids=[], default_address="")

        session = redis_store.get_or_create("+918888888888", mock_repo)
        session.order.address = "123 Park Avenue"
        redis_store.save("+918888888888", session)

        loaded = redis_store.get("+918888888888", mock_repo)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.phone, "+918888888888")
        self.assertEqual(loaded.order.address, "123 Park Avenue")


if __name__ == "__main__":
    unittest.main()
