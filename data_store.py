"""Stand-in for a real DB layer. Swap this module out for a Postgres/Mongo
repository later without touching recommender.py or conversation.py."""
from models import MenuItem, User, PromoCode

MENU: list[MenuItem] = [
    MenuItem("i1", "Veg Biryani", "Paradise Biryani", 4.6, 220, 35, ["biryani", "veg"]),
    MenuItem("i2", "Veg Biryani", "Biryani Blues", 4.3, 199, 40, ["biryani", "veg"]),
    MenuItem("i3", "Veg Biryani", "Meghana Foods", 4.7, 250, 30, ["biryani", "veg"]),
    MenuItem("i4", "Veg Biryani", "Street Cart Express", 3.8, 150, 25, ["biryani", "veg", "budget"]),
    MenuItem("i5", "Hyderabadi Veg Biryani", "Shah Ghouse", 4.4, 240, 45, ["biryani", "veg"]),
    MenuItem("i6", "Chicken Biryani", "Paradise Biryani", 4.8, 280, 35, ["biryani", "non-veg"]),
    MenuItem("i7", "Masala Dosa", "Vidyarthi Bhavan", 4.9, 90, 20, ["south-indian", "veg"]),
    MenuItem("i8", "Paneer Butter Masala", "Punjabi Rasoi", 4.2, 210, 30, ["north-indian", "veg"]),
]

USERS: dict[str, User] = {
    "+919900011122": User(
        phone="+919900011122",
        name="Asha",
        past_item_ids=["i2", "i1"],   # has ordered these veg biryanis before
        default_address="204, Palm Residency, Indiranagar, Bengaluru",
    ),
}

PROMO_CODES: dict[str, PromoCode] = {
    "WELCOME50": PromoCode("WELCOME50", "50 off on your first order", "flat", 50.0, 0.0),
    "SAVE15": PromoCode("SAVE15", "15% off on orders above 200", "percentage", 0.15, 200.0),
    "SAVE10": PromoCode("SAVE10", "10% off, no minimum", "percentage", 0.10, 0.0),
}


def get_or_create_user(phone: str) -> User:
    if phone not in USERS:
        USERS[phone] = User(phone=phone, name="Guest")
    return USERS[phone]
