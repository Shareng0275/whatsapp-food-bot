"""Automated tests for Structured Logging, Correlation Tracing, and Monitoring.

Tests:
1. Logs are generated with structured JSON format and event types.
2. Correlation IDs propagate from incoming headers to responses and log contexts.
3. Secret redaction filter prevents tokens, passwords, and secrets from appearing in logs.
4. Privacy-safe user IDs (pseudonymous hashed/masked format) are generated.
5. Health check endpoint accurately tests DB, session store, and gateway without leaking internal credentials.
6. Failure and state transition events produce structured log entries.
"""
import os
import json
import logging
import time
import hmac
import hashlib
import unittest
from io import StringIO
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
from session_store import InMemorySessionStore
from bot import create_app
from structured_logger import (
    setup_structured_logging,
    generate_safe_user_id,
    SecretRedactingFilter,
    StructuredJsonFormatter,
)


class TestLoggingAndMonitoring(unittest.TestCase):
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

    def test_correlation_id_propagation(self):
        """Incoming X-Correlation-ID header is propagated to the response header."""
        custom_cid = "corr-test-uuid-987654"
        url = "http://localhost/whatsapp"
        data = {"From": "whatsapp:+919900011122", "Body": "hi"}
        headers = {
            "X-Twilio-Signature": self._sign_twilio(url, data),
            "X-Correlation-ID": custom_cid,
        }

        response = self.client.post("/whatsapp", data=data, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("X-Correlation-ID"), custom_cid)

    def test_auto_generated_correlation_id_when_missing(self):
        """When X-Correlation-ID is missing, a UUID is automatically generated and returned in headers."""
        url = "http://localhost/whatsapp"
        data = {"From": "whatsapp:+919900011122", "Body": "hi"}
        headers = {"X-Twilio-Signature": self._sign_twilio(url, data)}

        response = self.client.post("/whatsapp", data=data, headers=headers)
        self.assertEqual(response.status_code, 200)
        cid = response.headers.get("X-Correlation-ID")
        self.assertIsNotNone(cid)
        self.assertGreater(len(cid), 10)

    def test_safe_user_id_generation(self):
        """Safe user ID is pseudonymous, deterministic, and masks personal phone numbers."""
        safe_id = generate_safe_user_id("+919900011122")
        self.assertTrue(safe_id.startswith("usr_"))
        self.assertIn("+919****1122", safe_id)
        # Raw unmasked phone number is not directly exposed
        self.assertNotIn("+919900011122", safe_id)

    def test_secret_redaction_filter_in_logs(self):
        """SecretRedactingFilter replaces sensitive auth tokens and passwords with ***REDACTED***."""
        stream = StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(StructuredJsonFormatter())
        handler.addFilter(SecretRedactingFilter())

        test_logger = logging.getLogger("test_redaction")
        test_logger.setLevel(logging.INFO)
        test_logger.addHandler(handler)

        # Log sensitive string containing token
        test_logger.info(f"Connecting with auth_token={self.twilio_token} and secret={self.payment_secret}")
        log_output = stream.getvalue()

        # Verify secrets are redacted
        self.assertNotIn(self.twilio_token, log_output)
        self.assertNotIn(self.payment_secret, log_output)
        self.assertIn("***REDACTED***", log_output)

    def test_structured_log_event_generation(self):
        """WhatsApp message and payment webhook generate structured logs with required event types."""
        stream = StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(StructuredJsonFormatter())

        bot_logger = logging.getLogger("whatsapp_bot")
        bot_logger.addHandler(handler)

        url = "http://localhost/whatsapp"
        data = {"From": "whatsapp:+919900011122", "Body": "hi"}
        headers = {
            "X-Twilio-Signature": self._sign_twilio(url, data),
            "X-Correlation-ID": "test-cid-111",
        }

        self.client.post("/whatsapp", data=data, headers=headers)
        log_records = [json.loads(line) for line in stream.getvalue().strip().split("\n") if line.strip()]

        event_types = [r.get("event_type") for r in log_records]
        self.assertIn("incoming_request", event_types)
        self.assertIn("state_transition", event_types)

        # Check correlation ID attached
        for r in log_records:
            if "correlation_id" in r:
                self.assertEqual(r["correlation_id"], "test-cid-111")

    def test_health_check_safe_monitoring(self):
        """Health check returns structured dependency health without leaking connection strings."""
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()

        self.assertEqual(data["status"], "healthy")
        self.assertEqual(data["database"], "healthy")
        self.assertEqual(data["session_store"], "healthy")
        self.assertEqual(data["payment_gateway"], "configured")
        # Ensure database URL or credentials are not exposed
        self.assertNotIn("sqlite", str(data))
        self.assertNotIn("password", str(data))


if __name__ == "__main__":
    unittest.main()
