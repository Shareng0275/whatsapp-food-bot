"""SQLAlchemy ORM table definitions for production WhatsApp Food Ordering Bot.

Features:
- Full backward-compatibility with existing UserRow, MenuItemRow, PromoCodeRow, OrderRow
- Normalized multi-item support (OrderItemRow)
- User address book with default address tracking (UserAddressRow)
- Durable conversation state machine persistence (ConversationRow)
- Complete message and NLU audit history (MessageRow)
- Promo-code redemption tracking and single-use enforcement (PromoRedemptionRow)
- Recommendation analytics logging (RecommendationEventRow)
- Webhook idempotency ledger (IdempotencyRecordRow)
- UTC timestamps, indexes, foreign keys, and optimistic locking
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, List, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.sqlite import JSON as SQLiteJSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


def _utcnow() -> datetime:
    """Return current timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# 1. Users & Address Book
# ---------------------------------------------------------------------------

class UserRow(Base):
    """Registered WhatsApp customer profile."""
    __tablename__ = "users"

    phone: Mapped[str] = mapped_column(String(20), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, default="Guest")
    past_item_ids: Mapped[str] = mapped_column(Text, nullable=False, default="")
    default_address: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    dietary_preferences: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)  # 'veg', 'non-veg'
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    # Relationships
    addresses: Mapped[List[UserAddressRow]] = relationship(
        "UserAddressRow", back_populates="user", cascade="all, delete-orphan"
    )
    orders: Mapped[List[OrderRow]] = relationship("OrderRow", back_populates="user")
    conversation: Mapped[Optional[ConversationRow]] = relationship(
        "ConversationRow", back_populates="user", uselist=False, cascade="all, delete-orphan"
    )


class UserAddressRow(Base):
    """Saved delivery locations for WhatsApp customers."""
    __tablename__ = "user_addresses"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_phone: Mapped[str] = mapped_column(
        String(20), ForeignKey("users.phone", ondelete="CASCADE"), nullable=False, index=True
    )
    label: Mapped[str] = mapped_column(String(50), nullable=False, default="Home")
    full_address: Mapped[str] = mapped_column(Text, nullable=False)
    landmark: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    postal_code: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    user: Mapped[UserRow] = relationship("UserRow", back_populates="addresses")


# ---------------------------------------------------------------------------
# 2. Menu Catalog
# ---------------------------------------------------------------------------

class MenuItemRow(Base):
    """Restaurant dish catalog item."""
    __tablename__ = "menu_items"

    item_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    restaurant: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    rating: Mapped[float] = mapped_column(Float, nullable=False, index=True)
    price: Mapped[float] = mapped_column(Float, nullable=False, index=True)
    eta_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    tags: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


# ---------------------------------------------------------------------------
# 3. Promotions & Redemptions
# ---------------------------------------------------------------------------

class PromoCodeRow(Base):
    """Discount vouchers and campaign rules."""
    __tablename__ = "promo_codes"

    code: Mapped[str] = mapped_column(String(30), primary_key=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    discount_type: Mapped[str] = mapped_column(String(20), nullable=False)
    discount_value: Mapped[float] = mapped_column(Float, nullable=False)
    min_order_value: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    max_discount_cap: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    usage_limit_per_user: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    redemptions: Mapped[List[PromoRedemptionRow]] = relationship(
        "PromoRedemptionRow", back_populates="promo"
    )


class PromoRedemptionRow(Base):
    """Audit ledger of all redeemed promo codes."""
    __tablename__ = "promo_redemptions"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    promo_code: Mapped[str] = mapped_column(
        String(30), ForeignKey("promo_codes.code"), nullable=False, index=True
    )
    user_phone: Mapped[str] = mapped_column(
        String(20), ForeignKey("users.phone"), nullable=False, index=True
    )
    order_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("orders.id"), nullable=False, index=True
    )
    discount_applied: Mapped[float] = mapped_column(Float, nullable=False)
    redeemed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    promo: Mapped[PromoCodeRow] = relationship("PromoCodeRow", back_populates="redemptions")


# ---------------------------------------------------------------------------
# 4. Orders & Order Items
# ---------------------------------------------------------------------------

class OrderRow(Base):
    """Customer food order record."""
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_phone: Mapped[str] = mapped_column(
        String(20), ForeignKey("users.phone"), nullable=False, index=True
    )
    item_id: Mapped[str] = mapped_column(String(20), nullable=False)
    item_name: Mapped[str] = mapped_column(String(200), nullable=False)
    restaurant: Mapped[str] = mapped_column(String(200), nullable=False)
    address: Mapped[str] = mapped_column(Text, nullable=False)
    promo_code: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    subtotal: Mapped[float] = mapped_column(Float, nullable=False)
    discount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    total: Mapped[float] = mapped_column(Float, nullable=False)
    payment_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    payment_status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="pending", index=True
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="confirmed", index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    user: Mapped[UserRow] = relationship("UserRow", back_populates="orders")
    items: Mapped[List[OrderItemRow]] = relationship(
        "OrderItemRow", back_populates="order", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_orders_user_created", "user_phone", "created_at"),
    )


class OrderItemRow(Base):
    """Granular line-item breakdown for an order."""
    __tablename__ = "order_items"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    order_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    item_id: Mapped[str] = mapped_column(
        String(20), ForeignKey("menu_items.item_id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    unit_price: Mapped[float] = mapped_column(Float, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    total_price: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    order: Mapped[OrderRow] = relationship("OrderRow", back_populates="items")


# ---------------------------------------------------------------------------
# 5. Durable Conversation State & Audit History
# ---------------------------------------------------------------------------

class ConversationRow(Base):
    """Durable conversation state machine record surviving process restarts."""
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_phone: Mapped[str] = mapped_column(
        String(20), ForeignKey("users.phone", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    state: Mapped[str] = mapped_column(String(30), nullable=False, default="SEARCHING")
    session_data: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON-serialized draft order
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)  # Optimistic concurrency lock
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    user: Mapped[UserRow] = relationship("UserRow", back_populates="conversation")
    messages: Mapped[List[MessageRow]] = relationship(
        "MessageRow", back_populates="conversation", cascade="all, delete-orphan"
    )


class MessageRow(Base):
    """Complete immutable audit log of incoming and outgoing WhatsApp messages."""
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    conversation_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    user_phone: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    twilio_message_sid: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, unique=True, index=True)
    direction: Mapped[str] = mapped_column(String(10), nullable=False)  # 'inbound' or 'outbound'
    body: Mapped[str] = mapped_column(Text, nullable=False)
    detected_intent: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    nlu_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, index=True
    )

    conversation: Mapped[Optional[ConversationRow]] = relationship("ConversationRow", back_populates="messages")


# ---------------------------------------------------------------------------
# 6. Recommendation Analytics & Idempotency Ledger
# ---------------------------------------------------------------------------

class RecommendationEventRow(Base):
    """Audit log of recommendation algorithm outputs and CTR ranking performance."""
    __tablename__ = "recommendation_events"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_phone: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    query: Mapped[str] = mapped_column(String(200), nullable=False)
    recommended_item_ids: Mapped[str] = mapped_column(Text, nullable=False)  # Comma-separated item IDs
    chosen_item_id: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class IdempotencyRecordRow(Base):
    """Idempotency cache preventing duplicate processing of retried webhooks."""
    __tablename__ = "idempotency_records"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)  # e.g. twilio:SMxxxx or pay:tx_xxxx
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="processing")
    response_payload: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
