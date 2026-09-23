"""Unit tests for promo-code discount logic.

Covers every required scenario from the spec, plus edge cases:
  - ₹220 + SAVE15  → discount ₹33   → total ₹187
  - ₹199 + SAVE15  → promo rejected (min ₹200)
  - ₹220 + SAVE10  → discount ₹22   → total ₹198
  - ₹220 + WELCOME50 → discount ₹50 → total ₹170
  - ₹40  + WELCOME50 → discount ₹40 → total ₹0 (capped at subtotal)
  - skip → no discount
  - invalid promo → stays in PROMO state and user can retry

Also tests the end-to-end conversation flow via Session.
"""
import os
import unittest

# Force SQLite for tests — must be set before importing database.py
os.environ["DATABASE_URL"] = "sqlite://"

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base
from db_models import UserRow, MenuItemRow, PromoCodeRow  # noqa: F401
from models import MenuItem, PromoCode, Order
from repository import Repository
from conversation import Session, State
from data_store import MENU, USERS, PROMO_CODES


# ---------------------------------------------------------------------------
# Test-database helpers
# ---------------------------------------------------------------------------

def _make_engine():
    return create_engine("sqlite://", echo=False)


def _seed_db(db):
    """Insert the standard demo data into the test database."""
    for item in MENU:
        db.add(MenuItemRow(
            item_id=item.item_id, name=item.name, restaurant=item.restaurant,
            rating=item.rating, price=item.price, eta_minutes=item.eta_minutes,
            tags=",".join(item.tags),
        ))
    for phone, user in USERS.items():
        db.add(UserRow(
            phone=user.phone, name=user.name,
            past_item_ids=",".join(user.past_item_ids),
            default_address=user.default_address,
        ))
    for code, promo in PROMO_CODES.items():
        db.add(PromoCodeRow(
            code=promo.code, description=promo.description,
            discount_type=promo.discount_type, discount_value=promo.discount_value,
            min_order_value=promo.min_order_value, active=True,
        ))
    db.commit()


# ---------------------------------------------------------------------------
# Helper to build an Order with a given price and promo
# ---------------------------------------------------------------------------

def _make_order(price: float, promo: PromoCode | None = None) -> Order:
    item = MenuItem("t1", "Test Item", "Test Restaurant", 4.5, price, 30)
    order = Order(user_phone="+910000000000", item=item, address="Test Addr", promo=promo)
    return order


# ===========================================================================
# Model-level tests (Order.subtotal / .discount / .total)
# These are pure domain logic — no database needed.
# ===========================================================================

class TestSAVE15(unittest.TestCase):
    """SAVE15: 15% off on orders ≥ ₹200."""

    def test_220_save15_discount_33_total_187(self):
        order = _make_order(220, PROMO_CODES["SAVE15"])
        self.assertEqual(order.subtotal, 220)
        self.assertEqual(order.discount, 33.0)
        self.assertEqual(order.total, 187.0)

    def test_199_save15_rejected(self):
        """Order below minimum gets ₹0 discount (model returns 0)."""
        order = _make_order(199, PROMO_CODES["SAVE15"])
        self.assertEqual(order.discount, 0.0)
        self.assertEqual(order.total, 199.0)


class TestSAVE10(unittest.TestCase):
    """SAVE10: 10% off, no minimum."""

    def test_220_save10_discount_22_total_198(self):
        order = _make_order(220, PROMO_CODES["SAVE10"])
        self.assertEqual(order.subtotal, 220)
        self.assertEqual(order.discount, 22.0)
        self.assertEqual(order.total, 198.0)


class TestWELCOME50(unittest.TestCase):
    """WELCOME50: flat ₹50 off, no minimum."""

    def test_220_welcome50_discount_50_total_170(self):
        order = _make_order(220, PROMO_CODES["WELCOME50"])
        self.assertEqual(order.subtotal, 220)
        self.assertEqual(order.discount, 50.0)
        self.assertEqual(order.total, 170.0)

    def test_40_welcome50_capped_at_subtotal(self):
        """Discount must never exceed the subtotal → total ₹0, never negative."""
        order = _make_order(40, PROMO_CODES["WELCOME50"])
        self.assertEqual(order.subtotal, 40)
        self.assertEqual(order.discount, 40.0)
        self.assertEqual(order.total, 0.0)


