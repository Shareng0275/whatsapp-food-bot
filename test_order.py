"""Automated tests for models.py and Order calculations.

Requirements tested:
1. No item -> subtotal 0.
2. Correct subtotal.
3. SAVE15 calculation.
4. SAVE10 calculation.
5. Minimum-order enforcement.
6. Discount never makes total negative.
7. WELCOME50 fixed discount after the promo fix.
"""
import pytest
from models import Order, MenuItem, PromoCode, User


class TestOrderRequirements:
    def test_1_no_item_subtotal_zero(self):
        """1. No item -> subtotal 0."""
        order = Order(user_phone="+919900011122")
        assert order.item is None
        assert order.subtotal == 0.0
        assert order.discount == 0.0
        assert order.total == 0.0

    def test_2_correct_subtotal(self):
        """2. Correct subtotal equals item.price."""
        item = MenuItem("i1", "Veg Biryani", "Paradise", 4.6, 220.0, 35, ["biryani", "veg"])
        order = Order(user_phone="+919900011122", item=item)
        assert order.subtotal == 220.0
        assert order.discount == 0.0
        assert order.total == 220.0

    def test_3_save15_calculation(self):
        """3. SAVE15 calculation (15% off when subtotal >= 200)."""
        promo_save15 = PromoCode("SAVE15", "15% off", "percentage", 0.15, 200.0)
        item = MenuItem("i1", "Veg Biryani", "Paradise", 4.6, 220.0, 35)
        order = Order(user_phone="+919900011122", item=item, promo=promo_save15)

        assert order.subtotal == 220.0
        assert order.discount == 33.0  # 220 * 0.15
        assert order.total == 187.0    # 220 - 33

    def test_4_save10_calculation(self):
        """4. SAVE10 calculation (10% off, no minimum order)."""
        promo_save10 = PromoCode("SAVE10", "10% off", "percentage", 0.10, 0.0)
        item = MenuItem("i7", "Masala Dosa", "Vidyarthi Bhavan", 4.9, 90.0, 20)
        order = Order(user_phone="+919900011122", item=item, promo=promo_save10)

        assert order.subtotal == 90.0
        assert order.discount == 9.0   # 90 * 0.10
        assert order.total == 81.0     # 90 - 9

    def test_5_minimum_order_enforcement(self):
        """5. Minimum-order enforcement (e.g. SAVE15 on ₹199 item gives 0 discount)."""
        promo_save15 = PromoCode("SAVE15", "15% off", "percentage", 0.15, 200.0)
        item = MenuItem("i2", "Veg Biryani", "Biryani Blues", 4.3, 199.0, 40)
        order = Order(user_phone="+919900011122", item=item, promo=promo_save15)

        assert order.subtotal == 199.0
        assert order.discount == 0.0
        assert order.total == 199.0

    def test_6_discount_never_makes_total_negative(self):
        """6. Discount never makes total negative (capped at subtotal)."""
        promo_big = PromoCode("FLAT100", "100 off", "flat", 100.0, 0.0)
        item_cheap = MenuItem("i_tea", "Chai", "Tea Point", 4.5, 30.0, 10)
        order = Order(user_phone="+919900011122", item=item_cheap, promo=promo_big)

        assert order.subtotal == 30.0
        assert order.discount == 30.0  # Capped at subtotal, not 100.0
        assert order.total == 0.0      # Non-negative
        assert order.total >= 0.0

    def test_7_welcome50_fixed_discount_after_promo_fix(self):
        """7. WELCOME50 fixed discount (flat ₹50 off)."""
        promo_welcome50 = PromoCode("WELCOME50", "50 off", "flat", 50.0, 0.0)
        item = MenuItem("i1", "Veg Biryani", "Paradise", 4.6, 220.0, 35)
        order = Order(user_phone="+919900011122", item=item, promo=promo_welcome50)

        assert order.subtotal == 220.0
        assert order.discount == 50.0
        assert order.total == 170.0
