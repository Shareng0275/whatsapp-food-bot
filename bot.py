"""Productionized, Security-Hardened, Monitored & Rate-Limited Flask/Twilio WhatsApp adapter.

Provides:
- Redis / In-Memory backed Rate Limiting & Abuse Protection
- Structured JSON logging with event types and privacy-safe user identifiers
- Contextual correlation ID tracing across the entire request lifecycle
- Secure health check endpoint with database, session store, and gateway checks
- POST /whatsapp (Twilio messaging webhook with signature validation, rate limiting & sanitization)
- POST /payment/webhook (Payment gateway callback with HMAC-SHA256 verification & idempotency)
- Masked PII in logs to protect user privacy
- Secret redaction filter to prevent credential exposure
"""
import hmac
import hashlib
import os
import re
import time
import uuid
from functools import wraps
from typing import Optional

from flask import Flask, request, Response, jsonify, g
from werkzeug.middleware.proxy_fix import ProxyFix
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse
from sqlalchemy import text

from database import get_db
from repository import Repository
from session_store import get_session_store, SessionStore
from rate_limiter import get_rate_limiter, RateLimiter
from config import Config, get_config, ConfigurationError
from structured_logger import (
    setup_structured_logging,
    get_correlation_id,
    set_correlation_id,
    generate_safe_user_id,
)

# ----------------------------------------------------------------------
# Logging Configuration
# ----------------------------------------------------------------------
logger = setup_structured_logging(level=os.environ.get("LOG_LEVEL", "INFO"))

# ----------------------------------------------------------------------
# Environment & Configuration
# ----------------------------------------------------------------------
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_WHATSAPP_NUMBER = os.environ.get("TWILIO_WHATSAPP_NUMBER", "")
PAYMENT_WEBHOOK_SECRET = os.environ.get("PAYMENT_WEBHOOK_SECRET", "")

WEBHOOK_RATE_LIMIT = int(os.environ.get("WEBHOOK_RATE_LIMIT", "60"))
USER_MESSAGE_RATE_LIMIT = int(os.environ.get("USER_MESSAGE_RATE_LIMIT", "20"))
PAYMENT_WEBHOOK_RATE_LIMIT = int(os.environ.get("PAYMENT_WEBHOOK_RATE_LIMIT", "30"))

PAYMENT_INITIATION_LIMIT = int(os.environ.get("PAYMENT_INITIATION_LIMIT", "3"))
PROMO_ATTEMPT_LIMIT = int(os.environ.get("PROMO_ATTEMPT_LIMIT", "5"))

_validate_env = os.environ.get("TWILIO_VALIDATE_SIGNATURE", "").strip().lower()
if _validate_env in ("true", "1", "yes"):
    VALIDATE_SIGNATURE = True
elif _validate_env in ("false", "0", "no"):
    VALIDATE_SIGNATURE = False
else:
    VALIDATE_SIGNATURE = bool(TWILIO_AUTH_TOKEN)


def validate_environment() -> dict:
    """Validate environment configuration safely without logging secrets."""
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN", TWILIO_AUTH_TOKEN)
    payment_secret = os.environ.get("PAYMENT_WEBHOOK_SECRET", PAYMENT_WEBHOOK_SECRET)
    status = {
        "twilio_configured": bool(auth_token),
        "signature_validation": VALIDATE_SIGNATURE,
        "payment_webhook_configured": bool(payment_secret),
        "database_url_configured": bool(os.environ.get("DATABASE_URL")),
        "webhook_rate_limit": WEBHOOK_RATE_LIMIT,
        "user_message_rate_limit": USER_MESSAGE_RATE_LIMIT,
        "payment_webhook_rate_limit": PAYMENT_WEBHOOK_RATE_LIMIT,
        "payment_initiation_limit": PAYMENT_INITIATION_LIMIT,
        "promo_attempt_limit": PROMO_ATTEMPT_LIMIT,
    }
    if not status["twilio_configured"] and VALIDATE_SIGNATURE:
        logger.warning(
            "Configuration warning: Signature validation enabled but TWILIO_AUTH_TOKEN is missing.",
            extra={"event_type": "config_warning"},
        )
    return status


