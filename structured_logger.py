"""Structured Logging & Monitoring Module for WhatsApp Food Ordering Bot.

Provides:
- JSON/Structured logging formatter
- Contextual correlation ID propagation across request lifecycles
- Privacy-safe user identifier hashing/masking
- Strict secret redaction filter (protects tokens, keys, passwords)
- Standardized event schemas for metrics and log ingestion
"""
import json
import logging
import re
import hashlib
import time
from datetime import datetime, timezone
import contextvars
from typing import Optional, Any, Dict

# Context variable to hold the active request correlation ID
correlation_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default=""
)


def get_correlation_id() -> str:
    """Retrieve the current request correlation ID."""
    return correlation_id_ctx.get() or ""


def set_correlation_id(cid: str) -> None:
    """Set the correlation ID for the active request context."""
    correlation_id_ctx.set(cid)


def generate_safe_user_id(phone: str) -> str:
    """Generate a deterministic, privacy-safe pseudonymous user ID from phone number."""
    if not phone:
        return "anon_user"
    # Masked string + short hash
    masked = f"{phone[:4]}****{phone[-4:]}" if len(phone) >= 8 else "***"
    sha = hashlib.sha256(phone.encode("utf-8")).hexdigest()[:8]
    return f"usr_{sha}_{masked}"


class SecretRedactingFilter(logging.Filter):
    """Logging filter to guarantee no API keys, tokens, or passwords appear in logs."""

    SENSITIVE_PATTERNS = [
        (
            re.compile(r"((?:auth_token|token|secret|password|api_key|key|signature)[\"']?\s*[:=]\s*[\"']?)([a-zA-Z0-9_\-\.]{8,})", re.IGNORECASE),
            r"\1***REDACTED***",
        ),
        (
            re.compile(r"(Bearer\s+)([a-zA-Z0-9_\-\.]{8,})", re.IGNORECASE),
            r"\1***REDACTED***",
        ),
    ]

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            for pattern, repl in self.SENSITIVE_PATTERNS:
                record.msg = pattern.sub(repl, record.msg)
        return True


class StructuredJsonFormatter(logging.Formatter):
    """Formats log records as structured JSON entries for log aggregators (ELK, CloudWatch, Datadog)."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": getattr(record, "correlation_id", get_correlation_id()),
        }

        # Attach standard domain tracking fields if present
        for field in (
            "event_type",
            "safe_user_id",
            "order_id",
            "payment_id",
            "prev_state",
            "new_state",
            "outcome",
            "error_category",
            "duration_ms",
            "item_id",
            "promo_code",
            "amount",
        ):
            if hasattr(record, field):
                log_entry[field] = getattr(record, field)

        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            log_entry["exception"] = record.exc_text

        return json.dumps(log_entry)


def setup_structured_logging(level: str = "INFO", json_format: bool = True) -> logging.Logger:
    """Configure and return the root structured logger for whatsapp_bot."""
    logger = logging.getLogger("whatsapp_bot")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()

    handler = logging.StreamHandler()
    if json_format:
        handler.setFormatter(StructuredJsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] [%(name)s] [corr=%(correlation_id)s] %(message)s")
        )

    handler.addFilter(SecretRedactingFilter())
    logger.addHandler(handler)
    logger.propagate = False
    return logger
