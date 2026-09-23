"""Real WhatsApp entry point for the food-ordering bot."""

import json
import logging
import os

from dotenv import load_dotenv
from flask import Flask, Response, request
from twilio.request_validator import RequestValidator  # type: ignore
from twilio.rest import Client  # type: ignore
from twilio.twiml.messaging_response import MessagingResponse  # type: ignore

from conversation import Session
from database import get_db
from repository import Repository


# ---------------------------------------------------------
# Load environment variables
# ---------------------------------------------------------

load_dotenv()


TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")

TWILIO_WHATSAPP_NUMBER = os.getenv(
    "TWILIO_WHATSAPP_NUMBER",
    "whatsapp:+17372508034",
)

# Required for the current Twilio WhatsApp trial.
TWILIO_CONTENT_SID = os.getenv("TWILIO_CONTENT_SID", "").strip()

# Set this to true/false in .env
TWILIO_VALIDATE_SIGNATURE = (
    os.getenv("TWILIO_VALIDATE_SIGNATURE", "true").lower()
    == "true"
)

TWILIO_WEBHOOK_URL = os.getenv("TWILIO_WEBHOOK_URL", "").strip()


# ---------------------------------------------------------
# Validation
# ---------------------------------------------------------

if not TWILIO_ACCOUNT_SID:
    raise RuntimeError("TWILIO_ACCOUNT_SID is missing from .env")

if not TWILIO_AUTH_TOKEN:
    raise RuntimeError("TWILIO_AUTH_TOKEN is missing from .env")


# ---------------------------------------------------------
# Twilio client
# ---------------------------------------------------------

twilio_client = Client(
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN,
)

request_validator = RequestValidator(
    TWILIO_AUTH_TOKEN
)


# ---------------------------------------------------------
# Flask app
# ---------------------------------------------------------

app = Flask(__name__)

logging.basicConfig(level=logging.INFO)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------
# Sessions
# ---------------------------------------------------------

# One conversation session per WhatsApp user.
SESSIONS: dict[str, Session] = {}


def get_session(phone: str, repo: Repository) -> Session:
    """Get or create the conversation session for a user."""

    if phone not in SESSIONS:
        SESSIONS[phone] = Session(phone, repo)

    return SESSIONS[phone]


# ---------------------------------------------------------
# Helpers
# ---------------------------------------------------------

def normalize_whatsapp_number(number: str) -> str:
    """Make sure a WhatsApp number has the whatsapp: prefix."""

    number = (number or "").strip()

    if number.startswith("whatsapp:"):
        return number

    return f"whatsapp:{number}"


def validate_twilio_request() -> bool:
    """
    Validate that the incoming webhook really came from Twilio.

    Validation can be disabled in .env for local debugging:
        TWILIO_VALIDATE_SIGNATURE=false
    """

    if not TWILIO_VALIDATE_SIGNATURE:
        logger.warning(
            "Twilio signature validation is disabled."
        )
        return True

    signature = request.headers.get(
        "X-Twilio-Signature",
        "",
    )

    if not signature:
        logger.warning(
            "Missing X-Twilio-Signature header."
        )
        return False

    # IMPORTANT:
    # Use the externally visible HTTPS URL that Twilio called.
    url = TWILIO_WEBHOOK_URL or request.url

    return request_validator.validate(
        url,
        request.form,
        signature,
    )


