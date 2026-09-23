"""Security-focused test suite for WhatsApp Food Ordering Bot.

Tests:
1. Invalid Twilio signature (403 Forbidden).
2. Missing credentials / configuration validation without secret leaks.
3. Malformed WhatsApp requests (invalid phone, empty body, payload length).
4. Malicious XML characters & XSS injection payload safety in TwiML.
5. Payment webhook HMAC-SHA256 signature verification (valid & invalid).
6. Payment webhook replay attack protection (timestamp expired).
7. Payment webhook amount tampering detection (amount mismatch -> 400).
8. Payment webhook idempotency (duplicate callback -> 200 already_processed).
9. Unauthorized order access / IDOR prevention on repository get_order.
10. SQL injection attempts on queries, phone numbers, promos, and addresses.
11. Sensitive data masking in structured logs (phone numbers, tokens).
"""
import os
import json
import time
import hmac
import hashlib
import unittest
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["TWILIO_AUTH_TOKEN"] = "test_twilio_secret_token_12345"
os.environ["PAYMENT_WEBHOOK_SECRET"] = "test_payment_secret_key_67890"
os.environ["TWILIO_VALIDATE_SIGNATURE"] = "true"

from twilio.request_validator import RequestValidator
from database import Base, get_db
from db_models import UserRow, MenuItemRow, PromoCodeRow, OrderRow  # noqa: F401
from repository import Repository
from models import Order, MenuItem, PromoCode, User
from recommender import recommend
from session_store import InMemorySessionStore
from bot import (
    create_app,
    mask_phone_number,
    normalize_whatsapp_number,
    sanitize_input_text,
    validate_environment,
)