# ----------------------------------------------------------------------
# Security Helpers: Masking, Normalization & Sanitization
# ----------------------------------------------------------------------
def mask_phone_number(phone: str) -> str:
    """Mask phone number for safe structured logging (e.g. +9198****3210)."""
    if not phone or len(phone) < 7:
        return "***"
    return f"{phone[:5]}****{phone[-4:]}"


def normalize_whatsapp_number(raw_phone: str) -> str:
    """Normalize and validate a WhatsApp phone number.

    Strips 'whatsapp:' prefix, removes whitespace, brackets, dashes,
    and ensures valid E.164 phone structure.
    """
    if not raw_phone:
        return ""
    cleaned = re.sub(r"^whatsapp:", "", raw_phone.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"[\s\(\)\-]", "", cleaned)
    if not re.match(r"^\+?[1-9]\d{6,14}$", cleaned):
        return ""
    if not cleaned.startswith("+"):
        cleaned = "+" + cleaned
    return cleaned


def sanitize_input_text(text: str, max_length: int = 1000) -> str:
    """Sanitize user text: enforce length bounds and strip dangerous control chars."""
    if not text:
        return ""
    sanitized = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    return sanitized[:max_length].strip()


def get_public_request_url() -> str:
    """Reconstruct effective public URL accounting for reverse proxy headers."""
    proto = request.headers.get("X-Forwarded-Proto", request.scheme)
    host = request.headers.get("X-Forwarded-Host", request.host)
    path = request.full_path if request.query_string else request.path
    if path.endswith("?"):
        path = path[:-1]
    return f"{proto}://{host}{path}"


def build_twiml_response(message_text: str) -> Response:
    """Build a safely XML-escaped TwiML response."""
    twiml_resp = MessagingResponse()
    twiml_resp.message(message_text)
    return Response(str(twiml_resp), mimetype="application/xml")