def send_trial_whatsapp_message(
    to_whatsapp: str,
    reply_text: str,
):
    """
    Send a WhatsApp message using Twilio's current
    trial Content Template API.

    IMPORTANT:
    The selected Twilio template must contain {{1}}
    if we want to put reply_text into the message.
    """

    if not TWILIO_CONTENT_SID:
        raise RuntimeError(
            "TWILIO_CONTENT_SID is missing from .env. "
            "Open Twilio Try out WhatsApp, select the "
            "template, and copy its ContentSid (HX...)."
        )

    from_whatsapp = normalize_whatsapp_number(
        TWILIO_WHATSAPP_NUMBER
    )

    to_whatsapp = normalize_whatsapp_number(
        to_whatsapp
    )

    # We use {{1}} in the Twilio template for the
    # bot's generated response.
    content_variables = json.dumps(
        {
            "1": reply_text
        }
    )

    logger.info(
        "Sending WhatsApp template | from=%s | to=%s | "
        "content_sid=%s | variables=%s",
        from_whatsapp,
        to_whatsapp,
        TWILIO_CONTENT_SID,
        content_variables,
    )

    sent_msg = twilio_client.messages.create(
        from_=from_whatsapp,
        to=to_whatsapp,
        content_sid=TWILIO_CONTENT_SID,
        content_variables=content_variables,
    )

    logger.info(
        "Twilio message sent | sid=%s | status=%s",
        sent_msg.sid,
        sent_msg.status,
    )

    return sent_msg


# ---------------------------------------------------------
# Health check
# ---------------------------------------------------------

@app.route("/health", methods=["GET"])
def health():
    return {
        "status": "ok",
        "service": "whatsapp-food-ordering-bot",
    }, 200


# ---------------------------------------------------------
# WhatsApp webhook
# ---------------------------------------------------------

@app.route("/whatsapp", methods=["POST"])
def whatsapp_webhook():

    try:

        # ---------------------------------------------
        # Verify Twilio
        # ---------------------------------------------

        if not validate_twilio_request():

            logger.warning(
                "Rejected request because Twilio "
                "signature validation failed."
            )

            return Response(
                "Invalid Twilio signature",
                status=403,
                mimetype="text/plain",
            )

        # ---------------------------------------------
        # Read incoming WhatsApp message
        # ---------------------------------------------

        from_number = request.form.get(
            "From",
            "",
        ).strip()

        incoming_text = request.form.get(
            "Body",
            "",
        ).strip()

        if not from_number:
            logger.warning(
                "Missing WhatsApp sender number."
            )

            return Response(
                "Missing From",
                status=400,
                mimetype="text/plain",
            )

        phone = from_number.replace(
            "whatsapp:",
            "",
        ).strip()

        logger.info(
            "WhatsApp message received | phone=%s | text=%s",
            phone,
            incoming_text,
        )

        # ---------------------------------------------
        # Run your existing food-order conversation
        # ---------------------------------------------

        with get_db() as db:

            repo = Repository(db)

            session = get_session(
                phone,
                repo,
            )

            reply_text = session.handle_message(
                incoming_text
            )

        logger.info(
            "WhatsApp response generated | phone=%s | reply=%s",
            phone,
            reply_text,
        )

        # ---------------------------------------------
        # Return TwiML MessagingResponse
        # ---------------------------------------------
        resp = MessagingResponse()
        resp.message(reply_text)

        logger.info(
            "TwiML response generated for %s | text: %s",
            phone,
            reply_text[:60],
        )

        return Response(
            str(resp),
            status=200,
            mimetype="application/xml",
        )

    except Exception:
        logger.exception(
            "WhatsApp webhook error"
        )
        err_resp = MessagingResponse()
        err_resp.message("Sorry, something went wrong. Type 'hi' to restart.")
        return Response(
            str(err_resp),
            status=200,
            mimetype="application/xml",
        )


# ---------------------------------------------------------
# Start Flask
# ---------------------------------------------------------

if __name__ == "__main__":

    # Initialize database tables and seed data.
    from db_init import create_tables, seed_data

    create_tables()
    seed_data()

    host = os.getenv(
        "FLASK_HOST",
        "0.0.0.0",
    )

    port = int(
        os.getenv(
            "FLASK_PORT",
            "5000",
        )
    )

    logger.info(
        "Starting WhatsApp bot on %s:%s",
        host,
        port,
    )

    app.run(
        host=host,
        port=port,
        debug=False,
    )