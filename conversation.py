"""Transport-agnostic conversation engine. Both bot.py (real WhatsApp webhook)
and simulate.py (CLI demo) drive this same class, so the flow logic is written
and tested exactly once.
"""
from enum import Enum, auto

from models import Order, MenuItem
from recommender import recommend


class State(Enum):
    SEARCHING = auto()      # waiting for a food item query
    SELECTING = auto()       # showed 3 options, waiting for 1/2/3
    ADDRESS = auto()         # waiting for delivery address (or "same" for default)
    PROMO = auto()           # waiting for a promo code or "skip"
    CONFIRMED = auto()       # order complete; a new message restarts the flow


class Session:
    """One session per user phone number. Holds conversation state + the in-progress order.

    Parameters
    ----------
    phone : str
        The user's phone number.
    repo : repository.Repository
        Data-access object for users, menu, promos, and orders.
    """

    def __init__(self, phone: str, repo):
        self.phone = phone
        self.repo = repo
        self.user = repo.get_or_create_user(phone)
        self.state = State.SEARCHING
        self.order = Order(user_phone=phone)
        self._last_options: list[MenuItem] = []
        # Cache menu and promos per session to avoid repeated DB round-trips
        # during a single conversation.
        self._menu: list[MenuItem] | None = None
        self._promos: dict | None = None
        self._promo_attempts: int = 0

    def _get_menu(self) -> list[MenuItem]:
        if self._menu is None:
            self._menu = self.repo.get_menu()
        return self._menu

    def _get_promos(self) -> dict:
        if self._promos is None:
            self._promos = self.repo.get_all_promos()
        return self._promos

    def reset(self):
        self.state = State.SEARCHING
        self.order = Order(user_phone=self.phone)
        self._last_options = []
        self._promo_attempts = 0

    def handle_message(self, text: str) -> str:
        text = text.strip()

        if text.lower() in ("hi", "hello", "start", "restart"):
            self.reset()
            return ("Hi! I'm your food ordering assistant. "
                    "What would you like to eat today? (e.g. 'Veg Biryani')")

        if self.state == State.SEARCHING:
            return self._handle_searching(text)
        if self.state == State.SELECTING:
            return self._handle_selecting(text)
        if self.state == State.ADDRESS:
            return self._handle_address(text)
        if self.state == State.PROMO:
            return self._handle_promo(text)
        if self.state == State.CONFIRMED:
            # Any message after confirmation starts a fresh order
            self.reset()
            return self._handle_searching(text)

        return "Sorry, something went wrong. Type 'hi' to start over."

    # ---- state handlers -------------------------------------------------

    def _handle_searching(self, text: str) -> str:
        options = recommend(text, self.user, self._get_menu(), top_n=3)
        if not options:
            return (f"Sorry, I couldn't find any 4★+ options for '{text}'. "
                     "Try another dish name?")
        self._last_options = options
        self.state = State.SELECTING
        lines = [f"Here are the top options for '{text}':"]
        for i, item in enumerate(options, start=1):
            tag = " (you've ordered this before ⭐)" if item.item_id in self.user.past_item_ids else ""
            lines.append(f"{i}. {item.name} — {item.restaurant} | {item.rating}★ | "
                         f"₹{item.price} | {item.eta_minutes} min{tag}")
        lines.append("Reply with 1, 2, or 3 to select.")
        return "\n".join(lines)

    def _handle_selecting(self, text: str) -> str:
        if text not in ("1", "2", "3") or int(text) > len(self._last_options):
            return "Please reply with 1, 2, or 3 to pick an option."
        chosen = self._last_options[int(text) - 1]
        self.order.item = chosen
        self.state = State.ADDRESS
        prompt = f"Great choice — {chosen.name} from {chosen.restaurant} (₹{chosen.price}).\n"
        if self.user.default_address:
            prompt += f"Deliver to your saved address ({self.user.default_address})? Reply 'same' or type a new address."
        else:
            prompt += "Please share your delivery address."
        return prompt

    def _handle_address(self, text: str) -> str:
        if not text:
            if self.user.default_address:
                return f"Please provide a delivery address, or reply 'same' to use your saved address ({self.user.default_address})."
            return "Please provide a valid delivery address to proceed."
        if text.lower() == "same":
            if self.user.default_address:
                self.order.address = self.user.default_address
            else:
                return "You don't have a saved address. Please type your delivery address."
        else:
            self.order.address = text
        self.state = State.PROMO
        return ("Got it. Do you have a promo code? Reply with the code, "
                "or 'skip' to continue without one.\n"
                "(Available: SAVE15, SAVE10, WELCOME50)")

    def _handle_promo(self, text: str) -> str:
        import os
        promos = self._get_promos()
        max_attempts = int(os.environ.get("PROMO_ATTEMPT_LIMIT", 5))

        if text.lower() != "skip":
            if self._promo_attempts >= max_attempts:
                return "Too many invalid promo attempts. Please reply 'skip' to continue without a promo code."
            code = text.strip().upper()
            promo = promos.get(code)
            if not promo:
                self._promo_attempts += 1
                if self._promo_attempts >= max_attempts:
                    return f"'{text}' isn't a valid code. Maximum attempts reached. Please reply 'skip' to continue."
                return f"'{text}' isn't a valid code. Try again or reply 'skip'."
            if self.order.subtotal < promo.min_order_value:
                self._promo_attempts += 1
                return (f"'{code}' needs a minimum order of ₹{promo.min_order_value}. "
                        "Try another code or reply 'skip'.")
            self.order.promo = promo

        self.state = State.CONFIRMED
        # Persist the order and update user history
        self.user.past_item_ids.append(self.order.item.item_id)
        order_id = self.repo.create_order(self.order)
        self.repo.update_user(self.user)
        self.order.order_id = order_id
        return self._order_summary()

    def _order_summary(self) -> str:
        o = self.order
        lines = [
            "✅ Order confirmed!",
            f"Order ID: {o.order_id}",
            f"Item: {o.item.name} ({o.item.restaurant})",
            f"Deliver to: {o.address}",
            f"Subtotal: ₹{o.subtotal:.2f}",
        ]
        if o.promo:
            lines.append(f"Discount ({o.promo.code}): -₹{o.discount:.2f}")
        lines.append(f"Total: ₹{o.total:.2f}")
        lines.append(f"ETA: ~{o.item.eta_minutes} minutes")
        lines.append("\nType 'hi' to place another order.")
        return "\n".join(lines)
