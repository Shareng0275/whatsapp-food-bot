"""SQLAlchemy ORM table definitions.

These are *persistence* models — they mirror the database schema.  The domain
dataclasses in ``models.py`` remain the interface used by ``recommender.py``
and ``conversation.py``.  The ``repository.py`` module converts between the
two layers.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    String, Float, Integer, Text, Boolean, DateTime, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

class UserRow(Base):
    __tablename__ = "users"

    phone: Mapped[str] = mapped_column(String(20), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, default="Guest")
    # Stored as comma-separated item IDs — simple and sufficient for this use case.
    past_item_ids: Mapped[str] = mapped_column(Text, nullable=False, default="")
    default_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
        onupdate=_utcnow,
    )


class MenuItemRow(Base):
    __tablename__ = "menu_items"

    item_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    restaurant: Mapped[str] = mapped_column(String(200), nullable=False)
    rating: Mapped[float] = mapped_column(Float, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    eta_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    # Tags stored as comma-separated values — avoids an extra join table for
    # a handful of simple labels.
    tags: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )


class PromoCodeRow(Base):
    __tablename__ = "promo_codes"

    code: Mapped[str] = mapped_column(String(30), primary_key=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    discount_type: Mapped[str] = mapped_column(String(20), nullable=False)
    discount_value: Mapped[float] = mapped_column(Float, nullable=False)
    min_order_value: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )


class OrderRow(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4()),
    )
    user_phone: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    item_id: Mapped[str] = mapped_column(String(20), nullable=False)
    item_name: Mapped[str] = mapped_column(String(200), nullable=False)
    restaurant: Mapped[str] = mapped_column(String(200), nullable=False)
    address: Mapped[str] = mapped_column(Text, nullable=False)
    promo_code: Mapped[str | None] = mapped_column(String(30), nullable=True)
    subtotal: Mapped[float] = mapped_column(Float, nullable=False)
    discount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    total: Mapped[float] = mapped_column(Float, nullable=False)
    payment_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    payment_status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="pending",
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="confirmed",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
        onupdate=_utcnow,
    )
