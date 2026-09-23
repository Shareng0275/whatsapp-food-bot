"""Domain and infrastructure exception hierarchy for WhatsApp Food Ordering Bot.

Provides structured, domain-specific exceptions to cleanly isolate failure modes
across Twilio, database, Redis, payment gateways, and conversation state transitions.
"""

class BotException(Exception):
    """Base exception for all bot errors."""
    pass


class ConfigurationError(BotException):
    """Raised when environment or secrets are missing or invalid."""
    pass


class DatabaseConnectionError(BotException):
    """Raised when database connection fails or times out."""
    pass


class PaymentGatewayError(BotException):
    """Base class for payment provider issues."""
    pass


class PaymentVerificationError(PaymentGatewayError):
    """Raised when payment webhook signature or amount verification fails."""
    pass


class PaymentTimeoutError(PaymentGatewayError):
    """Raised when payment gateway times out or fails to respond."""
    pass


class InvalidStateTransitionError(BotException):
    """Raised on invalid conversational or order state transitions."""
    pass


class UnauthorizedOrderAccessError(BotException):
    """Raised when accessing an order not owned by the caller."""
    pass
