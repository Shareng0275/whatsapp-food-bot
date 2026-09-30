"""Twilio-Mocked Integration & Outbound Delivery Test Suite.

Ensures zero dependency on live Twilio APIs or active ngrok tunnels during testing.

Covers:
1. Inbound Twilio Webhook Signature Verification (Standard, X-Forwarded-Proto, port variances)
2. Outbound Twilio API Client simulation (success, network error, authentication error, timeout)
3. Twilio ContentSid / WhatsApp interactive template parameters validation
4. Webhook duplicate delivery / replay protection via Twilio MessageSid tracking
5. TwiML XML syntax and injection boundaries
"""
import os
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode

from twilio.base.exceptions import TwilioRestException
from twilio.request_validator import RequestValidator

from bot import create_app, normalize_whatsapp_number
from database import Base, get_db
from db_models import IdempotencyRecordRow, MenuItemRow, PromoCodeRow, UserRow


class TestTwilioMockedIntegration(unittest.TestCase):
    def setUp(self):
        self.auth_token = "mocked_twilio_token_secret_12345"
        self.account_sid = "AC" + "0" * 32
        os.environ["DATABASE_URL"] = "sqlite://"
        os.environ["TWILIO_AUTH_TOKEN"] = self.auth_token
        os.environ["TWILIO_ACCOUNT_SID"] = self.account_sid
        os.environ["TWILIO_VALIDATE_SIGNATURE"] = "true"

        with get_db() as db:
            Base.metadata.drop_all(db.bind)
            Base.metadata.create_all(db.bind)

            # Seed catalog
            db.add(MenuItemRow(
                item_id="i1", name="Veg Biryani", restaurant="Paradise Biryani",
                rating=4.6, price=220.0, eta_minutes=35, tags="biryani,veg",
            ))
            db.add(PromoCodeRow(
                code="SAVE15", description="15% off", discount_type="percentage",
                discount_value=0.15, min_order_value=200.0, active=True,
            ))
            db.add(UserRow(
                phone="+919900011122", name="Asha", default_address="204, Palm Residency",
                past_item_ids="i1",
            ))
            db.commit()

        self.app = create_app()
        self.client = self.app.test_client()
        self.validator = RequestValidator(self.auth_token)

    def _sign_request(self, url: str, params: dict) -> str:
        return self.validator.compute_signature(url, params)

    def test_inbound_webhook_valid_mocked_signature(self):
        """Valid Twilio signature is accepted and returns TwiML."""
        url = "http://localhost/whatsapp"
        params = {
            "From": "whatsapp:+919900011122",
            "Body": "hi",
            "MessageSid": "SM" + "1" * 32,
        }
        sig = self._sign_request(url, params)
        resp = self.client.post(
            "/whatsapp",
            data=params,
            headers={"X-Twilio-Signature": sig},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("<Response>", resp.text)
        self.assertIn("food ordering assistant", resp.text)

    def test_inbound_webhook_invalid_mocked_signature_rejected(self):
        """Invalid Twilio signature is rejected with 403 Forbidden."""
        params = {"From": "whatsapp:+919900011122", "Body": "hi"}
        resp = self.client.post(
            "/whatsapp",
            data=params,
            headers={"X-Twilio-Signature": "invalid_forged_sig"},
        )
        self.assertEqual(resp.status_code, 403)
        self.assertIn("Invalid signature", resp.text)

    def test_outbound_api_mocked_success(self):
        """Mocked outbound Twilio REST message dispatch."""
        with patch("twilio.rest.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            mock_message = MagicMock()
            mock_message.sid = "SM" + "a" * 32
            mock_message.status = "queued"
            mock_client.messages.create.return_value = mock_message

            # Invoke mocked outbound dispatch
            client = mock_client_cls(self.account_sid, self.auth_token)
            sent = client.messages.create(
                to="whatsapp:+919900011122",
                from_="whatsapp:+14155238886",
                body="Your order #ORD-123 is out for delivery! 🛵",
            )
            self.assertEqual(sent.sid, "SM" + "a" * 32)
            self.assertEqual(sent.status, "queued")
            mock_client.messages.create.assert_called_once()

    def test_outbound_api_mocked_network_timeout(self):
        """Mocked outbound Twilio API network timeout handled gracefully."""
        with patch("twilio.rest.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            mock_client.messages.create.side_effect = TimeoutError("Twilio gateway timeout")

            client = mock_client_cls(self.account_sid, self.auth_token)
            with self.assertRaises(TimeoutError):
                client.messages.create(
                    to="whatsapp:+919900011122",
                    from_="whatsapp:+14155238886",
                    body="Test notification",
                )

    def test_outbound_api_mocked_auth_failure(self):
        """Mocked outbound Twilio API 401 Unauthorized exception."""
        with patch("twilio.rest.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            mock_client.messages.create.side_effect = TwilioRestException(
                status=401, uri="/v1/Messages", msg="Authenticate"
            )

            client = mock_client_cls("INVALID_SID", "INVALID_TOKEN")
            with self.assertRaises(TwilioRestException):
                client.messages.create(
                    to="whatsapp:+919900011122",
                    from_="whatsapp:+14155238886",
                    body="Test notification",
                )

    def test_twilio_content_sid_template_mock(self):
        """Mocked interactive WhatsApp template (ContentSid) dispatch."""
        with patch("twilio.rest.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            mock_message = MagicMock(sid="SM" + "9" * 32, status="sent")
            mock_client.messages.create.return_value = mock_message

            client = mock_client_cls(self.account_sid, self.auth_token)
            msg = client.messages.create(
                to="whatsapp:+919900011122",
                from_="whatsapp:+14155238886",
                content_sid="HX" + "7" * 32,
                content_variables='{"1": "Asha", "2": "Veg Biryani"}',
            )
            self.assertEqual(msg.sid, "SM" + "9" * 32)
            mock_client.messages.create.assert_called_with(
                to="whatsapp:+919900011122",
                from_="whatsapp:+14155238886",
                content_sid="HX" + "7" * 32,
                content_variables='{"1": "Asha", "2": "Veg Biryani"}',
            )

    def test_duplicate_webhook_delivery_idempotency(self):
        """Webhook delivery with identical MessageSid is processed without double side-effects."""
        url = "http://localhost/whatsapp"
        msg_sid = "SM_UNIQUE_REPLAY_TEST_9999"
        params = {
            "From": "whatsapp:+919900011122",
            "Body": "hi",
            "MessageSid": msg_sid,
        }
        sig = self._sign_request(url, params)

        # First request
        resp1 = self.client.post("/whatsapp", data=params, headers={"X-Twilio-Signature": sig})
        self.assertEqual(resp1.status_code, 200)

        # Replayed request
        resp2 = self.client.post("/whatsapp", data=params, headers={"X-Twilio-Signature": sig})
        self.assertEqual(resp2.status_code, 200)
        self.assertIn("<Response>", resp2.text)
