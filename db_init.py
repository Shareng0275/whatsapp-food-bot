"""Create database tables and seed demo data.

Usage
-----
    python db_init.py            # create tables + seed (skip if data exists)
    python db_init.py --reset    # drop all tables, recreate, and seed
"""
import sys

from database import engine, get_db, Base
# Import all ORM models so Base.metadata knows about them.
from db_models import UserRow, MenuItemRow, PromoCodeRow, OrderRow  # noqa: F401
from data_store import MENU, USERS, PROMO_CODES


def create_tables(drop_first: bool = False) -> None:
    """Create all tables.  Optionally drop existing tables first."""
    if drop_first:
        print("Dropping all tables …")
        Base.metadata.drop_all(bind=engine)
    print("Creating tables …")
    Base.metadata.create_all(bind=engine)
    print("Done.")


def seed_data() -> None:
    """Insert the same demo data that used to live in ``data_store.py``.

    Skips records that already exist so the script is safe to re-run.
    """
    with get_db() as db:
        # --- Menu items ---
        for item in MENU:
            if not db.get(MenuItemRow, item.item_id):
                db.add(MenuItemRow(
                    item_id=item.item_id,
                    name=item.name,
                    restaurant=item.restaurant,
                    rating=item.rating,
                    price=item.price,
                    eta_minutes=item.eta_minutes,
                    tags=",".join(item.tags),
                ))
                print(f"  Seeded menu item: {item.name} ({item.restaurant})")

        # --- Users ---
        for phone, user in USERS.items():
            if not db.get(UserRow, phone):
                db.add(UserRow(
                    phone=user.phone,
                    name=user.name,
                    past_item_ids=",".join(user.past_item_ids),
                    default_address=user.default_address,
                ))
                print(f"  Seeded user: {user.name} ({user.phone})")

        # --- Promo codes ---
        for code, promo in PROMO_CODES.items():
            if not db.get(PromoCodeRow, code):
                db.add(PromoCodeRow(
                    code=promo.code,
                    description=promo.description,
                    discount_type=promo.discount_type,
                    discount_value=promo.discount_value,
                    min_order_value=promo.min_order_value,
                    active=True,
                ))
                print(f"  Seeded promo: {promo.code}")

    print("Seed complete.")


if __name__ == "__main__":
    reset = "--reset" in sys.argv
    create_tables(drop_first=reset)
    seed_data()
