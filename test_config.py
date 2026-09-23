"""Test suite for Environment-Based Configuration System.

Tests:
1. Development configuration default values.
2. Testing configuration behavior.
3. Production configuration validation enforcement:
   - Secret key strength
   - PostgreSQL / non-SQLite database
   - Twilio credentials and WhatsApp number format
   - Mandatory Twilio signature validation in production
   - Payment webhook secret strength
   - Valid production configuration passes
4. Safe dictionary serialization (no leaked secrets).
"""
import os
import unittest
from config import Config, get_config, ConfigurationError


MOCK_TWILIO_SID = "AC" + "mock_test_sid_val"


class TestConfiguration(unittest.TestCase):
    """Unit tests for configuration profiles and production validation."""

    def test_development_config_defaults(self):
        config = get_config("development")
        config.validate()  # Should not raise
        self.assertEqual(config.ENV, "development")
        self.assertTrue(config.DEBUG)
        self.assertFalse(config.TESTING)

    def test_testing_config(self):
        config = get_config("testing")
        self.assertEqual(config.ENV, "testing")
        self.assertTrue(config.TESTING)

    def test_production_missing_secret_key_raises(self):
        config = Config(
            ENV="production",
            SECRET_KEY="dev-insecure-secret-key-change-in-prod",
            DATABASE_URL="postgresql://user:pass@localhost:5432/db",
            TWILIO_ACCOUNT_SID=MOCK_TWILIO_SID,
            TWILIO_AUTH_TOKEN="valid_secret_token_12345678",
            TWILIO_WHATSAPP_NUMBER="whatsapp:+14155238886",
            TWILIO_VALIDATE_SIGNATURE=True,
            PAYMENT_WEBHOOK_SECRET="valid_payment_secret_12345678",
        )
        with self.assertRaises(ConfigurationError) as ctx:
            config.validate()
        self.assertIn("SECRET_KEY must be set to a strong random secret", str(ctx.exception))

    def test_production_sqlite_rejected(self):
        config = Config(
            ENV="production",
            SECRET_KEY="a" * 32,
            DATABASE_URL="sqlite:///dev.db",
            TWILIO_ACCOUNT_SID=MOCK_TWILIO_SID,
            TWILIO_AUTH_TOKEN="valid_secret_token_12345678",
            TWILIO_WHATSAPP_NUMBER="whatsapp:+14155238886",
            TWILIO_VALIDATE_SIGNATURE=True,
            PAYMENT_WEBHOOK_SECRET="valid_payment_secret_12345678",
        )
        with self.assertRaises(ConfigurationError) as ctx:
            config.validate()
        self.assertIn("Production requires a robust production database URL", str(ctx.exception))

    def test_production_invalid_twilio_sid_rejected(self):
        config = Config(
            ENV="production",
            SECRET_KEY="a" * 32,
            DATABASE_URL="postgresql://user:pass@localhost:5432/db",
            TWILIO_ACCOUNT_SID="INVALID_SID_123",
            TWILIO_AUTH_TOKEN="valid_secret_token_12345678",
            TWILIO_WHATSAPP_NUMBER="whatsapp:+14155238886",
            TWILIO_VALIDATE_SIGNATURE=True,
            PAYMENT_WEBHOOK_SECRET="valid_payment_secret_12345678",
        )
        with self.assertRaises(ConfigurationError) as ctx:
            config.validate()
        self.assertIn("TWILIO_ACCOUNT_SID must be a valid Twilio Account SID", str(ctx.exception))

    def test_production_disabled_signature_validation_rejected(self):
        config = Config(
            ENV="production",
            SECRET_KEY="a" * 32,
            DATABASE_URL="postgresql://user:pass@localhost:5432/db",
            TWILIO_ACCOUNT_SID=MOCK_TWILIO_SID,
            TWILIO_AUTH_TOKEN="valid_secret_token_12345678",
            TWILIO_WHATSAPP_NUMBER="whatsapp:+14155238886",
            TWILIO_VALIDATE_SIGNATURE=False,
            PAYMENT_WEBHOOK_SECRET="valid_payment_secret_12345678",
        )
        with self.assertRaises(ConfigurationError) as ctx:
            config.validate()
        self.assertIn("TWILIO_VALIDATE_SIGNATURE must never be disabled in production", str(ctx.exception))

    def test_production_weak_payment_secret_rejected(self):
        config = Config(
            ENV="production",
            SECRET_KEY="a" * 32,
            DATABASE_URL="postgresql://user:pass@localhost:5432/db",
            TWILIO_ACCOUNT_SID=MOCK_TWILIO_SID,
            TWILIO_AUTH_TOKEN="valid_secret_token_12345678",
            TWILIO_WHATSAPP_NUMBER="whatsapp:+14155238886",
            TWILIO_VALIDATE_SIGNATURE=True,
            PAYMENT_WEBHOOK_SECRET="short",
        )
        with self.assertRaises(ConfigurationError) as ctx:
            config.validate()
        self.assertIn("PAYMENT_WEBHOOK_SECRET must be configured with a strong secret", str(ctx.exception))

    def test_production_valid_configuration_passes(self):
        config = Config(
            ENV="production",
            SECRET_KEY="secure_prod_secret_key_1234567890",
            DATABASE_URL="postgresql://postgres:prodpass@db.example.com:5432/app",
            TWILIO_ACCOUNT_SID=MOCK_TWILIO_SID,
            TWILIO_AUTH_TOKEN="prod_twilio_auth_token_1234567890",
            TWILIO_WHATSAPP_NUMBER="whatsapp:+14155238886",
            TWILIO_VALIDATE_SIGNATURE=True,
            PAYMENT_WEBHOOK_SECRET="prod_payment_webhook_secret_12345",
            SESSION_STORE_TYPE="redis",
            REDIS_URL="redis://default:pass@redis.example.com:6379/0",
        )
        config.validate()
        safe_dict = config.to_safe_dict()
        self.assertEqual(safe_dict["environment"], "production")
        self.assertTrue(safe_dict["twilio_configured"])
        self.assertTrue(safe_dict["payment_webhook_configured"])
        # Ensure raw secret is not in safe_dict
        self.assertNotIn("prod_payment_webhook_secret_12345", str(safe_dict))
        self.assertNotIn("prod_twilio_auth_token_1234567890", str(safe_dict))


if __name__ == "__main__":
    unittest.main()
