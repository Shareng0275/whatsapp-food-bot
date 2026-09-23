"""Automated tests for data_store.py."""
from data_store import MENU, USERS, PROMO_CODES, get_or_create_user
from models import MenuItem, User, PromoCode


def test_data_store_menu_structure():
    assert len(MENU) >= 8
    for item in MENU:
        assert isinstance(item, MenuItem)
        assert item.price > 0
        assert item.rating >= 0


def test_data_store_promo_codes():
    assert "WELCOME50" in PROMO_CODES
    assert "SAVE15" in PROMO_CODES
    assert "SAVE10" in PROMO_CODES
    assert PROMO_CODES["WELCOME50"].discount_type == "flat"
    assert PROMO_CODES["WELCOME50"].discount_value == 50.0


def test_get_or_create_user_existing():
    existing = get_or_create_user("+919900011122")
    assert existing.name == "Asha"


def test_get_or_create_user_new():
    new_user = get_or_create_user("+919999900000")
    assert new_user.name == "Guest"
    assert new_user.phone == "+919999900000"
