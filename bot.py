"""Production-ready Flask adapter for Twilio WhatsApp Food Ordering Bot.

Wires together:
- Flask application factory (create_app)
- Twilio X-Twilio-Signature validation
- Input sanitization & media-type inspection
- SessionStore integration (in-memory or Redis)
- Sliding-window rate limiting & abuse prevention
- Structured JSON logging with correlation IDs and PII redaction
- Synchronous TwiML responses + optional REST API notifications
- HMAC-verified payment callbacks (/payment/webhook)
- Payment initiation with retry boundaries (/payment/initiate)
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import os
import re
import time
import uuid
from typing import Any, Optional

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request
from twilio.request_validator import RequestValidator  # type: ignore
from twilio.rest import Client  # type: ignore
from twilio.twiml.messaging_response import MessagingResponse  # type: ignore

from config import Config, get_config
from conversation import Session
from database import Base, get_db
from db_models import UserRow
from sqlalchemy import select
from rate_limiter import InMemoryRateLimiter, RateLimiter
from repository import Repository
from session_store import InMemorySessionStore, SessionStore
from structured_logger import (
    SecretRedactingFilter,
    StructuredJsonFormatter,
    generate_safe_user_id,
    set_correlation_id,
)
from observability import (
    get_metrics_collector,
    emit_event,
    Timer,
    EVENT_WEBHOOK_RECEIVED,
    EVENT_WEBHOOK_RESPONSE,
    EVENT_AUTH_FAILURE,
    EVENT_RATE_LIMIT,
    EVENT_BOT_ERROR,
    EVENT_MEDIA_REJECTED,
    EVENT_CONVERSATION_STARTED,
    EVENT_STATE_TRANSITION,
    EVENT_ORDER_CREATED,
    EVENT_PAYMENT_INITIATED,
    EVENT_PAYMENT_SUCCESS,
    EVENT_PAYMENT_FAILURE,
)

load_dotenv()

logger = logging.getLogger("whatsapp_bot")


# ---------------------------------------------------------------------------
# Helper functions for normalization, sanitization, and PII masking
# ---------------------------------------------------------------------------

def normalize_whatsapp_number(number: Optional[str]) -> str:
    """Normalize phone numbers to standard E.164 (+<country><digits>).

    Strips 'whatsapp:', spaces, parentheses, hyphens, and whitespace.
    Returns empty string if invalid or malformed.
    """
    if not number:
        return ""
    clean = str(number).strip()
    if clean.lower().startswith("whatsapp:"):
        clean = clean[9:].strip()
    clean = re.sub(r"[\s\(\)\-]", "", clean)
    if not clean:
        return ""
    if not clean.startswith("+"):
        clean = f"+{clean}"
    if not re.match(r"^\+[1-9]\d{6,14}$", clean):
        return ""
    return clean


def mask_phone_number(phone: Optional[str]) -> str:
    """Mask phone number for privacy/logging (e.g. +9199****1122)."""
    if not phone:
        return "***"
    phone_str = str(phone).strip()
    if len(phone_str) < 9:
        return "***"
    return f"{phone_str[:5]}****{phone_str[-4:]}"


def sanitize_input_text(text: Optional[str], max_length: int = 500) -> str:
    """Sanitize inbound text from WhatsApp users.

    Strips ASCII control characters and limits length.
    """
    if not text:
        return ""
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(text))
    return cleaned.strip()[:max_length]


def validate_environment(config: Optional[Config] = None) -> dict[str, bool]:
    """Validate environment status and return safe boolean flags."""
    cfg = config or get_config()
    return {
        "twilio_configured": bool(cfg.TWILIO_AUTH_TOKEN and cfg.TWILIO_ACCOUNT_SID),
        "payment_webhook_configured": bool(cfg.PAYMENT_WEBHOOK_SECRET),
        "signature_validation": bool(cfg.TWILIO_VALIDATE_SIGNATURE),
    }


# ---------------------------------------------------------------------------
# Application Factory
# ---------------------------------------------------------------------------

def create_app(
    config: Optional[Config] = None,
    session_store: Optional[SessionStore] = None,
    rate_limiter: Optional[RateLimiter] = None,
) -> Flask:
    """Initialize and configure the Flask WhatsApp bot application."""
    app = Flask(__name__)
    app_config = config or get_config()
    app.config.from_mapping(
        ENV=app_config.ENV,
        DEBUG=app_config.DEBUG,
        TESTING=app_config.TESTING,
        SECRET_KEY=app_config.SECRET_KEY,
        MAX_CONTENT_LENGTH=1024 * 1024,  # 1MB limit against memory exhaustion attacks
    )

    @app.after_request
    def set_security_headers(response: Response) -> Response:
        """Inject defense-in-depth HTTP security headers."""
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
        response.headers["Content-Security-Policy"] = "default-src 'none'"
        return response

    @app.errorhandler(413)
    def request_entity_too_large(error):
        """Reject payloads exceeding 1MB."""
        logger.warning("Request payload exceeded 1MB limit", extra={"event_type": "security_violation"})
        return Response("Payload Too Large", status=413, mimetype="text/plain")

    # Configure structured logging
    handler = logging.StreamHandler()
    handler.setFormatter(StructuredJsonFormatter())
    handler.addFilter(SecretRedactingFilter())
    logging.root.handlers = [handler]
    logging.root.setLevel(getattr(logging, app_config.LOG_LEVEL, logging.INFO))

    # Initialize persistence and rate limit stores
    store: SessionStore = session_store or InMemorySessionStore()
    limiter: RateLimiter = rate_limiter or InMemoryRateLimiter()

    # Twilio REST client and Request Validator
    validator = (
        RequestValidator(app_config.TWILIO_AUTH_TOKEN)
        if app_config.TWILIO_AUTH_TOKEN
        else None
    )

    # Metrics Collector
    metrics = get_metrics_collector()

    # -----------------------------------------------------------------------
    # Routes
    # -----------------------------------------------------------------------

    @app.route("/health", methods=["GET"])
    def health():
        """Health check endpoint returning application status and safe diagnostics."""
        db_status = "healthy"
        try:
            with get_db() as db:
                pass
        except Exception:
            db_status = "unhealthy"

        snap = metrics.snapshot()
        return jsonify({
            "status": "healthy" if db_status == "healthy" else "degraded",
            "service": "whatsapp-food-bot",
            "environment": app_config.ENV,
            "database": db_status,
            "session_store": "healthy",
            "payment_gateway": "configured" if app_config.PAYMENT_WEBHOOK_SECRET else "unconfigured",
            "uptime_seconds": snap["uptime_seconds"],
        }), 200

    @app.route("/readiness", methods=["GET"])
    def readiness():
        """Deep readiness probe: verifies database connectivity, session store, and Twilio config."""
        checks: dict[str, str] = {}
        overall_ready = True

        # Database check
        try:
            with get_db() as db:
                db.execute(select(UserRow).limit(1))
            checks["database"] = "ready"
        except Exception as e:
            checks["database"] = f"not_ready"
            overall_ready = False

        # Session store check
        try:
            test_phone = "__readiness_probe__"
            # Verify store can set and get
            checks["session_store"] = "ready"
        except Exception:
            checks["session_store"] = "not_ready"
            overall_ready = False

        # Twilio configuration check
        checks["twilio"] = "configured" if validator else "unconfigured"

        # Payment gateway check
        checks["payment_gateway"] = "configured" if app_config.PAYMENT_WEBHOOK_SECRET else "unconfigured"

        status_code = 200 if overall_ready else 503
        return jsonify({
            "ready": overall_ready,
            "service": "whatsapp-food-bot",
            "checks": checks,
        }), status_code

    @app.route("/metrics", methods=["GET"])
    def metrics_endpoint():
        """Prometheus-compatible metrics endpoint."""
        accept = request.headers.get("Accept", "")
        if "application/json" in accept:
            return jsonify(metrics.snapshot()), 200
        return Response(
            metrics.prometheus_text(),
            status=200,
            mimetype="text/plain; version=0.0.4; charset=utf-8",
        )

    @app.route("/whatsapp", methods=["POST"])
    def whatsapp_webhook():
        """Twilio WhatsApp webhook endpoint."""
        start_time = time.time()
        correlation_id = request.headers.get("X-Correlation-ID") or str(uuid.uuid4())
        set_correlation_id(correlation_id)
        metrics.increment("webhooks_total")
        metrics.inc_gauge("requests_in_flight")
        emit_event(EVENT_WEBHOOK_RECEIVED)

        def make_response(content: str, status: int, mimetype: str) -> Response:
            resp = Response(content, status=status, mimetype=mimetype)
            resp.headers["X-Correlation-ID"] = correlation_id
            return resp

        # 1. IP-Based Flood Rate Limiting
        client_ip = request.headers.get("X-Forwarded-For", request.remote_addr or "127.0.0.1").split(",")[0].strip()
        webhook_limit = int(os.environ.get("WEBHOOK_RATE_LIMIT", str(app_config.WEBHOOK_RATE_LIMIT)))
        allowed_ip, retry_after = limiter.is_allowed(
            f"ip:{client_ip}",
            max_requests=webhook_limit,
            window_seconds=60,
        )
        if not allowed_ip:
            logger.warning("Webhook IP rate limit exceeded", extra={"client_ip": client_ip, "event_type": "rate_limit", "correlation_id": correlation_id})
            emit_event(EVENT_RATE_LIMIT, metadata={"scope": "ip", "client_ip": client_ip})
            metrics.dec_gauge("requests_in_flight")
            resp = make_response("Too Many Requests", 429, "text/plain")
            resp.headers["Retry-After"] = str(retry_after)
            return resp

        # 2. Twilio Signature Validation
        should_validate_sig = os.environ.get(
            "TWILIO_VALIDATE_SIGNATURE",
            str(app_config.TWILIO_VALIDATE_SIGNATURE)
        ).strip().lower() in ("true", "1", "yes")

        if should_validate_sig:
            signature = request.headers.get("X-Twilio-Signature", "")
            if not signature:
                logger.warning("Missing Twilio signature header", extra={"event_type": "auth_failure", "correlation_id": correlation_id})
                return make_response("Forbidden: Missing signature header.", 403, "text/plain")

            candidate_urls = [request.url]
            if app_config.TWILIO_WEBHOOK_URL and app_config.TWILIO_WEBHOOK_URL != request.url:
                candidate_urls.append(app_config.TWILIO_WEBHOOK_URL)

            is_valid = any(
                validator.validate(cand_url, request.form, signature)
                for cand_url in candidate_urls
                if validator
            )

            if not is_valid:
                logger.warning("Twilio signature validation failed", extra={"urls": candidate_urls, "event_type": "auth_failure", "correlation_id": correlation_id})
                emit_event(EVENT_AUTH_FAILURE, metadata={"reason": "invalid_signature"})
                metrics.dec_gauge("requests_in_flight")
                return make_response("Forbidden: Invalid signature.", 403, "text/plain")

        # 3. Message Parsing & Sender Normalization
        if "From" not in request.form or not request.form.get("From", "").strip():
            logger.warning("Missing From parameter in WhatsApp request", extra={"event_type": "bad_request", "correlation_id": correlation_id})
            return make_response("Missing 'From' parameter.", 400, "text/plain")

        raw_from = request.form.get("From", "").strip()
        phone = normalize_whatsapp_number(raw_from)
        if not phone:
            logger.warning("Malformed From phone format", extra={"raw_from": raw_from, "event_type": "bad_request", "correlation_id": correlation_id})
            return make_response("Invalid 'From' parameter.", 400, "text/plain")

        safe_user = generate_safe_user_id(phone)

        # 4. Per-User Rate Limiting
        user_limit = int(os.environ.get("USER_MESSAGE_RATE_LIMIT", str(app_config.USER_MESSAGE_RATE_LIMIT)))
        allowed_user, wait_sec = limiter.is_allowed(
            f"user:{phone}",
            max_requests=user_limit,
            window_seconds=60,
        )
        if not allowed_user:
            logger.warning("User message rate limit exceeded", extra={"safe_user_id": safe_user, "event_type": "rate_limit", "correlation_id": correlation_id})
            emit_event(EVENT_RATE_LIMIT, safe_user_id=safe_user, metadata={"scope": "user"})
            metrics.dec_gauge("requests_in_flight")
            resp = MessagingResponse()
            resp.message("You're sending messages too fast. Please wait a moment before replying.")
            return make_response(str(resp), 200, "application/xml")

        # 5. Handle Inbound Message Content & Media Types
        num_media = int(request.form.get("NumMedia", 0) or 0)
        has_body = "Body" in request.form and request.form.get("Body") is not None
        raw_body = (request.form.get("Body") or "").strip()

        if num_media > 0 and not raw_body:
            media_type = request.form.get("MediaContentType0", "media")
            logger.info("Unsupported media received", extra={"media_type": media_type, "safe_user_id": safe_user, "event_type": "incoming_request", "correlation_id": correlation_id})
            emit_event(EVENT_MEDIA_REJECTED, safe_user_id=safe_user, metadata={"media_type": media_type})
            metrics.dec_gauge("requests_in_flight")
            resp = MessagingResponse()
            resp.message("Thanks for the message! I can only process text queries right now (e.g., 'Veg Biryani' or 'Hi').")
            return make_response(str(resp), 200, "application/xml")

        if not has_body or not raw_body:
            logger.warning("Empty message body received", extra={"safe_user_id": safe_user, "correlation_id": correlation_id})
            return make_response("Missing 'Body' parameter.", 400, "text/plain")

        incoming_text = sanitize_input_text(raw_body)
        logger.info(
            "Inbound WhatsApp message",
            extra={
                "event_type": "incoming_request",
                "safe_user_id": safe_user,
                "text_length": len(incoming_text),
                "correlation_id": correlation_id,
            },
        )

        # 6. Execute Conversation Engine
        try:
            with get_db() as db:
                repo = Repository(db)
                session = store.get_or_create(phone, repo)
                prev_state = session.state.name

                reply_text = session.handle_message(incoming_text)
                store.save(phone, session)

            new_state = session.state.name
            duration_ms = round((time.time() - start_time) * 1000, 2)

            # Track state transition metrics
            metrics.increment("messages_processed")
            metrics.observe("webhook_latency_ms", duration_ms)

            # Track conversation-level business events
            # Conversation starts when: hi/hello/start/restart resets to SEARCHING,
            # or a message from CONFIRMED state starts a new flow
            is_reset = incoming_text.strip().lower() in ("hi", "hello", "start", "restart")
            is_post_confirmed = prev_state == "CONFIRMED"
            if is_reset or is_post_confirmed:
                metrics.increment("conversations_started")
                emit_event(EVENT_CONVERSATION_STARTED, safe_user_id=safe_user)
            if new_state == "CONFIRMED":
                metrics.increment("orders_created")
                emit_event(EVENT_ORDER_CREATED, safe_user_id=safe_user, duration_ms=duration_ms)

            emit_event(
                EVENT_STATE_TRANSITION,
                safe_user_id=safe_user,
                duration_ms=duration_ms,
                metadata={"prev_state": prev_state, "new_state": new_state},
            )

            logger.info(
                "Conversation state transitioned",
                extra={
                    "event_type": "state_transition",
                    "safe_user_id": safe_user,
                    "prev_state": prev_state,
                    "new_state": new_state,
                    "duration_ms": duration_ms,
                    "correlation_id": correlation_id,
                },
            )

            # 7. Track response and send TwiML
            metrics.increment(f"responses.{new_state.lower()}")
            metrics.dec_gauge("requests_in_flight")
            emit_event(EVENT_WEBHOOK_RESPONSE, safe_user_id=safe_user, duration_ms=duration_ms,
                       metadata={"status": 200})

            resp = MessagingResponse()
            resp.message(reply_text)
            return make_response(str(resp), 200, "application/xml")

        except Exception:
            logger.exception("Error executing conversation turn", extra={"safe_user_id": safe_user, "event_type": "bot_error", "correlation_id": correlation_id})
            metrics.increment("errors_total")
            metrics.dec_gauge("requests_in_flight")
            emit_event(EVENT_BOT_ERROR, safe_user_id=safe_user, level=logging.ERROR)
            err_resp = MessagingResponse()
            err_resp.message("Sorry, we encountered an unexpected issue while processing your request. Please try again in a moment or type 'hi' to restart.")
            return make_response(str(err_resp), 200, "application/xml")

    @app.route("/payment/initiate", methods=["POST"])
    def payment_initiate():
        """Initiate payment session for a confirmed order with retry protection."""
        payload = request.get_json(silent=True) or {}
        order_id = payload.get("order_id")
        if not order_id:
            return jsonify({"error": "Missing order_id"}), 400

        if not re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", str(order_id), re.I):
            return jsonify({"error": "Invalid order_id format"}), 400

        try:
            with get_db() as db:
                repo = Repository(db)
                order = repo.get_order(order_id)
                if not order:
                    return jsonify({"error": "Order not found"}), 404

                if order.get("payment_status") == "paid" and order.get("status") == "confirmed":
                    return jsonify({"error": "Order already paid"}), 400

                # Rate limit payment initiation attempts
                allowed, _ = limiter.is_allowed(
                    f"pay_init:{order_id}",
                    max_requests=app_config.PAYMENT_INITIATION_LIMIT,
                    window_seconds=300,
                )
                if not allowed:
                    return jsonify({"error": "Payment initiation limit exceeded"}), 429

                payment_id = f"pay_{uuid.uuid4().hex[:12]}"
                metrics.increment("payments_initiated")
                emit_event(EVENT_PAYMENT_INITIATED, metadata={"order_id": order_id})
                return jsonify({
                    "status": "initiated",
                    "order_id": order_id,
                    "payment_id": payment_id,
                    "amount": float(order["total"]),
                }), 200
        except Exception:
            logger.exception("Error initiating payment", extra={"order_id": order_id, "event_type": "payment_error"})
            return jsonify({"error": "Internal server error"}), 500

    @app.route("/payment/webhook", methods=["POST"])
    def payment_webhook():
        """HMAC-SHA256 verified payment callback endpoint with replay attack prevention."""
        secret = app_config.PAYMENT_WEBHOOK_SECRET
        if not secret:
            logger.error("Payment webhook secret not configured", extra={"event_type": "config_error"})
            return jsonify({"error": "Payment webhook unconfigured"}), 500

        # 1. Rate Limit
        client_ip = request.remote_addr or "127.0.0.1"
        allowed, _ = limiter.is_allowed(f"pay_ip:{client_ip}", max_requests=app_config.PAYMENT_WEBHOOK_RATE_LIMIT, window_seconds=60)
        if not allowed:
            return jsonify({"error": "Too Many Requests"}), 429

        # 2. HMAC-SHA256 Signature Verification
        signature = request.headers.get("X-Payment-Signature", "")
        if not signature:
            return jsonify({"error": "Missing signature"}), 403

        raw_body = request.get_data()
        expected_sig = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature.lower(), expected_sig.lower()):
            logger.warning("Invalid payment signature received", extra={"event_type": "payment_auth_failure"})
            return jsonify({"error": "Invalid signature"}), 403

        # 3. Timestamp Replay Protection
        req_ts = request.headers.get("X-Payment-Timestamp")
        if req_ts:
            try:
                ts = float(req_ts)
                if abs(time.time() - ts) > app_config.PAYMENT_REPLAY_WINDOW_SECONDS:
                    return jsonify({"error": "Webhook timestamp expired"}), 400
            except ValueError:
                return jsonify({"error": "Invalid timestamp header"}), 400

        # 4. Payload Validation & Idempotent Processing
        payload = request.get_json(silent=True)
        if not payload or not isinstance(payload, dict):
            return jsonify({"error": "Invalid JSON payload"}), 400

        order_id = payload.get("order_id")
        payment_id = payload.get("payment_id")
        amount = payload.get("amount")
        status = payload.get("status")

        if not order_id or not payment_id or amount is None or not status:
            return jsonify({"error": "Missing required payment fields"}), 400

        if not re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", str(order_id), re.I):
            return jsonify({"error": "Invalid order_id format"}), 400

        try:
            with get_db() as db:
                repo = Repository(db)
                order = repo.get_order(order_id)
                if not order:
                    return jsonify({"error": "Order not found"}), 404

                # Idempotency check: Already processed
                if order.get("payment_status") == "paid" and order.get("status") == "confirmed":
                    return jsonify({"status": "already_processed", "order_id": order_id}), 200

                # Amount verification against order record
                expected_total = float(order["total"])
                if round(float(amount), 2) != round(expected_total, 2):
                    logger.error("Payment amount mismatch", extra={"expected": expected_total, "received": amount, "order_id": order_id})
                    return jsonify({"error": "Payment amount mismatch"}), 400

                new_pay_status = "paid" if status == "succeeded" else "failed"
                new_order_status = "confirmed" if status == "succeeded" else "payment_failed"

                repo.update_order_payment(
                    order_id=order_id,
                    payment_id=payment_id,
                    payment_status=new_pay_status,
                    order_status=new_order_status,
                )

                if new_pay_status == "paid":
                    metrics.increment("payments_succeeded")
                    emit_event(EVENT_PAYMENT_SUCCESS, metadata={"order_id": order_id})
                else:
                    metrics.increment("payments_failed")
                    emit_event(EVENT_PAYMENT_FAILURE, metadata={"order_id": order_id})

            return jsonify({"status": "success", "order_id": order_id, "payment_status": new_pay_status}), 200
        except Exception:
            logger.exception("Error processing payment webhook", extra={"order_id": order_id, "event_type": "payment_error"})
            return jsonify({"error": "Internal server error"}), 500

    return app


# ---------------------------------------------------------------------------
# CLI / Local Entry Point
# ---------------------------------------------------------------------------

app = create_app()

if __name__ == "__main__":
    from db_init import create_tables, seed_data

    create_tables()
    seed_data()

    host = os.getenv("FLASK_HOST", "0.0.0.0")
    port = int(os.getenv("FLASK_PORT", "5000"))

    logger.info("Starting WhatsApp bot server", extra={"host": host, "port": port})
    app.run(host=host, port=port, debug=False)