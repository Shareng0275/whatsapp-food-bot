"""Repository layer tests — all run against SQLite in-memory, no PostgreSQL required.

Tests cover:
  - User CRUD (get, create, get_or_create, update)
  - Duplicate user detection
  - Menu retrieval
  - Promo lookup (active/inactive/missing)
  - Order creation (with and without promo)
  - Order retrieval by ID
  - User order listing
  - Transaction integrity (order + user history updated atomically)
  - Error handling for not-found and duplicates
"""
import os
import unittest

# Force SQLite for tests — must be set before importing database.py
os.environ["DATABASE_URL"] = "sqlite://"

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base
from db_models import UserRow, MenuItemRow, PromoCodeRow, OrderRow  # noqa: F401
from models import MenuItem, PromoCode, Order
from repository import Repository, NotFoundError, DuplicateError


def _make_engine():
    return create_engine("sqlite://", echo=False)


def _seed_menu(db):
    """Insert a small menu for testing."""
    db.add(MenuItemRow(
        item_id="i1", name="Veg Biryani", restaurant="Paradise",
        rating=4.6, price=220, eta_minutes=35, tags="biryani,veg",
    ))
    db.add(MenuItemRow(
        item_id="i2", name="Chicken Biryani", restaurant="Paradise",
        rating=4.8, price=280, eta_minutes=35, tags="biryani,non-veg",
    ))
    db.commit()


def _seed_promos(db):
    """Insert test promo codes."""
    db.add(PromoCodeRow(
        code="SAVE15", description="15% off", discount_type="percentage",
        discount_value=0.15, min_order_value=200.0, active=True,
    ))
    db.add(PromoCodeRow(
        code="EXPIRED", description="Old promo", discount_type="flat",
        discount_value=100.0, min_order_value=0.0, active=False,
    ))
    db.commit()


class RepositoryTestBase(unittest.TestCase):
    """Base class that creates a fresh SQLite DB for each test."""

    def setUp(self):
        self.engine = _make_engine()
        Base.metadata.create_all(self.engine)
        self.SessionFactory = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.db = self.SessionFactory()
        self.repo = Repository(self.db)

    def tearDown(self):
        self.db.close()


# ===========================================================================
# User CRUD
# ===========================================================================

class TestUserCRUD(RepositoryTestBase):

    def test_create_and_get_user(self):
        user = self.repo.create_user("+911111111111", "Alice", "123 Main St")
        self.assertEqual(user.phone, "+911111111111")
        self.assertEqual(user.name, "Alice")
        self.assertEqual(user.default_address, "123 Main St")
        self.assertEqual(user.past_item_ids, [])

        fetched = self.repo.get_user("+911111111111")
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.name, "Alice")

    def test_get_user_not_found(self):
        result = self.repo.get_user("+919999999999")
        self.assertIsNone(result)

    def test_create_duplicate_user_raises(self):
        self.repo.create_user("+911111111111")
        with self.assertRaises(DuplicateError):
            self.repo.create_user("+911111111111")

    def test_get_or_create_user_creates(self):
        user = self.repo.get_or_create_user("+912222222222")
        self.assertEqual(user.name, "Guest")

    def test_get_or_create_user_gets_existing(self):
        self.repo.create_user("+913333333333", "Bob")
        user = self.repo.get_or_create_user("+913333333333")
        self.assertEqual(user.name, "Bob")

    def test_update_user(self):
        user = self.repo.create_user("+914444444444", "Carol")
        user.name = "Caroline"
        user.past_item_ids = ["i1", "i2"]
        user.default_address = "456 Oak Ave"
        updated = self.repo.update_user(user)
        self.assertEqual(updated.name, "Caroline")
        self.assertEqual(updated.past_item_ids, ["i1", "i2"])
        self.assertEqual(updated.default_address, "456 Oak Ave")

    def test_update_nonexistent_user_raises(self):
        from models import User
        ghost = User(phone="+910000000000", name="Ghost")
        with self.assertRaises(NotFoundError):
            self.repo.update_user(ghost)


# ===========================================================================
# Menu
# ===========================================================================

class TestMenu(RepositoryTestBase):

    def test_get_menu_empty(self):
        items = self.repo.get_menu()
        self.assertEqual(items, [])

    def test_get_menu_with_items(self):
        _seed_menu(self.db)
        items = self.repo.get_menu()
        self.assertEqual(len(items), 2)
        # Verify domain objects
        veg = next(i for i in items if i.item_id == "i1")
        self.assertEqual(veg.name, "Veg Biryani")
        self.assertEqual(veg.tags, ["biryani", "veg"])
        self.assertIsInstance(veg, MenuItem)


