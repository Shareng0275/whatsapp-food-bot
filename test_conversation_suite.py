"""Automated tests for conversation.py state machine.

Requirements tested:
1. hi starts/reset flow.
2. search displays options.
3. invalid selection rejected.
4. valid selection transitions to ADDRESS.
5. same uses saved address.
6. custom address accepted.
7. invalid promo remains in PROMO.
8. SAVE15 minimum rejected.
9. skip confirms order.
10. valid promo confirms order.
11. confirmation contains correct totals.
12. message after CONFIRMED starts a new order.
13. unknown user is created correctly.
14. empty input handled safely.
"""
import os
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ["DATABASE_URL"] = "sqlite://"

from database import Base
from db_models import UserRow, MenuItemRow, PromoCodeRow, OrderRow  # noqa: F401
from repository import Repository
from conversation import Session, State


@pytest.fixture
def repo():
    engine = create_engine("sqlite://", echo=False)
    Base.metadata.create_all(engine)
    SessionMaker = sessionmaker(bind=engine, expire_on_commit=False)
    db = SessionMaker()

    # Seed test menu
    db.add(MenuItemRow(
        item_id="i1", name="Veg Biryani", restaurant="Paradise Biryani",
        rating=4.6, price=220.0, eta_minutes=35, tags="biryani,veg",
    ))
    db.add(MenuItemRow(
        item_id="i2", name="Veg Biryani", restaurant="Biryani Blues",
        rating=4.3, price=199.0, eta_minutes=40, tags="biryani,veg",
    ))
    db.add(MenuItemRow(
        item_id="i3", name="Veg Biryani", restaurant="Meghana Foods",
        rating=4.7, price=250.0, eta_minutes=30, tags="biryani,veg",
    ))
    db.add(MenuItemRow(
        item_id="i7", name="Masala Dosa", restaurant="Vidyarthi Bhavan",
        rating=4.9, price=90.0, eta_minutes=20, tags="south-indian,veg",
    ))

    # Seed test promos
    db.add(PromoCodeRow(
        code="SAVE15", description="15% off", discount_type="percentage",
        discount_value=0.15, min_order_value=200.0, active=True,
    ))
    db.add(PromoCodeRow(
        code="WELCOME50", description="50 off", discount_type="flat",
        discount_value=50.0, min_order_value=0.0, active=True,
    ))
    db.add(PromoCodeRow(
        code="SAVE10", description="10% off", discount_type="percentage",
        discount_value=0.10, min_order_value=0.0, active=True,
    ))

    # Seed test user
    db.add(UserRow(
        phone="+919900011122", name="Asha", default_address="204, Palm Residency, Indiranagar",
        past_item_ids="i2",
    ))
    db.commit()

    return Repository(db)


