"""Automated tests for simulate.py."""
import os
import unittest
from unittest.mock import patch
from io import StringIO

os.environ["DATABASE_URL"] = "sqlite://"

from database import Base, get_db
from db_models import UserRow, MenuItemRow, PromoCodeRow, OrderRow  # noqa: F401
import simulate


class TestSimulate(unittest.TestCase):
    def setUp(self):
        with get_db() as db:
            Base.metadata.drop_all(db.bind)
            Base.metadata.create_all(db.bind)
            db.add(MenuItemRow(
                item_id="i1", name="Veg Biryani", restaurant="Paradise Biryani",
                rating=4.6, price=220.0, eta_minutes=35, tags="biryani,veg",
            ))
            db.add(PromoCodeRow(
                code="SAVE15", description="15% off", discount_type="percentage",
                discount_value=0.15, min_order_value=200.0, active=True,
            ))
            db.add(UserRow(
                phone=simulate.DEMO_PHONE, name="Asha",
                default_address="204, Palm Residency, Indiranagar, Bengaluru",
                past_item_ids="",
            ))
            db.commit()

    def test_run_auto(self):
        """run_auto completes scripted order without throwing errors."""
        with patch("sys.stdout", new_callable=StringIO) as fake_out:
            simulate.run_auto()
            output = fake_out.getvalue()
            assert "✅ Order confirmed!" in output
            assert "Subtotal: ₹220.00" in output
            assert "Discount (SAVE15): -₹33.00" in output
            assert "Total: ₹187.00" in output

    def test_run_interactive_exit(self):
        """run_interactive exits cleanly on 'quit'."""
        with patch("builtins.input", side_effect=["quit"]):
            with patch("sys.stdout", new_callable=StringIO) as fake_out:
                simulate.run_interactive()
                output = fake_out.getvalue()
                assert "Hi! I'm your food ordering assistant." in output

    def test_run_interactive_eof(self):
        """run_interactive handles EOFError gracefully."""
        with patch("builtins.input", side_effect=EOFError):
            with patch("sys.stdout", new_callable=StringIO) as fake_out:
                simulate.run_interactive()
                output = fake_out.getvalue()
                assert "Hi! I'm your food ordering assistant." in output