# ===========================================================================
# Promos
# ===========================================================================

class TestPromos(RepositoryTestBase):

    def setUp(self):
        super().setUp()
        _seed_promos(self.db)

    def test_get_active_promo(self):
        promo = self.repo.get_promo("SAVE15")
        self.assertIsNotNone(promo)
        self.assertEqual(promo.discount_type, "percentage")
        self.assertEqual(promo.discount_value, 0.15)

    def test_get_inactive_promo_returns_none(self):
        promo = self.repo.get_promo("EXPIRED")
        self.assertIsNone(promo)

    def test_get_missing_promo_returns_none(self):
        promo = self.repo.get_promo("DOESNOTEXIST")
        self.assertIsNone(promo)

    def test_get_all_promos_excludes_inactive(self):
        promos = self.repo.get_all_promos()
        self.assertIn("SAVE15", promos)
        self.assertNotIn("EXPIRED", promos)


# ===========================================================================
# Orders
# ===========================================================================

class TestOrders(RepositoryTestBase):

    def setUp(self):
        super().setUp()
        _seed_menu(self.db)
        _seed_promos(self.db)
        self.repo.create_user("+915555555555", "Dave")

    def _make_test_order(self, promo_code: str | None = None) -> Order:
        item = MenuItem("i1", "Veg Biryani", "Paradise", 4.6, 220, 35, ["biryani", "veg"])
        promo = None
        if promo_code:
            promo = self.repo.get_promo(promo_code)
        return Order(
            user_phone="+915555555555",
            item=item,
            address="789 Pine Rd",
            promo=promo,
        )

    def test_create_order_returns_id(self):
        order = self._make_test_order()
        order_id = self.repo.create_order(order)
        self.assertIsNotNone(order_id)
        self.assertEqual(len(order_id), 36)  # UUID format

    def test_create_order_with_promo(self):
        order = self._make_test_order("SAVE15")
        order_id = self.repo.create_order(order)
        retrieved = self.repo.get_order(order_id)
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved["promo_code"], "SAVE15")
        self.assertEqual(retrieved["subtotal"], 220.0)
        self.assertEqual(retrieved["discount"], 33.0)
        self.assertEqual(retrieved["total"], 187.0)

    def test_create_order_without_promo(self):
        order = self._make_test_order()
        order_id = self.repo.create_order(order)
        retrieved = self.repo.get_order(order_id)
        self.assertIsNone(retrieved["promo_code"])
        self.assertEqual(retrieved["discount"], 0.0)
        self.assertEqual(retrieved["total"], 220.0)

    def test_get_nonexistent_order(self):
        result = self.repo.get_order("00000000-0000-0000-0000-000000000000")
        self.assertIsNone(result)

    def test_order_updates_user_history(self):
        """Creating an order should add the item to the user's past_item_ids."""
        order = self._make_test_order()
        self.repo.create_order(order)
        user = self.repo.get_user("+915555555555")
        self.assertIn("i1", user.past_item_ids)

    def test_list_user_orders(self):
        order1 = self._make_test_order()
        order2 = self._make_test_order("SAVE15")
        self.repo.create_order(order1)
        self.repo.create_order(order2)
        orders = self.repo.list_user_orders("+915555555555")
        self.assertEqual(len(orders), 2)
        # Most recent first
        self.assertIn("id", orders[0])
        self.assertIn("total", orders[0])

    def test_list_orders_empty(self):
        orders = self.repo.list_user_orders("+915555555555")
        self.assertEqual(orders, [])

    def test_order_persists_all_fields(self):
        order = self._make_test_order("SAVE15")
        order_id = self.repo.create_order(order)
        retrieved = self.repo.get_order(order_id)
        self.assertEqual(retrieved["user_phone"], "+915555555555")
        self.assertEqual(retrieved["item_id"], "i1")
        self.assertEqual(retrieved["item_name"], "Veg Biryani")
        self.assertEqual(retrieved["restaurant"], "Paradise")
        self.assertEqual(retrieved["address"], "789 Pine Rd")
        self.assertEqual(retrieved["status"], "confirmed")


if __name__ == "__main__":
    unittest.main()
