"""Transport-agnostic conversation and dialogue management engine.

Architecture & Separation of Concerns:
1. State: Canonical FSM enum (SEARCHING, SELECTING, ADDRESS, PROMO, CONFIRMED)
2. ConversationContext: Typed session state snapshot with undo/back history stack
3. CommandHandler: Global command dispatcher (cancel, back, help, status, digressions)
4. ResponseBuilder: Pure view formatting and template generator
5. Session: Main dialogue coordinator linking NLU, FSM, and persistence
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Tuple

from models import MenuItem, Order, PromoCode, User
from nlu import Intent, NLUEngine
from recommender import recommend


# ---------------------------------------------------------------------------
# 1. State Model & Transitions
# ---------------------------------------------------------------------------

class State(Enum):
    """Conversational dialogue states."""
    SEARCHING = auto()      # Waiting for food search query
    SELECTING = auto()      # Displayed options, waiting for choice (1, 2, 3 or ordinal)
    ADDRESS = auto()        # Waiting for delivery address (or 'same' for saved address)
    PROMO = auto()          # Waiting for promo code or 'skip'
    CONFIRMED = auto()      # Order complete; next message restarts flow


@dataclass
class StateSnapshot:
    """Snapshot of session state for undo / back navigation."""
    state: State
    last_options: List[MenuItem] = field(default_factory=list)
    item: Optional[MenuItem] = None
    address: Optional[str] = None
    promo: Optional[PromoCode] = None


# ---------------------------------------------------------------------------
# 2. Response Builder (View / Presentation Layer)
# ---------------------------------------------------------------------------

class ResponseBuilder:
    """Centralized formatting for all user-facing conversational messages."""

    @staticmethod
    def welcome() -> str:
        return (
            "Hi! I'm your food ordering assistant. "
            "What would you like to eat today? (e.g. 'Veg Biryani', 'something spicy', 'pizza')"
        )

    @staticmethod
    def search_results(query: str, options: List[MenuItem], user: User) -> str:
        lines = [f"Here are the top options for '{query}':"]
        for i, item in enumerate(options, start=1):
            tag = " (you've ordered this before ⭐)" if item.item_id in user.past_item_ids else ""
            lines.append(
                f"{i}. {item.name} — {item.restaurant} | {item.rating}★ | "
                f"₹{item.price} | {item.eta_minutes} min{tag}"
            )
        lines.append("Reply with 1, 2, or 3 to select.")
        return "\n".join(lines)

    @staticmethod
    def no_results(query: str) -> str:
        return f"Sorry, I couldn't find any 4★+ options for '{query}'. Try another dish name?"

    @staticmethod
    def selection_prompt(chosen: MenuItem, user: User) -> str:
        prompt = f"Great choice — {chosen.name} from {chosen.restaurant} (₹{chosen.price}).\n"
        if user.default_address:
            prompt += f"Deliver to your saved address ({user.default_address})? Reply 'same' or type a new address."
        else:
            prompt += "Please share your delivery address."
        return prompt

    @staticmethod
    def invalid_selection() -> str:
        return "Please reply with 1, 2, or 3 to pick an option."

    @staticmethod
    def address_missing_saved() -> str:
        return "You don't have a saved address. Please type your delivery address."

    @staticmethod
    def address_prompt(user: User) -> str:
        if user.default_address:
            return f"Please provide a delivery address, or reply 'same' to use your saved address ({user.default_address})."
        return "Please provide a valid delivery address to proceed."

    @staticmethod
    def promo_prompt() -> str:
        return (
            "Got it. Do you have a promo code? Reply with the code, "
            "or 'skip' to continue without one.\n"
            "(Available: SAVE15, SAVE10, WELCOME50)"
        )

    @staticmethod
    def invalid_promo(code: str) -> str:
        return f"'{code}' isn't a valid code. Try again or reply 'skip'."

    @staticmethod
    def promo_limit_reached(code: str) -> str:
        return f"'{code}' isn't a valid code. Maximum attempts reached. Please reply 'skip' to continue."

    @staticmethod
    def promo_minimum_order(code: str, min_value: float) -> str:
        return f"'{code}' needs a minimum order of ₹{min_value}. Try another code or reply 'skip'."

    @staticmethod
    def order_summary(order: Order) -> str:
        lines = [
            "✅ Order confirmed!",
            f"Order ID: {order.order_id}",
            f"Item: {order.item.name} ({order.item.restaurant})",
            f"Deliver to: {order.address}",
            f"Subtotal: ₹{order.subtotal:.2f}",
        ]
        if order.promo:
            lines.append(f"Discount ({order.promo.code}): -₹{order.discount:.2f}")
        lines.append(f"Total: ₹{order.total:.2f}")
        lines.append(f"ETA: ~{order.item.eta_minutes} minutes")
        lines.append("\nType 'hi' to place another order.")
        return "\n".join(lines)

    @staticmethod
    def cancel_response() -> str:
        return "🚫 Order cancelled. What would you like to eat today? (e.g. 'Veg Biryani')"

    @staticmethod
    def back_response(new_state: State, chosen_item: Optional[MenuItem] = None) -> str:
        if new_state == State.SEARCHING:
            return "🔙 Returned to search. What would you like to eat today?"
        if new_state == State.SELECTING:
            return "🔙 Returned to selection. Reply with 1, 2, or 3 to pick an option."
        if new_state == State.ADDRESS:
            item_desc = f" for {chosen_item.name}" if chosen_item else ""
            return f"🔙 Returned to address step{item_desc}. Please provide delivery address or reply 'same'."
        return "🔙 Returned to previous step."

    @staticmethod
    def clarification_response(current_state: State) -> str:
        return (
            "No problem! Would you like to pick a different dish, change your address, or cancel?\n"
            "• Type 'back' to go to the previous step\n"
            "• Type 'cancel' to start over\n"
            "• Or type a new food item (e.g. 'pizza', 'dosa')"
        )

    @staticmethod
    def help_response(current_state: State) -> str:
        if current_state == State.SEARCHING:
            return (
                "ℹ️ *Search Help*: You can search by dish name ('Veg Biryani'), "
                "cuisine ('Chinese', 'South Indian'), dietary preference ('veg', 'non-veg'), "
                "or budget ('under 500', 'cheap'). Type 'cancel' at any time to start over."
            )
        if current_state == State.SELECTING:
            return "ℹ️ *Selection Help*: Reply with 1, 2, or 3 to choose a dish, or type a new dish name to search again."
        if current_state == State.ADDRESS:
            return "ℹ️ *Address Help*: Reply 'same' to deliver to your saved address, or type your complete delivery address."
        if current_state == State.PROMO:
            return "ℹ️ *Promo Help*: Enter a promo code (SAVE15, SAVE10, WELCOME50) or reply 'skip' to order without discounts."
        return "ℹ️ Type 'hi' to start a new order or 'cancel' to stop."

    @staticmethod
    def status_response(order: Order) -> str:
        if order.order_id:
            return f"📦 Order #{order.order_id} ({order.item.name if order.item else 'Item'}) status: {order.status.upper()} (Payment: {order.payment_status.upper()})."
        return "You don't have an active in-flight order. Type a food name to start ordering!"


# ---------------------------------------------------------------------------
# 3. Command & Digression Handler
# ---------------------------------------------------------------------------

class CommandHandler:
    """Interception handler for global commands, cancellations, back navigation,

    and intent digressions (e.g., changing food items mid-order).
    """

    @staticmethod
    def is_greeting(text: str) -> bool:
        return text.lower() in ("hi", "hello", "hey", "start", "restart", "menu", "hola")

    @staticmethod
    def is_cancel(text: str) -> bool:
        return text.lower() in ("cancel", "abort", "stop", "terminate", "exit", "cancel order", "stop order")

    @staticmethod
    def is_back(text: str) -> bool:
        return text.lower() in ("back", "previous", "prev", "undo", "go back", "return")

    @staticmethod
    def is_help(text: str) -> bool:
        return text.lower() in ("help", "info", "support", "commands", "options", "?")

    @staticmethod
    def is_status(text: str) -> bool:
        return text.lower() in ("status", "track", "track order", "where is my order", "order status")

    @staticmethod
    def is_mind_change(text: str) -> bool:
        lower = text.lower()
        return lower in (
            "no", "wait", "i changed my mind", "changed my mind", "hold on",
            "not this", "wrong", "different", "never mind", "nevermind"
        )

    @staticmethod
    def extract_digression_query(text: str) -> Optional[str]:
        """Detect if user changed their mind to a different food mid-flow.

        e.g. 'Actually I want pizza instead', 'show me chinese instead', 'i want dosa'
        """
        lower = text.lower()
        patterns = [
            r"(?:actually\s+)?(?:i\s+want|give\s+me|show\s+me|order|switch\s+to|change\s+to)\s+([a-zA-Z\s\-]+?)(?:\s+instead|\s+now)?$",
            r"(?:instead\s+of\s+\w+,\s*)?(?:i\s+want|get\s+me|show\s+me)\s+([a-zA-Z\s\-]+)$",
        ]
        for pat in patterns:
            m = re.match(pat, lower)
            if m:
                extracted = m.group(1).strip()
                if extracted and extracted not in ("to cancel", "to go back", "help"):
                    return extracted
        return None


# ---------------------------------------------------------------------------
# 4. Main Session Dialogue Coordinator
# ---------------------------------------------------------------------------

class Session:
    """One session per user phone number.

    Coordinates conversational state machine, intent parsing, candidate ranking,
    undo/back navigation, and persistence.
    """

    def __init__(self, phone: str, repo):
        self.phone = phone
        self.repo = repo
        self.user: User = repo.get_or_create_user(phone)
        self.state: State = State.SEARCHING
        self.order: Order = Order(user_phone=phone)
        self._last_options: List[MenuItem] = []
        self._state_history: List[StateSnapshot] = []
        self._menu: Optional[List[MenuItem]] = None
        self._promos: Optional[Dict[str, PromoCode]] = None
        self._promo_attempts: int = 0
        self.nlu = NLUEngine()

    def _get_menu(self) -> List[MenuItem]:
        if self._menu is None:
            self._menu = self.repo.get_menu()
        return self._menu

    def _get_promos(self) -> Dict[str, PromoCode]:
        if self._promos is None:
            self._promos = self.repo.get_all_promos()
        return self._promos

    def _save_snapshot(self) -> None:
        """Save current conversational state snapshot for back navigation."""
        snapshot = StateSnapshot(
            state=self.state,
            last_options=list(self._last_options),
            item=self.order.item,
            address=self.order.address,
            promo=self.order.promo,
        )
        self._state_history.append(snapshot)
        # Limit history stack depth to 5 snapshots
        if len(self._state_history) > 5:
            self._state_history.pop(0)

    def reset(self) -> None:
        """Reset conversation and in-progress order to initial clean state."""
        self.state = State.SEARCHING
        self.order = Order(user_phone=self.phone)
        self._last_options = []
        self._state_history.clear()
        self._promo_attempts = 0

    def handle_message(self, text: str) -> str:
        """Process incoming user message through command interceptors and FSM."""
        text = text.strip()

        # 1. Global Command: Greeting / Restart
        if CommandHandler.is_greeting(text):
            self.reset()
            return ResponseBuilder.welcome()

        # 2. Global Command: Cancel
        if CommandHandler.is_cancel(text):
            self.reset()
            return ResponseBuilder.cancel_response()

        # 3. Global Command: Help
        if CommandHandler.is_help(text):
            return ResponseBuilder.help_response(self.state)

        # 4. Global Command: Status / Track
        if CommandHandler.is_status(text):
            return ResponseBuilder.status_response(self.order)

        # 5. Global Command: Back / Undo Navigation
        if CommandHandler.is_back(text):
            return self._handle_back()

        # 6. Intent Interruption: Mind Change / Clarification ("no", "wait", "changed my mind")
        if CommandHandler.is_mind_change(text) and self.state in (State.SELECTING, State.ADDRESS, State.PROMO):
            return ResponseBuilder.clarification_response(self.state)

        # 7. Intent Interruption: Mid-flow Search Digression ("Actually I want pizza instead")
        digressed_query = CommandHandler.extract_digression_query(text)
        if digressed_query and self.state in (State.SELECTING, State.ADDRESS, State.PROMO):
            self._save_snapshot()
            self.state = State.SEARCHING
            self.order.item = None
            return self._handle_searching(digressed_query)

        # 8. State Machine Dispatch
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

    # ---- State Transition Handlers --------------------------------------

    def _handle_back(self) -> str:
        """Navigate to the previous conversational state."""
        if not self._state_history:
            # If at beginning or no history, return to search
            self.reset()
            return ResponseBuilder.back_response(State.SEARCHING)

        prev = self._state_history.pop()
        self.state = prev.state
        self._last_options = prev.last_options
        self.order.item = prev.item
        self.order.address = prev.address
        self.order.promo = prev.promo
        return ResponseBuilder.back_response(self.state, self.order.item)

    def _handle_searching(self, text: str) -> str:
        options = recommend(text, self.user, self._get_menu(), top_n=3)
        if not options:
            return ResponseBuilder.no_results(text)

        self._save_snapshot()
        self._last_options = options
        self.state = State.SELECTING
        return ResponseBuilder.search_results(text, options, self.user)

    def _handle_selecting(self, text: str) -> str:
        # Check ordinal selection or digit
        selection_idx: Optional[int] = None
        if text in ("1", "2", "3"):
            selection_idx = int(text)
        else:
            # Use NLU to extract ordinals (e.g. 'first', 'the second one', 'option 3')
            nlu_res = self.nlu.parse(text, current_state="SELECTING")
            if nlu_res.entities.selection_index in (1, 2, 3):
                selection_idx = nlu_res.entities.selection_index

        if selection_idx is None or selection_idx > len(self._last_options):
            return ResponseBuilder.invalid_selection()

        self._save_snapshot()
        chosen = self._last_options[selection_idx - 1]
        self.order.item = chosen
        self.state = State.ADDRESS
        return ResponseBuilder.selection_prompt(chosen, self.user)

    def _handle_address(self, text: str) -> str:
        if not text:
            return ResponseBuilder.address_prompt(self.user)

        lower = text.lower()
        if lower in ("same", "same address", "my address", "saved address", "default"):
            if self.user.default_address:
                self._save_snapshot()
                self.order.address = self.user.default_address
            else:
                return ResponseBuilder.address_missing_saved()
        else:
            self._save_snapshot()
            self.order.address = text

        self.state = State.PROMO
        return ResponseBuilder.promo_prompt()

    def _handle_promo(self, text: str) -> str:
        promos = self._get_promos()
        max_attempts = int(os.environ.get("PROMO_ATTEMPT_LIMIT", 5))

        if text.lower() not in ("skip", "no promo", "none", "no"):
            if self._promo_attempts >= max_attempts:
                return "Too many invalid promo attempts. Please reply 'skip' to continue without a promo code."

            code = text.strip().upper()
            promo = promos.get(code)
            if not promo:
                self._promo_attempts += 1
                if self._promo_attempts >= max_attempts:
                    return ResponseBuilder.promo_limit_reached(text)
                return ResponseBuilder.invalid_promo(text)

            if self.order.subtotal < promo.min_order_value:
                self._promo_attempts += 1
                return ResponseBuilder.promo_minimum_order(code, promo.min_order_value)

            self.order.promo = promo

        self.state = State.CONFIRMED
        # Persist the order and update user history
        if self.order.item:
            self.user.past_item_ids.append(self.order.item.item_id)
        order_id = self.repo.create_order(self.order)
        self.repo.update_user(self.user)
        self.order.order_id = order_id
        return ResponseBuilder.order_summary(self.order)