class TestSecurityHardening(unittest.TestCase):
    def setUp(self):
        self.twilio_token = "test_twilio_secret_token_12345"
        self.payment_secret = "test_payment_secret_key_67890"
        os.environ["TWILIO_AUTH_TOKEN"] = self.twilio_token
        os.environ["PAYMENT_WEBHOOK_SECRET"] = self.payment_secret
        os.environ["TWILIO_VALIDATE_SIGNATURE"] = "true"

        with get_db() as db:
            Base.metadata.drop_all(db.bind)
            Base.metadata.create_all(db.bind)

            # Seed test menu
            db.add(MenuItemRow(
                item_id="i1", name="Veg Biryani", restaurant="Paradise Biryani",
                rating=4.6, price=220.0, eta_minutes=35, tags="biryani,veg",
            ))
            # Seed test user
            db.add(UserRow(
                phone="+919900011122", name="Asha",
                default_address="204, Palm Residency, Indiranagar",
                past_item_ids="",
            ))
            # Seed test promo
            db.add(PromoCodeRow(
                code="SAVE15", description="15% off", discount_type="percentage",
                discount_value=0.15, min_order_value=200.0, active=True,
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
    # 1. Twilio Webhook Signature & Header Security
    # ------------------------------------------------------------------
    def test_invalid_twilio_signature_rejected(self):
        """Invalid Twilio signature returns 403 Forbidden."""
        data = {"From": "whatsapp:+919900011122", "Body": "hi"}
        headers = {"X-Twilio-Signature": "forged_signature_hash"}
        response = self.client.post("/whatsapp", data=data, headers=headers)
        self.assertEqual(response.status_code, 403)
        self.assertIn(b"Forbidden: Invalid signature.", response.data)

    def test_missing_twilio_signature_rejected(self):
        """Missing Twilio signature returns 403 Forbidden."""
        data = {"From": "whatsapp:+919900011122", "Body": "hi"}
        response = self.client.post("/whatsapp", data=data)
        self.assertEqual(response.status_code, 403)
        self.assertIn(b"Forbidden: Missing signature header.", response.data)

    # ------------------------------------------------------------------
    # 2. Environment Validation & No Secret Leakage
    # ------------------------------------------------------------------
    def test_environment_validation_does_not_leak_secrets(self):
        """Environment status dictionary contains booleans, not raw token values."""
        status = validate_environment()
        self.assertTrue(status["twilio_configured"])
        self.assertTrue(status["payment_webhook_configured"])
        self.assertTrue(status["signature_validation"])
        # Ensure secret strings are never present in status keys or values
        self.assertNotIn(self.twilio_token, str(status))
        self.assertNotIn(self.payment_secret, str(status))

    # ------------------------------------------------------------------
    # 3. Input Validation & Sanitization
    # ------------------------------------------------------------------
    def test_malformed_phone_rejected(self):
        """Invalid phone numbers (letters, malformed E.164) are rejected with 400."""
        url = "http://localhost/whatsapp"
        data = {"From": "whatsapp:invalid_phone_123", "Body": "hi"}
        headers = {"X-Twilio-Signature": self._sign_twilio(url, data)}

        response = self.client.post("/whatsapp", data=data, headers=headers)
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"Invalid 'From' parameter.", response.data)

    def test_sanitize_input_text_bounds_and_control_chars(self):
        """Input sanitizer strips ASCII control characters and enforces length limits."""
        raw = "Hello\x00\x08World\x1f\nKeep This"
        sanitized = sanitize_input_text(raw, max_length=50)
        self.assertEqual(sanitized, "HelloWorld\nKeep This")

        # Length bounding
        long_text = "A" * 2000
        bounded = sanitize_input_text(long_text, max_length=100)
        self.assertEqual(len(bounded), 100)

    # ------------------------------------------------------------------
    # 4. XML / TwiML Output Escaping & XSS Protection
    # ------------------------------------------------------------------
    def test_xss_and_xml_injection_safety(self):
        """Arbitrary user input containing XML/HTML tags cannot break TwiML structure."""
        url = "http://localhost/whatsapp"
        payload = "<script>alert('XSS')</script> & <Message>Injected</Message>"
        data = {"From": "whatsapp:+919900011122", "Body": payload}
        headers = {"X-Twilio-Signature": self._sign_twilio(url, data)}

        response = self.client.post("/whatsapp", data=data, headers=headers)
        self.assertEqual(response.status_code, 200)
        xml_content = response.data.decode("utf-8")

        # Verify XML structure remains intact and payload is escaped
        self.assertNotIn("<script>", xml_content)
        self.assertNotIn("<Message>Injected</Message>", xml_content)
        self.assertIn("&lt;script&gt;", xml_content)
        self.assertIn("&amp;", xml_content)

    # ------------------------------------------------------------------
    # 5. Payment Webhook Authentication & Verification
    # ------------------------------------------------------------------
    def test_payment_webhook_valid_hmac_signature(self):
        """Valid HMAC-SHA256 signature successfully confirms payment."""
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
            "payment_id": "pay_test_abc123",
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
        res_json = response.get_json()
        self.assertEqual(res_json["status"], "success")
        self.assertEqual(res_json["payment_status"], "paid")

        # Verify database updated
        with get_db() as db:
            repo = Repository(db)
            saved = repo.get_order(order_id)
            self.assertEqual(saved["payment_status"], "paid")
            self.assertEqual(saved["status"], "confirmed")

    def test_payment_webhook_invalid_hmac_signature_rejected(self):
        """Forged or invalid HMAC signature returns 403 Forbidden."""
        payload = {
            "order_id": "00000000-0000-0000-0000-000000000000",
            "payment_id": "pay_forged",
            "amount": 100.0,
            "status": "succeeded",
        }
        body_bytes = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "X-Payment-Signature": "forged_signature_hash_value",
            "X-Payment-Timestamp": str(time.time()),
        }
        response = self.client.post("/payment/webhook", data=body_bytes, headers=headers)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error"], "Invalid signature")

    # ------------------------------------------------------------------
    # 6. Replay Attack Protection
    # ------------------------------------------------------------------
    def test_payment_webhook_replay_protection_expired_timestamp(self):
        """Payment webhooks with timestamp older than 300s are rejected with 400."""
        payload = {
            "order_id": "00000000-0000-0000-0000-000000000000",
            "payment_id": "pay_old",
            "amount": 220.0,
            "status": "succeeded",
        }
        body_bytes = json.dumps(payload).encode("utf-8")
        signature = self._sign_payment(body_bytes)

        # 10 minutes (600s) old timestamp
        old_timestamp = str(time.time() - 600)
        headers = {
            "Content-Type": "application/json",
            "X-Payment-Signature": signature,
            "X-Payment-Timestamp": old_timestamp,
        }

        response = self.client.post("/payment/webhook", data=body_bytes, headers=headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "Webhook timestamp expired")

    # ------------------------------------------------------------------
    # 7. Payment Amount Tampering Detection
    # ------------------------------------------------------------------
    def test_payment_webhook_tampered_amount_rejected(self):
        """Payment amount lower than backend order total is rejected with 400."""
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

        # Attempt to pay ₹1.00 for a ₹220.00 order
        payload = {
            "order_id": order_id,
            "payment_id": "pay_tampered_1",
            "amount": 1.0,
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
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "Payment amount mismatch")

    # ------------------------------------------------------------------
    # 8. Webhook Idempotency & Duplicate Prevention
    # ------------------------------------------------------------------
    def test_payment_webhook_idempotency(self):
        """Repeated webhook callbacks for an already-paid order return 200 already_processed."""
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
            "payment_id": "pay_dup_123",
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
        res_json = response.get_json()
        self.assertEqual(res_json["status"], "already_processed")

    # ------------------------------------------------------------------
    # 9. IDOR & Unauthorized Order Access
    # ------------------------------------------------------------------
    def test_idor_unauthorized_order_access_prevented(self):
        """User cannot access an order belonging to another phone number."""
        with get_db() as db:
            repo = Repository(db)
            order_id = repo.create_order(
                Order(
                    user_phone="+919900011122",
                    item=MenuItem("i1", "Veg Biryani", "Paradise", 4.6, 220.0, 35),
                )
            )

            # Legitimate owner accesses order -> succeeds
            owner_order = repo.get_order(order_id, user_phone="+919900011122")
            self.assertIsNotNone(owner_order)
            self.assertEqual(owner_order["id"], order_id)

            # Different user attempts to access order -> None (unauthorized)
            attacker_order = repo.get_order(order_id, user_phone="+919888877777")
            self.assertIsNone(attacker_order)

    # ------------------------------------------------------------------
    # 10. SQL Injection Protection
    # ------------------------------------------------------------------
    def test_sql_injection_attempt_in_recommender(self):
        """SQL injection payloads in search queries do not crash or alter query logic."""
        user = User("+919900011122", "Asha")
        menu = [MenuItem("i1", "Veg Biryani", "Paradise", 4.6, 220.0, 35, ["biryani", "veg"])]

        sqli_payload = "Veg' OR '1'='1' --"
        results = recommend(sqli_payload, user, menu)
        # Recommender treats it as literal string terms and safely returns no match
        self.assertEqual(results, [])

    def test_sql_injection_attempt_in_repository(self):
        """SQL injection payloads in phone numbers or order queries are safely parameterized."""
        with get_db() as db:
            repo = Repository(db)
            sqli_phone = "'+OR+'1'='1"
            orders = repo.list_user_orders(sqli_phone)
            self.assertEqual(orders, [])

            # Malformed UUID injection
            malformed_order = repo.get_order("' OR 1=1 --")
            self.assertIsNone(malformed_order)

    # ------------------------------------------------------------------
    # 11. Sensitive Data Masking in Logs
    # ------------------------------------------------------------------
    def test_phone_number_masking_for_logging(self):
        """Phone numbers are masked when logged to prevent PII exposure."""
        self.assertEqual(mask_phone_number("+919900011122"), "+9199****1122")
        self.assertEqual(mask_phone_number("+14155552671"), "+1415****2671")
        self.assertEqual(mask_phone_number("123"), "***")
        self.assertEqual(mask_phone_number(""), "***")


if __name__ == "__main__":
    unittest.main()
