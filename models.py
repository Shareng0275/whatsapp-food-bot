"""Plain data shapes. No behavior lives here on purpose —
keeping models dumb makes the state machine and recommender easy to test in isolation."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class MenuItem:
    item_id: str
    name: str
    restaurant: str
    rating: float          # out of 5
    price: float            # in rupees
    eta_minutes: int
    tags: list[str] = field(default_factory=list)


@dataclass
class PromoCode:
    """Supports both percentage-based and flat/fixed-amount discounts.

    Can be initialized with (discount_pct, fixed_discount) or (discount_type, discount_value).
    """
    code: str
    description: str
    discount_type: str = "percentage"      # 'percentage' or 'flat'
    discount_value: float = 0.0            # fraction for percentage, rupees for flat
    min_order_value: float = 0.0
    discount_pct: float = 0.0
    fixed_discount: float = 0.0

    def __post_init__(self):
        if self.fixed_discount > 0 and self.discount_value == 0.0:
            self.discount_type = "flat"
            self.discount_value = self.fixed_discount
        elif self.discount_pct > 0 and self.discount_value == 0.0:
            self.discount_type = "percentage"
            self.discount_value = self.discount_pct
        elif self.discount_type == "flat" and self.fixed_discount == 0.0:
            self.fixed_discount = self.discount_value
        elif self.discount_type == "percentage" and self.discount_pct == 0.0:
            self.discount_pct = self.discount_value


@dataclass
class User:
    phone: str
    name: str
    past_item_ids: list[str] = field(default_factory=list)   # order history, most recent last
    default_address: Optional[str] = None


@dataclass
class Order:
    user_phone: str
    item: Optional[MenuItem] = None
    address: Optional[str] = None
    promo: Optional[PromoCode] = None
    order_id: Optional[str] = None
    payment_id: Optional[str] = None
    payment_status: str = "pending"
    status: str = "confirmed"
    created_at: Optional[datetime] = None

    @property
    def subtotal(self) -> float:
        return self.item.price if self.item else 0.0

    @property
    def discount(self) -> float:
        if not self.promo or self.subtotal < self.promo.min_order_value:
            return 0.0
        if self.promo.discount_pct > 0 or self.promo.fixed_discount > 0:
            raw = (self.subtotal * self.promo.discount_pct) + self.promo.fixed_discount
        elif self.promo.discount_type == "percentage":
            raw = self.subtotal * self.promo.discount_value
        elif self.promo.discount_type == "flat":
            raw = self.promo.discount_value
        else:
            raw = 0.0
        # Discount can never exceed the subtotal
        return round(min(raw, self.subtotal), 2)

    @property
    def total(self) -> float:
        return round(self.subtotal - self.discount, 2)
