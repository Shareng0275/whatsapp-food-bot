"""Repository layer — translates between SQLAlchemy rows and domain dataclasses.

All database access from the conversation engine and transport layers goes
through this class.  ``recommender.py`` and ``models.py`` never import
SQLAlchemy.

Error hierarchy
---------------
    RepositoryError          — base for all repo errors
    ├── NotFoundError        — record does not exist
    └── DuplicateError       — unique-constraint violation
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session as SASession
from sqlalchemy.exc import IntegrityError
from sqlalchemy import select

from models import MenuItem, User, PromoCode, Order
from db_models import UserRow, MenuItemRow, PromoCodeRow, OrderRow


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class RepositoryError(Exception):
    """Base class for repository errors."""


class NotFoundError(RepositoryError):
    """Requested record does not exist."""


class DuplicateError(RepositoryError):
    """A record with the same unique key already exists."""


# ---------------------------------------------------------------------------
# Converters: SQLAlchemy row  ↔  domain dataclass
# ---------------------------------------------------------------------------

def _row_to_user(row: UserRow) -> User:
    past = [s for s in row.past_item_ids.split(",") if s] if row.past_item_ids else []
    return User(
        phone=row.phone,
        name=row.name,
        past_item_ids=past,
        default_address=row.default_address,
    )


def _user_to_row_dict(user: User) -> dict:
    return {
        "phone": user.phone,
        "name": user.name,
        "past_item_ids": ",".join(user.past_item_ids),
        "default_address": user.default_address,
    }


def _row_to_menu_item(row: MenuItemRow) -> MenuItem:
    tags = [t for t in row.tags.split(",") if t] if row.tags else []
    return MenuItem(
        item_id=row.item_id,
        name=row.name,
        restaurant=row.restaurant,
        rating=row.rating,
        price=row.price,
        eta_minutes=row.eta_minutes,
        tags=tags,
    )


def _row_to_promo(row: PromoCodeRow) -> PromoCode:
    return PromoCode(
        code=row.code,
        description=row.description,
        discount_type=row.discount_type,
        discount_value=row.discount_value,
        min_order_value=row.min_order_value,
    )


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------

class Repository:
    """Provides all data-access operations used by the conversation engine.

    Accepts a SQLAlchemy ``Session`` so the caller controls the lifecycle
    (useful for testing with an in-memory SQLite database).
    """

    def __init__(self, db: SASession):
        self._db = db

    # -- Users ---------------------------------------------------------------

    def get_user(self, phone: str) -> User | None:
        """Return the domain User for *phone*, or ``None`` if not found."""
        row = self._db.get(UserRow, phone)
        return _row_to_user(row) if row else None

    def create_user(self, phone: str, name: str = "Guest",
                    default_address: str | None = None) -> User:
        """Insert a new user.  Raises ``DuplicateError`` if phone exists."""
        row = UserRow(phone=phone, name=name, past_item_ids="",
                      default_address=default_address)
        self._db.add(row)
        try:
            self._db.flush()
        except IntegrityError as exc:
            self._db.rollback()
            raise DuplicateError(f"User {phone} already exists") from exc
        return _row_to_user(row)

    def get_or_create_user(self, phone: str) -> User:
        """Get existing user or create a Guest user."""
        user = self.get_user(phone)
        if user is None:
            user = self.create_user(phone)
        return user

    def update_user(self, user: User) -> User:
        """Persist changes to an existing user.  Raises ``NotFoundError``."""
        row = self._db.get(UserRow, user.phone)
        if not row:
            raise NotFoundError(f"User {user.phone} not found")
        row.name = user.name
        row.past_item_ids = ",".join(user.past_item_ids)
        row.default_address = user.default_address
        row.updated_at = datetime.now(timezone.utc)
        self._db.flush()
        return _row_to_user(row)

    # -- Menu ----------------------------------------------------------------

    def get_menu(self) -> list[MenuItem]:
        """Return all menu items as domain objects."""
        rows = self._db.execute(select(MenuItemRow)).scalars().all()
        return [_row_to_menu_item(r) for r in rows]

    # -- Promos --------------------------------------------------------------

    def get_promo(self, code: str) -> PromoCode | None:
        """Look up an active promo code.  Returns ``None`` if not found or inactive."""
        row = self._db.get(PromoCodeRow, code)
        if row and row.active:
            return _row_to_promo(row)
        return None

    def get_all_promos(self) -> dict[str, PromoCode]:
        """Return all active promo codes keyed by code string."""
        rows = self._db.execute(
            select(PromoCodeRow).where(PromoCodeRow.active == True)  # noqa: E712
        ).scalars().all()
        return {r.code: _row_to_promo(r) for r in rows}

    # -- Orders --------------------------------------------------------------

    def create_order(self, order: Order, status: str | None = None,
                     payment_status: str | None = None,
                     payment_id: str | None = None) -> str:
        """Persist an order inside a transaction.

        Also updates the user's ``past_item_ids`` so order history is
        consistent. Returns the generated order ID.

        Raises ``RepositoryError`` on any failure (the transaction is
        rolled back).
        """
        order_id = str(uuid.uuid4())
        effective_status = status or order.status or "confirmed"
        effective_pay_status = payment_status or getattr(order, "payment_status", "pending")
        effective_pay_id = payment_id or getattr(order, "payment_id", None)

        row = OrderRow(
            id=order_id,
            user_phone=order.user_phone,
            item_id=order.item.item_id if order.item else "",
            item_name=order.item.name if order.item else "",
            restaurant=order.item.restaurant if order.item else "",
            address=order.address or "",
            promo_code=order.promo.code if order.promo else None,
            subtotal=order.subtotal,
            discount=order.discount,
            total=order.total,
            payment_id=effective_pay_id,
            payment_status=effective_pay_status,
            status=effective_status,
        )
        self._db.add(row)

        # Also persist the user's updated order history
        user_row = self._db.get(UserRow, order.user_phone)
        if user_row:
            existing = [s for s in user_row.past_item_ids.split(",") if s]
            if order.item and order.item.item_id not in existing:
                existing.append(order.item.item_id)
            user_row.past_item_ids = ",".join(existing)
            user_row.updated_at = datetime.now(timezone.utc)

        try:
            self._db.flush()
        except IntegrityError as exc:
            self._db.rollback()
            raise RepositoryError(f"Failed to create order: {exc}") from exc

        return order_id

    def get_order(self, order_id: str, user_phone: str | None = None) -> dict | None:
        """Retrieve an order by its UUID with optional user authorization check.

        Returns a plain dict summary, or ``None`` if not found or unauthorized.
        """
        # Validate UUID format to prevent malformed queries
        if not re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", order_id, re.IGNORECASE):
            return None

        row = self._db.get(OrderRow, order_id)
        if not row:
            return None

        # Ownership authorization check
        if user_phone and row.user_phone != user_phone:
            return None

        return {
            "id": row.id,
            "user_phone": row.user_phone,
            "item_id": row.item_id,
            "item_name": row.item_name,
            "restaurant": row.restaurant,
            "address": row.address,
            "promo_code": row.promo_code,
            "subtotal": row.subtotal,
            "discount": row.discount,
            "total": row.total,
            "payment_id": row.payment_id,
            "payment_status": row.payment_status,
            "status": row.status,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    def update_order_payment(self, order_id: str, payment_id: str,
                             payment_status: str, order_status: str = "confirmed") -> dict | None:
        """Atomically and idempotently update payment status on an order."""
        if not re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", order_id, re.IGNORECASE):
            return None

        row = self._db.get(OrderRow, order_id)
        if not row:
            return None

        row.payment_id = payment_id
        row.payment_status = payment_status
        row.status = order_status
        row.updated_at = datetime.now(timezone.utc)
        self._db.flush()

        return {
            "id": row.id,
            "user_phone": row.user_phone,
            "total": row.total,
            "payment_id": row.payment_id,
            "payment_status": row.payment_status,
            "status": row.status,
        }

    def list_user_orders(self, phone: str) -> list[dict]:
        """Return all orders for a user, most recent first."""
        rows = self._db.execute(
            select(OrderRow)
            .where(OrderRow.user_phone == phone)
            .order_by(OrderRow.created_at.desc())
        ).scalars().all()
        return [
            {
                "id": r.id,
                "item_name": r.item_name,
                "restaurant": r.restaurant,
                "total": r.total,
                "payment_status": r.payment_status,
                "status": r.status,
                "created_at": r.created_at,
            }
            for r in rows
        ]