# ----------------------------------------------------------------------
# Twilio Signature Validator Decorator
# ----------------------------------------------------------------------
def validate_twilio_signature(f):
    """Flask view decorator to validate Twilio webhook signatures."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        val_env = os.environ.get("TWILIO_VALIDATE_SIGNATURE", "").strip().lower()
        if val_env in ("false", "0", "no"):
            should_validate = False
        elif val_env in ("true", "1", "yes"):
            should_validate = True
        else:
            should_validate = VALIDATE_SIGNATURE

        if not should_validate:
            return f(*args, **kwargs)

        auth_token = os.environ.get("TWILIO_AUTH_TOKEN", TWILIO_AUTH_TOKEN)
        if not auth_token:
            logger.error(
                "TWILIO_AUTH_TOKEN missing; rejecting webhook request.",
                extra={"event_type": "auth_error", "outcome": "rejected"},
            )
            return Response("Server configuration error: missing auth token.", status=500, mimetype="text/plain")

        signature = request.headers.get("X-Twilio-Signature", "")
        if not signature:
            logger.warning(
                "Missing X-Twilio-Signature header from %s", request.remote_addr,
                extra={"event_type": "signature_missing", "outcome": "rejected"},
            )
            return Response("Forbidden: Missing signature header.", status=403, mimetype="text/plain")

        validator = RequestValidator(auth_token)
        url = get_public_request_url()
        post_params = request.form.to_dict()

        if not validator.validate(url, post_params, signature):
            logger.warning(
                "Invalid Twilio signature from %s for %s", request.remote_addr, url,
                extra={"event_type": "signature_invalid", "outcome": "rejected"},
            )
            return Response("Forbidden: Invalid signature.", status=403, mimetype="text/plain")

        return f(*args, **kwargs)
    return decorated_function


# ----------------------------------------------------------------------
# Application Factory
# ----------------------------------------------------------------------
def create_app(session_store: Optional[SessionStore] = None,
               rate_limiter: Optional[RateLimiter] = None,
               config: Optional[Config] = None) -> Flask:
    """Create and configure the hardened, monitored & rate-limited Flask application."""
    app = Flask(__name__)

    cfg = config or get_config()
    app.config["APP_CONFIG"] = cfg
    app.config["SECRET_KEY"] = cfg.SECRET_KEY

    # Support reverse proxy headers
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1)

    # Initialize session store and rate limiter
    store_type = cfg.SESSION_STORE_TYPE
    redis_url = cfg.REDIS_URL

    app.config["SESSION_STORE"] = session_store or get_session_store(
        store_type=store_type,
        redis_url=redis_url,
    )

    redis_client = getattr(app.config["SESSION_STORE"], "redis", None)
    app.config["RATE_LIMITER"] = rate_limiter or get_rate_limiter(
        store_type=store_type,
        redis_client=redis_client,
    )

    # ------------------------------------------------------------------
    # Request Tracing & Correlation ID Propagation
    # ------------------------------------------------------------------
    @app.before_request
    def before_request():
        cid = request.headers.get("X-Correlation-ID") or str(uuid.uuid4())
        set_correlation_id(cid)
        g.correlation_id = cid
        g.start_time = time.time()

    @app.after_request
    def after_request(response):
        cid = get_correlation_id()
        if cid:
            response.headers["X-Correlation-ID"] = cid
        return response

    # ------------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------------
    @app.route("/", methods=["GET"])
    def index():
        """Interactive Web Chat UI for testing the WhatsApp Bot in the browser."""
        html = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>QuickBite WhatsApp Bot - Web Simulator</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: 'Inter', sans-serif;
      background: #0b141a;
      color: #e9edef;
      height: 100vh;
      display: flex;
      justify-content: center;
      align-items: center;
    }
    .app-container {
      width: 100%;
      max-width: 900px;
      height: 94vh;
      background: #111b21;
      border-radius: 16px;
      display: flex;
      flex-direction: column;
      box-shadow: 0 12px 40px rgba(0,0,0,0.6);
      border: 1px solid #222e35;
      overflow: hidden;
    }
    .header {
      background: #202c33;
      padding: 14px 20px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      border-bottom: 1px solid #2a3942;
    }
    .header-left {
      display: flex;
      align-items: center;
      gap: 14px;
    }
    .avatar {
      width: 44px;
      height: 44px;
      border-radius: 50%;
      background: #00a884;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 22px;
      color: white;
      font-weight: bold;
    }
    .bot-info h2 {
      font-size: 16px;
      font-weight: 600;
      color: #e9edef;
    }
    .bot-info p {
      font-size: 12px;
      color: #00a884;
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .status-dot {
      width: 8px;
      height: 8px;
      background: #00a884;
      border-radius: 50%;
      display: inline-block;
    }
    .badge {
      background: #222e35;
      color: #8696a0;
      font-size: 11px;
      padding: 4px 10px;
      border-radius: 12px;
      border: 1px solid #2a3942;
    }
    .chat-body {
      flex: 1;
      padding: 20px;
      overflow-y: auto;
      background-color: #0b141a;
      background-image: radial-gradient(#202c33 1px, transparent 1px);
      background-size: 20px 20px;
      display: flex;
      flex-direction: column;
      gap: 12px;
    }
    .message {
      max-width: 75%;
      padding: 10px 14px;
      border-radius: 10px;
      font-size: 14px;
      line-height: 1.5;
      white-space: pre-wrap;
      position: relative;
      word-break: break-word;
    }
    .message.bot {
      align-self: flex-start;
      background: #202c33;
      color: #e9edef;
      border-top-left-radius: 2px;
    }
    .message.user {
      align-self: flex-end;
      background: #005c4b;
      color: #e9edef;
      border-top-right-radius: 2px;
    }
    .message-time {
      font-size: 10px;
      color: #8696a0;
      text-align: right;
      margin-top: 4px;
    }
    .quick-replies {
      padding: 8px 16px;
      background: #111b21;
      display: flex;
      gap: 8px;
      overflow-x: auto;
      border-top: 1px solid #222e35;
    }
    .quick-chip {
      background: #202c33;
      color: #00a884;
      border: 1px solid #2a3942;
      padding: 6px 14px;
      border-radius: 16px;
      font-size: 13px;
      cursor: pointer;
      white-space: nowrap;
      transition: all 0.2s;
    }
    .quick-chip:hover {
      background: #00a884;
      color: white;
    }
    .footer {
      background: #202c33;
      padding: 12px 16px;
      display: flex;
      align-items: center;
      gap: 12px;
    }
    .phone-select {
      background: #111b21;
      border: 1px solid #2a3942;
      color: #e9edef;
      padding: 10px 12px;
      border-radius: 8px;
      font-size: 13px;
      outline: none;
    }
    .input-box {
      flex: 1;
      background: #2a3942;
      border: none;
      outline: none;
      color: #e9edef;
      padding: 12px 16px;
      border-radius: 8px;
      font-size: 14px;
    }
    .send-btn {
      background: #00a884;
      border: none;
      color: white;
      padding: 12px 20px;
      border-radius: 8px;
      font-weight: 600;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 6px;
      transition: background 0.2s;
    }
    .send-btn:hover {
      background: #02906f;
    }
  </style>
</head>
<body>
  <div class="app-container">
    <div class="header">
      <div class="header-left">
        <div class="avatar">🍔</div>
        <div class="bot-info">
          <h2>QuickBite WhatsApp Bot</h2>
          <p><span class="status-dot"></span> Online (Production Hardened)</p>
        </div>
      </div>
      <div>
        <a href="/health" target="_blank" style="text-decoration:none;"><span class="badge">API Health: /health</span></a>
      </div>
    </div>

    <div class="chat-body" id="chatBody">
      <div class="message bot">
        👋 Welcome to <b>QuickBite Food Ordering Bot</b>!
Send <b>'hi'</b> or tap a quick action below to start ordering delicious food.
        <div class="message-time">Bot • Online</div>
      </div>
    </div>

    <div class="quick-replies">
      <button class="quick-chip" onclick="sendQuick('hi')">👋 Say Hi</button>
      <button class="quick-chip" onclick="sendQuick('Veg Biryani')">🍛 Veg Biryani</button>
      <button class="quick-chip" onclick="sendQuick('1')">1️⃣ Select #1</button>
      <button class="quick-chip" onclick="sendQuick('same')">📍 Use Saved Address</button>
      <button class="quick-chip" onclick="sendQuick('SAVE15')">🎟️ Apply SAVE15</button>
      <button class="quick-chip" onclick="sendQuick('skip')">⏭️ Skip Promo</button>
    </div>

    <div class="footer">
      <select id="userPhone" class="phone-select" title="Simulated User Phone">
        <option value="+919900011122">Asha (+919900011122)</option>
        <option value="+919876543210">New User (+919876543210)</option>
      </select>
      <input type="text" id="userInput" class="input-box" placeholder="Type a message (e.g. 'hi', 'biryani', '1', 'skip')..." onkeydown="if(event.key==='Enter') sendMessage()" autofocus />
      <button class="send-btn" onclick="sendMessage()">Send ➔</button>
    </div>
  </div>

  <script>
    const chatBody = document.getElementById('chatBody');
    const userInput = document.getElementById('userInput');
    const userPhone = document.getElementById('userPhone');

    function appendMessage(text, isUser) {
      const msgDiv = document.createElement('div');
      msgDiv.className = 'message ' + (isUser ? 'user' : 'bot');
      const time = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
      msgDiv.innerHTML = text.replace(/\\n/g, '<br>') + '<div class="message-time">' + time + (isUser ? ' ✓✓' : '') + '</div>';
      chatBody.appendChild(msgDiv);
      chatBody.scrollTop = chatBody.scrollHeight;
    }

    async function sendMessage() {
      const text = userInput.value.trim();
      if (!text) return;
      userInput.value = '';
      appendMessage(text, true);

      try {
        const phone = userPhone.value;
        const res = await fetch('/api/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ phone: phone, message: text })
        });
        const data = await res.json();
        appendMessage(data.reply || 'No response from server.', false);
      } catch (err) {
        appendMessage('⚠️ Error communicating with server: ' + err.message, false);
      }
    }

    function sendQuick(val) {
      userInput.value = val;
      sendMessage();
    }
  </script>
</body>
</html>"""
        return Response(html, mimetype="text/html")

    @app.route("/api/chat", methods=["POST"])
    def web_chat():
        """Lightweight API endpoint for the web chat simulator UI."""
        data = request.get_json(silent=True) or {}
        phone = data.get("phone", "+919900011122")
        raw_msg = sanitize_input_text(data.get("message", ""))
        if not raw_msg:
            return jsonify({"reply": "Please type a message."})

        store: SessionStore = app.config["SESSION_STORE"]
        with get_db() as db:
            repo = Repository(db)
            session = store.get_or_create(phone, repo)
            reply = session.handle_message(raw_msg)
            store.save(phone, session)

        return jsonify({
            "phone": phone,
            "state": session.state.name,
            "reply": reply,
        })

    @app.route("/health", methods=["GET"])
    def health():
        """Health check endpoint checking database, session store, and gateway dependencies."""
        db_status = "healthy"
        try:
            with get_db() as db:
                db.execute(text("SELECT 1"))
        except Exception as e:
            logger.warning("Health check detected DB issue: %s", e, extra={"event_type": "health_check_db_failure"})
            db_status = "unhealthy"

        store_status = "healthy"
        try:
            store: SessionStore = app.config["SESSION_STORE"]
            if hasattr(store, "redis") and store.redis:
                store.redis.ping()
        except Exception:
            store_status = "degraded"

        is_healthy = (db_status == "healthy")
        status_code = 200 if is_healthy else 503

        return jsonify({
            "status": "healthy" if is_healthy else "degraded",
            "service": "whatsapp-food-bot",
            "database": db_status,
            "session_store": store_status,
            "payment_gateway": "configured" if os.environ.get("PAYMENT_WEBHOOK_SECRET") else "unconfigured",
            "environment": os.environ.get("FLASK_ENV", "production"),
        }), status_code

    @app.route("/whatsapp", methods=["POST"])
    @validate_twilio_signature
    def whatsapp_webhook():
        """Twilio WhatsApp messaging webhook endpoint with multi-tier rate limiting."""
        cid = get_correlation_id()
        limiter: RateLimiter = app.config["RATE_LIMITER"]

        # 1. IP-Level Flood Protection Rate Limit
        webhook_limit = int(os.environ.get("WEBHOOK_RATE_LIMIT", str(WEBHOOK_RATE_LIMIT)))
        allowed_ip, retry_after_ip = limiter.is_allowed(
            f"ip:{request.remote_addr}",
            max_requests=webhook_limit,
            window_seconds=60,
        )
        if not allowed_ip:
            logger.warning(
                "Webhook rate limit exceeded for IP %s", request.remote_addr,
                extra={"event_type": "ip_rate_limit_exceeded", "outcome": "rejected"},
            )
            return Response("Too Many Requests", status=429, headers={"Retry-After": str(retry_after_ip)}, mimetype="text/plain")

        # 2. Validate input parameters
        raw_from = request.form.get("From")
        if raw_from is None or not raw_from.strip():
            logger.warning("Rejected webhook: Missing 'From' parameter.", extra={"event_type": "validation_error", "outcome": "rejected"})
            return Response("Bad Request: Missing 'From' parameter.", status=400, mimetype="text/plain")

        if "Body" not in request.form:
            logger.warning("Rejected webhook: Missing 'Body' parameter.", extra={"event_type": "validation_error", "outcome": "rejected"})
            return Response("Bad Request: Missing 'Body' parameter.", status=400, mimetype="text/plain")

        phone = normalize_whatsapp_number(raw_from)
        if not phone:
            logger.warning("Rejected webhook: Invalid 'From' phone format.", extra={"event_type": "validation_error", "outcome": "rejected"})
            return Response("Bad Request: Invalid 'From' parameter.", status=400, mimetype="text/plain")

        safe_user_id = generate_safe_user_id(phone)

        # 3. Per-User Message Rate Limit
        user_limit = int(os.environ.get("USER_MESSAGE_RATE_LIMIT", str(USER_MESSAGE_RATE_LIMIT)))
        allowed_user, retry_after_user = limiter.is_allowed(
            f"user:{phone}",
            max_requests=user_limit,
            window_seconds=60,
        )
        if not allowed_user:
            logger.warning(
                "User message rate limit exceeded for %s", safe_user_id,
                extra={"event_type": "user_rate_limit_exceeded", "safe_user_id": safe_user_id, "outcome": "rate_limited"},
            )
            return build_twiml_response("You're sending messages too fast. Please wait a moment before replying.")

        raw_body = sanitize_input_text(request.form.get("Body", ""))

        logger.info(
            "Incoming WhatsApp message received (length: %d chars)",
            len(raw_body),
            extra={
                "event_type": "incoming_request",
                "safe_user_id": safe_user_id,
                "correlation_id": cid,
            },
        )

        try:
            store: SessionStore = app.config["SESSION_STORE"]

            with get_db() as db:
                repo = Repository(db)
                session = store.get_or_create(phone, repo)
                prev_state = session.state.name

                reply_text = session.handle_message(raw_body)
                store.save(phone, session)
                new_state = session.state.name

                duration_ms = (time.time() - g.start_time) * 1000

                logger.info(
                    "State transition: %s -> %s (handled in %.1fms)",
                    prev_state, new_state, duration_ms,
                    extra={
                        "event_type": "state_transition",
                        "safe_user_id": safe_user_id,
                        "prev_state": prev_state,
                        "new_state": new_state,
                        "order_id": getattr(session.order, "order_id", None),
                        "duration_ms": round(duration_ms, 1),
                        "outcome": "success",
                    },
                )

                if session.order and session.order.order_id and new_state == "CONFIRMED":
                    logger.info(
                        "Order confirmed: %s for amount ₹%.2f",
                        session.order.order_id, session.order.total,
                        extra={
                            "event_type": "order_confirmed",
                            "order_id": session.order.order_id,
                            "safe_user_id": safe_user_id,
                            "amount": session.order.total,
                            "promo_code": session.order.promo.code if session.order.promo else None,
                        },
                    )

            return build_twiml_response(reply_text)

        except Exception:
            logger.exception(
                "Unexpected exception in whatsapp_webhook for %s",
                safe_user_id,
                extra={
                    "event_type": "internal_error",
                    "safe_user_id": safe_user_id,
                    "error_category": "application_exception",
                    "outcome": "error",
                },
            )
            fallback_message = (
                "Sorry, we encountered an unexpected issue while processing your request. "
                "Please try again in a moment or type 'hi' to start over."
            )
            return build_twiml_response(fallback_message)

    @app.route("/payment/initiate", methods=["POST"])
    def initiate_payment():
        """Initiate payment for an order with abuse protection, retry limits, and duplicate payment prevention."""
        cid = get_correlation_id()
        limiter: RateLimiter = app.config["RATE_LIMITER"]
        init_limit = int(os.environ.get("PAYMENT_INITIATION_LIMIT", str(PAYMENT_INITIATION_LIMIT)))

        data = request.get_json(silent=True)
        if not data or not isinstance(data, dict):
            return jsonify({"error": "Invalid JSON payload"}), 400

        order_id = data.get("order_id")
        if not order_id:
            return jsonify({"error": "Missing order_id"}), 400

        if not re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", str(order_id), re.I):
            return jsonify({"error": "Invalid order_id format"}), 400

        # Rate Limit / Abuse check per order_id
        allowed, retry_after = limiter.is_allowed(
            f"pay_init:{order_id}",
            max_requests=init_limit,
            window_seconds=300,
        )
        if not allowed:
            logger.warning(
                "Payment initiation limit exceeded for order %s", order_id,
                extra={"event_type": "payment_initiation_limit_exceeded", "order_id": order_id, "outcome": "rate_limited"}
            )
            return jsonify({
                "error": "Payment initiation limit exceeded for this order. Please retry later or contact support.",
                "retry_after": retry_after
            }), 429

        try:
            with get_db() as db:
                repo = Repository(db)
                order = repo.get_order(order_id)
                if not order:
                    return jsonify({"error": "Order not found"}), 404

                safe_user_id = generate_safe_user_id(order.get("user_phone", ""))

                # Prevent re-charging an already paid order
                if order.get("payment_status") == "paid" and order.get("status") == "confirmed":
                    logger.warning(
                        "Attempted payment initiation for already paid order %s", order_id,
                        extra={"event_type": "payment_already_completed", "order_id": order_id, "safe_user_id": safe_user_id}
                    )
                    return jsonify({"error": "Order already paid. Cannot initiate a new payment."}), 400

                payment_id = f"pay_{uuid.uuid4().hex[:12]}"
                # Update payment status to pending if not already
                repo.update_order_payment(
                    order_id=order_id,
                    payment_id=payment_id,
                    payment_status="pending",
                    order_status=order.get("status", "pending")
                )

                logger.info(
                    "Payment initiated for order %s (payment_id: %s, amount: ₹%.2f)",
                    order_id, payment_id, float(order["total"]),
                    extra={
                        "event_type": "payment_initiated",
                        "order_id": order_id,
                        "payment_id": payment_id,
                        "safe_user_id": safe_user_id,
                        "amount": float(order["total"]),
                        "outcome": "success",
                    }
                )

                return jsonify({
                    "status": "initiated",
                    "order_id": order_id,
                    "payment_id": payment_id,
                    "amount": float(order["total"]),
                    "currency": "INR",
                }), 200

        except Exception:
            logger.exception("Error initiating payment for order %s", order_id, extra={"event_type": "payment_initiation_error", "order_id": order_id})
            return jsonify({"error": "Internal server error"}), 500

    @app.route("/payment/webhook", methods=["POST"])
    def payment_webhook():
        """Payment gateway callback webhook with HMAC-SHA256 verification, rate limiting, and idempotency."""
        cid = get_correlation_id()
        limiter: RateLimiter = app.config["RATE_LIMITER"]

        # IP Rate Limit for Payment Webhook Callbacks
        pay_limit = int(os.environ.get("PAYMENT_WEBHOOK_RATE_LIMIT", str(PAYMENT_WEBHOOK_RATE_LIMIT)))
        allowed_ip, retry_after = limiter.is_allowed(
            f"pay_ip:{request.remote_addr}",
            max_requests=pay_limit,
            window_seconds=60,
        )
        if not allowed_ip:
            logger.warning("Payment webhook rate limit exceeded from %s", request.remote_addr, extra={"event_type": "payment_rate_limit_exceeded"})
            return jsonify({"error": "Too Many Requests"}), 429

        secret = os.environ.get("PAYMENT_WEBHOOK_SECRET", PAYMENT_WEBHOOK_SECRET)
        if not secret:
            logger.error("PAYMENT_WEBHOOK_SECRET not configured.", extra={"event_type": "config_error", "outcome": "rejected"})
            return jsonify({"error": "Payment webhook unconfigured"}), 500

        # 1. Verify HMAC-SHA256 Signature
        signature = request.headers.get("X-Payment-Signature", "")
        if not signature:
            logger.warning("Missing X-Payment-Signature header.", extra={"event_type": "payment_signature_missing", "outcome": "rejected"})
            return jsonify({"error": "Missing signature"}), 403

        raw_body = request.get_data()
        expected_sig = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()

        if not hmac.compare_digest(signature.lower(), expected_sig.lower()):
            logger.warning("Invalid payment webhook signature.", extra={"event_type": "payment_signature_invalid", "outcome": "rejected"})
            return jsonify({"error": "Invalid signature"}), 403

        # 2. Replay Protection: Check timestamp tolerance (300 seconds)
        req_timestamp = request.headers.get("X-Payment-Timestamp")
        if req_timestamp:
            try:
                ts = float(req_timestamp)
                if abs(time.time() - ts) > 300:
                    logger.warning("Payment webhook timestamp expired.", extra={"event_type": "payment_timestamp_expired", "outcome": "rejected"})
                    return jsonify({"error": "Webhook timestamp expired"}), 400
            except ValueError:
                return jsonify({"error": "Invalid timestamp header"}), 400

        # 3. Validate JSON payload
        data = request.get_json(silent=True)
        if not data or not isinstance(data, dict):
            return jsonify({"error": "Invalid JSON payload"}), 400

        order_id = data.get("order_id")
        payment_id = data.get("payment_id")
        amount = data.get("amount")
        status = data.get("status")

        if not order_id or not payment_id or amount is None or not status:
            return jsonify({"error": "Missing required payment fields"}), 400

        if not re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", str(order_id), re.I):
            return jsonify({"error": "Invalid order_id format"}), 400

        # 4. Idempotency and Amount Verification against Database
        try:
            with get_db() as db:
                repo = Repository(db)
                order = repo.get_order(order_id)
                if not order:
                    logger.warning("Payment webhook referenced non-existent order %s", order_id, extra={"event_type": "payment_order_not_found", "order_id": order_id})
                    return jsonify({"error": "Order not found"}), 404

                safe_user_id = generate_safe_user_id(order.get("user_phone", ""))

                # Idempotency: Check if already confirmed & paid
                if order.get("payment_status") == "paid" and order.get("status") == "confirmed":
                    logger.info(
                        "Duplicate payment webhook for order %s already processed.",
                        order_id,
                        extra={
                            "event_type": "payment_idempotent_skipped",
                            "order_id": order_id,
                            "payment_id": payment_id,
                            "safe_user_id": safe_user_id,
                        },
                    )
                    return jsonify({"status": "already_processed", "order_id": order_id}), 200

                # Verify amount matches backend order total
                expected_total = float(order["total"])
                if round(float(amount), 2) != round(expected_total, 2):
                    logger.error(
                        "Amount mismatch for order %s: expected ₹%.2f, received ₹%.2f",
                        order_id, expected_total, float(amount),
                        extra={
                            "event_type": "payment_amount_mismatch",
                            "order_id": order_id,
                            "amount": float(amount),
                            "outcome": "rejected",
                        },
                    )
                    return jsonify({"error": "Payment amount mismatch"}), 400

                # Process status
                new_pay_status = "paid" if status == "succeeded" else "failed"
                new_order_status = "confirmed" if status == "succeeded" else "payment_failed"

                repo.update_order_payment(
                    order_id=order_id,
                    payment_id=payment_id,
                    payment_status=new_pay_status,
                    order_status=new_order_status,
                )

                logger.info(
                    "Payment verified for order %s (payment_id: %s, amount: ₹%.2f)",
                    order_id, payment_id, expected_total,
                    extra={
                        "event_type": "payment_verified",
                        "order_id": order_id,
                        "payment_id": payment_id,
                        "safe_user_id": safe_user_id,
                        "amount": expected_total,
                        "new_state": new_order_status,
                        "outcome": "success",
                    },
                )
                return jsonify({
                    "status": "success",
                    "order_id": order_id,
                    "payment_status": new_pay_status,
                }), 200

        except Exception:
            logger.exception(
                "Error processing payment webhook for order %s",
                order_id,
                extra={"event_type": "payment_error", "order_id": order_id, "outcome": "error"},
            )
            return jsonify({"error": "Internal server error"}), 500

    return app


app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
