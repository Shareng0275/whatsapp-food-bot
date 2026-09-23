"""Environment-Based Configuration System.

Manages configuration profiles for Development, Testing, and Production environments.
Validates all required secrets and parameters at startup, preventing production startup
with insecure defaults, missing credentials, or disabled security checks.
"""
import os
import re
from dataclasses import dataclass, field
from typing import Optional, List


class ConfigurationError(ValueError):
    """Raised when environment configuration is invalid or missing required variables."""
    pass


@dataclass
class Config:
    """Base application configuration with default safe values."""
    # Environment
    ENV: str = "development"
    DEBUG: bool = field(default_factory=lambda: os.environ.get("FLASK_DEBUG", "0").strip().lower() in ("1", "true", "yes") or os.environ.get("FLASK_ENV", "development") == "development")
    TESTING: bool = False

    # Security & Flask
    SECRET_KEY: str = field(default_factory=lambda: os.environ.get("SECRET_KEY", "dev-insecure-secret-key-change-in-prod"))
    ALLOWED_HOSTS: List[str] = field(default_factory=lambda: [h.strip() for h in os.environ.get("ALLOWED_HOSTS", "*").split(",") if h.strip()])
    
    # Server & Networking
    PORT: int = field(default_factory=lambda: int(os.environ.get("PORT", "5000")))
    REQUEST_TIMEOUT_SECONDS: int = field(default_factory=lambda: int(os.environ.get("REQUEST_TIMEOUT_SECONDS", "10")))

    # Database
    DATABASE_URL: str = field(
        default_factory=lambda: os.environ.get(
            "DATABASE_URL",
            "sqlite:///dev.db"
        )
    )

    # Session Store & Redis
    SESSION_STORE_TYPE: str = field(default_factory=lambda: os.environ.get("SESSION_STORE_TYPE", "memory").lower())
    REDIS_URL: str = field(default_factory=lambda: os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    SESSION_TTL_SECONDS: int = field(default_factory=lambda: int(os.environ.get("SESSION_TTL_SECONDS", "86400")))

    # Twilio Webhook & Messaging
    TWILIO_ACCOUNT_SID: str = field(default_factory=lambda: os.environ.get("TWILIO_ACCOUNT_SID", ""))
    TWILIO_AUTH_TOKEN: str = field(default_factory=lambda: os.environ.get("TWILIO_AUTH_TOKEN", ""))
    TWILIO_WHATSAPP_NUMBER: str = field(default_factory=lambda: os.environ.get("TWILIO_WHATSAPP_NUMBER", ""))
    TWILIO_VALIDATE_SIGNATURE: bool = field(default_factory=lambda: os.environ.get("TWILIO_VALIDATE_SIGNATURE", "true").strip().lower() in ("true", "1", "yes"))

    # Payment Gateway
    PAYMENT_WEBHOOK_SECRET: str = field(default_factory=lambda: os.environ.get("PAYMENT_WEBHOOK_SECRET", ""))
    PAYMENT_REPLAY_WINDOW_SECONDS: int = field(default_factory=lambda: int(os.environ.get("PAYMENT_REPLAY_WINDOW_SECONDS", "300")))

    # Rate Limiting & Abuse Protection
    WEBHOOK_RATE_LIMIT: int = field(default_factory=lambda: int(os.environ.get("WEBHOOK_RATE_LIMIT", "60")))
    USER_MESSAGE_RATE_LIMIT: int = field(default_factory=lambda: int(os.environ.get("USER_MESSAGE_RATE_LIMIT", "20")))
    PAYMENT_WEBHOOK_RATE_LIMIT: int = field(default_factory=lambda: int(os.environ.get("PAYMENT_WEBHOOK_RATE_LIMIT", "30")))
    PAYMENT_INITIATION_LIMIT: int = field(default_factory=lambda: int(os.environ.get("PAYMENT_INITIATION_LIMIT", "3")))
    PROMO_ATTEMPT_LIMIT: int = field(default_factory=lambda: int(os.environ.get("PROMO_ATTEMPT_LIMIT", "5")))

    # Logging
    LOG_LEVEL: str = field(default_factory=lambda: os.environ.get("LOG_LEVEL", "INFO").upper())

    def validate(self) -> None:
        """Validate configuration settings based on environment mode."""
        if self.ENV == "production":
            self._validate_production()
        elif self.ENV == "testing":
            self._validate_testing()
        else:
            self._validate_development()

    def _validate_production(self) -> None:
        """Enforce strict production invariants."""
        errors = []

        # 1. Secret Key
        if not self.SECRET_KEY or self.SECRET_KEY in ("dev-insecure-secret-key-change-in-prod", "secret", "changeme", "default"):
            errors.append("SECRET_KEY must be set to a strong random secret in production.")

        # 2. Database
        if not self.DATABASE_URL or self.DATABASE_URL.startswith("sqlite"):
            errors.append("Production requires a robust production database URL (e.g. PostgreSQL), not SQLite.")

        # 3. Twilio Credentials
        if not self.TWILIO_ACCOUNT_SID or not self.TWILIO_ACCOUNT_SID.startswith("AC"):
            errors.append("TWILIO_ACCOUNT_SID must be a valid Twilio Account SID (starting with AC).")
        if not self.TWILIO_AUTH_TOKEN or len(self.TWILIO_AUTH_TOKEN) < 16:
            errors.append("TWILIO_AUTH_TOKEN must be configured with a valid Twilio token.")
        if not self.TWILIO_WHATSAPP_NUMBER or not self.TWILIO_WHATSAPP_NUMBER.startswith("whatsapp:"):
            errors.append("TWILIO_WHATSAPP_NUMBER must be formatted as 'whatsapp:+<E164>'.")
        if not self.TWILIO_VALIDATE_SIGNATURE:
            errors.append("TWILIO_VALIDATE_SIGNATURE must never be disabled in production.")

        # 4. Payment Gateway Secret
        if not self.PAYMENT_WEBHOOK_SECRET or len(self.PAYMENT_WEBHOOK_SECRET) < 16:
            errors.append("PAYMENT_WEBHOOK_SECRET must be configured with a strong secret (at least 16 characters).")

        # 5. Session Store
        if self.SESSION_STORE_TYPE == "redis" and not self.REDIS_URL:
            errors.append("REDIS_URL must be configured when SESSION_STORE_TYPE is 'redis'.")

        # 6. Rate limits
        if self.WEBHOOK_RATE_LIMIT <= 0 or self.USER_MESSAGE_RATE_LIMIT <= 0:
            errors.append("Rate limits must be positive integers.")

        if errors:
            raise ConfigurationError(
                "Production configuration validation failed with the following errors:\n- "
                + "\n- ".join(errors)
            )

    def _validate_development(self) -> None:
        """Validate development settings."""
        if self.TWILIO_ACCOUNT_SID and not self.TWILIO_ACCOUNT_SID.startswith("AC"):
            pass  # Warn or allow mock SIDs in dev

    def _validate_testing(self) -> None:
        """Validate testing settings."""
        pass

    def to_safe_dict(self) -> dict:
        """Return non-sensitive configuration values for logging / health reporting."""
        return {
            "environment": self.ENV,
            "debug": self.DEBUG,
            "testing": self.TESTING,
            "session_store_type": self.SESSION_STORE_TYPE,
            "twilio_configured": bool(self.TWILIO_AUTH_TOKEN and self.TWILIO_ACCOUNT_SID),
            "twilio_signature_validation": self.TWILIO_VALIDATE_SIGNATURE,
            "payment_webhook_configured": bool(self.PAYMENT_WEBHOOK_SECRET),
            "database_type": self.DATABASE_URL.split("://")[0] if "://" in self.DATABASE_URL else "unknown",
            "webhook_rate_limit": self.WEBHOOK_RATE_LIMIT,
            "user_message_rate_limit": self.USER_MESSAGE_RATE_LIMIT,
            "payment_initiation_limit": self.PAYMENT_INITIATION_LIMIT,
            "promo_attempt_limit": self.PROMO_ATTEMPT_LIMIT,
            "log_level": self.LOG_LEVEL,
        }


def get_config(env_name: Optional[str] = None) -> Config:
    """Factory function to load and validate configuration for the current environment."""
    env = (env_name or os.environ.get("FLASK_ENV") or os.environ.get("APP_ENV") or "development").strip().lower()

    if env == "production" or env == "prod":
        config = Config(ENV="production", DEBUG=False, TESTING=False)
    elif env == "testing" or env == "test":
        config = Config(
            ENV="testing",
            DEBUG=True,
            TESTING=True,
            DATABASE_URL=os.environ.get("DATABASE_URL", "sqlite://"),
            TWILIO_VALIDATE_SIGNATURE=os.environ.get("TWILIO_VALIDATE_SIGNATURE", "false").strip().lower() in ("true", "1", "yes"),
        )
    else:
        config = Config(ENV="development", DEBUG=True, TESTING=False)

    config.validate()
    return config