class TestNoPromo(unittest.TestCase):
    """No promo applied at all."""

    def test_no_promo(self):
        order = _make_order(220, promo=None)
        self.assertEqual(order.discount, 0.0)
        self.assertEqual(order.total, 220.0)


class TestDiscountNeverNegative(unittest.TestCase):
    """Extra edge-case: even a flat discount bigger than the item price is capped."""

    def test_flat_greater_than_subtotal(self):
        big_flat = PromoCode("BIG", "Huge flat", "flat", 9999.0, 0.0)
        order = _make_order(100, big_flat)
        self.assertEqual(order.discount, 100.0)
        self.assertEqual(order.total, 0.0)


class TestUnknownDiscountType(unittest.TestCase):
    """An unrecognised discount_type gives ₹0 discount gracefully."""

    def test_unknown_type_no_discount(self):
        weird = PromoCode("WEIRD", "Bad type", "bogus", 0.5, 0.0)
        order = _make_order(200, weird)
        self.assertEqual(order.discount, 0.0)
        self.assertEqual(order.total, 200.0)


# ===========================================================================
# Conversation-level tests (Session / State transitions)
# These use a SQLite in-memory database seeded with demo data.
# ===========================================================================

class TestConversationPromoFlow(unittest.TestCase):
    """Drive the Session through the promo-code step to verify integration."""

    def setUp(self):
        self.engine = _make_engine()
        Base.metadata.create_all(self.engine)
        self.SessionFactory = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.db = self.SessionFactory()
        _seed_db(self.db)
        self.repo = Repository(self.db)

    def tearDown(self):
        self.db.close()

    def _advance_to_promo(self, phone: str = "+910000000000") -> Session:
        """Walk a session through hi → search → select → address → PROMO state."""
        session = Session(phone, self.repo)
        session.handle_message("hi")
        session.handle_message("Veg Biryani")
        session.handle_message("1")             # selects first option
        session.handle_message("123 Test Addr")  # address
        self.assertEqual(session.state, State.PROMO)
        return session

    def test_skip_no_discount(self):
        session = self._advance_to_promo()
        reply = session.handle_message("skip")
        self.assertEqual(session.state, State.CONFIRMED)
        self.assertIn("Total", reply)
        # No discount line should appear
        self.assertNotIn("Discount", reply)

    def test_invalid_promo_stays_in_promo_state(self):
        session = self._advance_to_promo()
        reply = session.handle_message("FAKECODE")
        self.assertEqual(session.state, State.PROMO)
        self.assertIn("isn't a valid code", reply)

    def test_valid_promo_applied(self):
        session = self._advance_to_promo()
        reply = session.handle_message("SAVE10")
        self.assertEqual(session.state, State.CONFIRMED)
        self.assertIn("Discount", reply)
        self.assertIn("SAVE10", reply)

    def test_below_minimum_stays_in_promo_state(self):
        """If the selected item is below SAVE15's ₹200 minimum,
        the session stays in PROMO and the user can retry."""
        session = Session("+910000000000", self.repo)
        session.handle_message("hi")
        session.handle_message("Masala Dosa")    # ₹90, below ₹200 min
        session.handle_message("1")
        session.handle_message("123 Test Addr")
        self.assertEqual(session.state, State.PROMO)

        reply = session.handle_message("SAVE15")
        self.assertEqual(session.state, State.PROMO)
        self.assertIn("minimum order", reply)

    def test_retry_after_invalid_then_skip(self):
        """User enters invalid promo, then skips."""
        session = self._advance_to_promo()
        session.handle_message("BADCODE")
        self.assertEqual(session.state, State.PROMO)
        reply = session.handle_message("skip")
        self.assertEqual(session.state, State.CONFIRMED)
        self.assertIn("Total", reply)

    def test_retry_after_invalid_then_valid(self):
        """User enters invalid promo, then a valid one."""
        session = self._advance_to_promo()
        session.handle_message("BADCODE")
        self.assertEqual(session.state, State.PROMO)
        reply = session.handle_message("WELCOME50")
        self.assertEqual(session.state, State.CONFIRMED)
        self.assertIn("Discount", reply)
        self.assertIn("WELCOME50", reply)

    def test_order_id_in_confirmation(self):
        """The confirmation message should include an Order ID."""
        session = self._advance_to_promo()
        reply = session.handle_message("skip")
        self.assertIn("Order ID:", reply)


if __name__ == "__main__":
    unittest.main()
