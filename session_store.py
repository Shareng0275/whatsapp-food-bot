"""Session store abstraction for conversational state.

Allows swapping the default in-memory session store for Redis or another
distributed backend in production without modifying the conversation engine or
webhook routing logic.
"""
from abc import ABC, abstractmethod
import json
import logging
import threading
from typing import Optional, Any

from conversation import Session, State
from models import Order, MenuItem, PromoCode

logger = logging.getLogger("whatsapp_bot.session_store")


class SessionStore(ABC):
    """Abstract base class for conversation session persistence."""

    @abstractmethod
    def get(self, phone: str, repo: Any) -> Optional[Session]:
        """Retrieve an existing session for phone number, or None if not found."""
        pass

    @abstractmethod
    def get_or_create(self, phone: str, repo: Any) -> Session:
        """Get an existing session or initialize a new one for phone number."""
        pass

    @abstractmethod
    def save(self, phone: str, session: Session) -> None:
        """Persist session state for phone number."""
        pass

    @abstractmethod
    def delete(self, phone: str) -> None:
        """Delete session state for phone number."""
        pass

    @abstractmethod
    def clear(self) -> None:
        """Clear all sessions (primarily for testing/maintenance)."""
        pass


class InMemorySessionStore(SessionStore):
    """Thread-safe in-memory session store."""

    def __init__(self):
        self._sessions: dict[str, Session] = {}
        self._lock = threading.RLock()

    def get(self, phone: str, repo: Any) -> Optional[Session]:
        with self._lock:
            session = self._sessions.get(phone)
            if session is not None:
                session.repo = repo
            return session

    def get_or_create(self, phone: str, repo: Any) -> Session:
        with self._lock:
            session = self._sessions.get(phone)
            if session is None:
                session = Session(phone, repo)
                self._sessions[phone] = session
            else:
                session.repo = repo
            return session

    def save(self, phone: str, session: Session) -> None:
        with self._lock:
            self._sessions[phone] = session

    def delete(self, phone: str) -> None:
        with self._lock:
            self._sessions.pop(phone, None)

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()


class RedisSessionStore(SessionStore):
    """Redis-backed session store for multi-process / clustered production deployments.

    Serializes conversation state (State enum, Order, item, address, promo) into JSON.
    Reconstructs the Session object upon retrieval with the active request repository.
    Includes in-memory fallback if Redis connection fails or times out.
    """

    def __init__(self, redis_client: Any, ttl_seconds: int = 86400, key_prefix: str = "wa_session:"):
        self.redis = redis_client
        self.ttl_seconds = ttl_seconds
        self.key_prefix = key_prefix
        self._fallback_memory: dict[str, Session] = {}
        self._lock = threading.RLock()

    def _key(self, phone: str) -> str:
        return f"{self.key_prefix}{phone}"

    def _serialize(self, session: Session) -> str:
        payload = {
            "phone": session.phone,
            "state": session.state.name,
            "order": {
                "user_phone": session.order.user_phone,
                "address": session.order.address,
                "order_id": session.order.order_id,
                "payment_id": getattr(session.order, "payment_id", None),
                "payment_status": getattr(session.order, "payment_status", "pending"),
                "status": getattr(session.order, "status", "confirmed"),
                "item": {
                    "item_id": session.order.item.item_id,
                    "name": session.order.item.name,
                    "restaurant": session.order.item.restaurant,
                    "rating": session.order.item.rating,
                    "price": session.order.item.price,
                    "eta_minutes": session.order.item.eta_minutes,
                    "tags": session.order.item.tags,
                } if session.order.item else None,
                "promo": {
                    "code": session.order.promo.code,
                    "description": session.order.promo.description,
                    "discount_pct": session.order.promo.discount_pct,
                    "fixed_discount": getattr(session.order.promo, "fixed_discount", 0.0),
                    "min_order_value": session.order.promo.min_order_value,
                } if session.order.promo else None,
            },
            "last_options": [
                {
                    "item_id": it.item_id,
                    "name": it.name,
                    "restaurant": it.restaurant,
                    "rating": it.rating,
                    "price": it.price,
                    "eta_minutes": it.eta_minutes,
                    "tags": it.tags,
                }
                for it in session._last_options
            ],
        }
        return json.dumps(payload)

    def _deserialize(self, raw_data: str, repo: Any) -> Session:
        data = json.loads(raw_data)
        phone = data["phone"]
        session = Session(phone, repo)
        session.state = State[data["state"]]

        # Reconstruct last options
        session._last_options = [
            MenuItem(**opt) for opt in data.get("last_options", [])
        ]

        # Reconstruct order
        order_data = data.get("order", {})
        order = Order(user_phone=order_data.get("user_phone", phone))
        order.address = order_data.get("address", "")
        order.order_id = order_data.get("order_id")
        order.payment_id = order_data.get("payment_id")
        order.payment_status = order_data.get("payment_status", "pending")
        order.status = order_data.get("status", "confirmed")

        if order_data.get("item"):
            order.item = MenuItem(**order_data["item"])
        if order_data.get("promo"):
            order.promo = PromoCode(**order_data["promo"])

        session.order = order
        return session

    def get(self, phone: str, repo: Any) -> Optional[Session]:
        try:
            raw = self.redis.get(self._key(phone))
            if raw:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                return self._deserialize(raw, repo)
        except Exception as e:
            logger.warning("Redis get failed for %s (%s); falling back to in-memory cache.", phone, e)
            with self._lock:
                session = self._fallback_memory.get(phone)
                if session is not None:
                    session.repo = repo
                return session
        return None

    def get_or_create(self, phone: str, repo: Any) -> Session:
        session = self.get(phone, repo)
        if session is None:
            session = Session(phone, repo)
            self.save(phone, session)
        return session

    def save(self, phone: str, session: Session) -> None:
        try:
            serialized = self._serialize(session)
            self.redis.setex(self._key(phone), self.ttl_seconds, serialized)
        except Exception as e:
            logger.warning("Redis setex failed for %s (%s); saving to fallback memory.", phone, e)
            with self._lock:
                self._fallback_memory[phone] = session

    def delete(self, phone: str) -> None:
        try:
            self.redis.delete(self._key(phone))
        except Exception as e:
            logger.warning("Redis delete failed for %s: %s", phone, e)
        with self._lock:
            self._fallback_memory.pop(phone, None)

    def clear(self) -> None:
        try:
            for key in self.redis.scan_iter(f"{self.key_prefix}*"):
                self.redis.delete(key)
        except Exception as e:
            logger.warning("Redis clear failed: %s", e)
        with self._lock:
            self._fallback_memory.clear()


def get_session_store(store_type: str = "memory", **kwargs) -> SessionStore:
    """Factory to instantiate the appropriate SessionStore."""
    if store_type == "redis":
        try:
            import redis
            redis_url = kwargs.get("redis_url", "redis://localhost:6379/0")
            client = redis.from_url(redis_url)
            return RedisSessionStore(client, **{k: v for k, v in kwargs.items() if k != "redis_url"})
        except ImportError:
            raise RuntimeError("Redis package not installed. Run 'pip install redis' to use RedisSessionStore.")
    return InMemorySessionStore()