class TestConversationRequirements:
    def test_1_hi_starts_or_resets_flow(self, repo):
        """1. 'hi' starts/resets flow."""
        session = Session("+919900011122", repo)
        session.state = State.ADDRESS  # Simulate mid-conversation state
        session.order.address = "Temporary Address"

        reply = session.handle_message("hi")
        assert session.state == State.SEARCHING
        assert session.order.address is None
        assert "Hi! I'm your food ordering assistant." in reply

        # Also works with hello, start, restart
        assert "food ordering assistant" in session.handle_message("hello")
        assert "food ordering assistant" in session.handle_message("start")
        assert "food ordering assistant" in session.handle_message("restart")

    def test_2_search_displays_options(self, repo):
        """2. search displays options."""
        session = Session("+919900011122", repo)
        reply = session.handle_message("Veg Biryani")
        assert session.state == State.SELECTING
        assert "Here are the top options for 'Veg Biryani':" in reply
        assert "1. " in reply
        assert "2. " in reply
        assert "3. " in reply
        assert "Reply with 1, 2, or 3 to select." in reply

    def test_3_invalid_selection_rejected(self, repo):
        """3. invalid selection rejected."""
        session = Session("+919900011122", repo)
        session.handle_message("Veg Biryani")
        assert session.state == State.SELECTING

        reply_invalid = session.handle_message("4")
        assert session.state == State.SELECTING
        assert "Please reply with 1, 2, or 3 to pick an option." in reply_invalid

        reply_abc = session.handle_message("abc")
        assert session.state == State.SELECTING
        assert "Please reply with 1, 2, or 3 to pick an option." in reply_abc

    def test_4_valid_selection_transitions_to_address(self, repo):
        """4. valid selection transitions to ADDRESS."""
        session = Session("+919900011122", repo)
        session.handle_message("Veg Biryani")
        reply = session.handle_message("1")

        assert session.state == State.ADDRESS
        assert session.order.item is not None
        assert "Deliver to your saved address" in reply

    def test_5_same_uses_saved_address(self, repo):
        """5. 'same' uses saved address."""
        session = Session("+919900011122", repo)
        session.handle_message("Veg Biryani")
        session.handle_message("1")
        reply = session.handle_message("same")

        assert session.state == State.PROMO
        assert session.order.address == "204, Palm Residency, Indiranagar"
        assert "Do you have a promo code?" in reply

    def test_6_custom_address_accepted(self, repo):
        """6. custom address accepted."""
        session = Session("+919900011122", repo)
        session.handle_message("Veg Biryani")
        session.handle_message("1")
        custom_addr = "Flat 401, Galaxy Apartments, Koramangala"
        reply = session.handle_message(custom_addr)

        assert session.state == State.PROMO
        assert session.order.address == custom_addr
        assert "Do you have a promo code?" in reply

    def test_7_invalid_promo_remains_in_promo(self, repo):
        """7. invalid promo remains in PROMO."""
        session = Session("+919900011122", repo)
        session.handle_message("Veg Biryani")
        session.handle_message("1")
        session.handle_message("same")
        assert session.state == State.PROMO

        reply = session.handle_message("BOGUS_CODE")
        assert session.state == State.PROMO
        assert "'BOGUS_CODE' isn't a valid code." in reply

    def test_8_save15_minimum_rejected(self, repo):
        """8. SAVE15 minimum rejected when subtotal < min_order_value."""
        session = Session("+919900011122", repo)
        session.handle_message("Masala Dosa")  # ₹90
        session.handle_message("1")
        session.handle_message("same")
        assert session.state == State.PROMO

        reply = session.handle_message("SAVE15")
        assert session.state == State.PROMO
        assert "'SAVE15' needs a minimum order of ₹200.0." in reply

    def test_9_skip_confirms_order(self, repo):
        """9. skip confirms order without discount."""
        session = Session("+919900011122", repo)
        session.handle_message("Veg Biryani")
        session.handle_message("1")
        session.handle_message("same")
        reply = session.handle_message("skip")

        assert session.state == State.CONFIRMED
        assert "✅ Order confirmed!" in reply
        assert "Discount" not in reply
        assert session.order.order_id is not None

    def test_10_valid_promo_confirms_order(self, repo):
        """10. valid promo confirms order with discount."""
        session = Session("+919900011122", repo)
        session.handle_message("Veg Biryani")
        session.handle_message("2")  # Selects Meghana Foods (₹250) or Paradise (₹220), subtotal >= 200
        session.handle_message("same")
        reply = session.handle_message("SAVE15")

        assert session.state == State.CONFIRMED
        assert "✅ Order confirmed!" in reply
        assert "Discount (SAVE15): -₹" in reply
        assert session.order.order_id is not None

    def test_11_confirmation_contains_correct_totals(self, repo):
        """11. confirmation contains correct totals."""
        session = Session("+919900011122", repo)
        session.handle_message("Veg Biryani")
        session.handle_message("2")  # Selects option >= 200
        session.handle_message("same")
        reply = session.handle_message("SAVE15")

        item = session.order.item
        subtotal = item.price
        discount = round(subtotal * 0.15, 2)
        total = round(subtotal - discount, 2)

        assert f"Subtotal: ₹{subtotal:.2f}" in reply
        assert f"Discount (SAVE15): -₹{discount:.2f}" in reply
        assert f"Total: ₹{total:.2f}" in reply
        assert f"ETA: ~{item.eta_minutes} minutes" in reply

    def test_12_message_after_confirmed_starts_new_order(self, repo):
        """12. message after CONFIRMED starts a new order."""
        session = Session("+919900011122", repo)
        session.handle_message("Veg Biryani")
        session.handle_message("1")
        session.handle_message("same")
        session.handle_message("skip")
        assert session.state == State.CONFIRMED

        # A new message restarts search for Masala Dosa
        reply_new = session.handle_message("Masala Dosa")
        assert session.state == State.SELECTING
        assert "Here are the top options for 'Masala Dosa':" in reply_new

    def test_13_unknown_user_created_correctly(self, repo):
        """13. unknown user is created correctly."""
        new_phone = "+919888877777"
        session = Session(new_phone, repo)
        assert session.user.phone == new_phone
        assert session.user.name == "Guest"
        assert session.user.default_address is None

        # User without saved address is asked for delivery address
        session.handle_message("Veg Biryani")
        reply_sel = session.handle_message("1")
        assert session.state == State.ADDRESS
        assert "Please share your delivery address." in reply_sel

    def test_14_empty_input_handled_safely(self, repo):
        """14. empty input handled safely across states."""
        session = Session("+919900011122", repo)

        # In SEARCHING state
        reply_empty = session.handle_message("")
        assert session.state == State.SEARCHING
        assert "couldn't find any 4★+ options" in reply_empty

        # In SELECTING state
        session.handle_message("Veg Biryani")
        reply_empty_sel = session.handle_message("   ")
        assert session.state == State.SELECTING
        assert "Please reply with 1, 2, or 3" in reply_empty_sel

        # In ADDRESS state
        session.handle_message("1")
        assert session.state == State.ADDRESS
        reply_empty_addr = session.handle_message("")
        assert session.state == State.ADDRESS
        assert "Please provide a delivery address" in reply_empty_addr

    def test_15_address_same_without_saved_address_rejected(self, repo):
        """User without default_address replying 'same' is asked for their address."""
        session = Session("+919888877777", repo)  # New guest user with no saved address
        session.handle_message("Veg Biryani")
        session.handle_message("1")
        assert session.state == State.ADDRESS
        reply = session.handle_message("same")
        assert session.state == State.ADDRESS
        assert "You don't have a saved address." in reply
