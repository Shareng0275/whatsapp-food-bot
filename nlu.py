"""Natural Language Understanding (NLU) Module for WhatsApp Food Ordering Bot.

Architecture:
- High-performance, zero-latency, rule-and-regex deterministic slot extractor
- Intent classification with confidence scoring
- Multi-entity extraction: food items, cuisines, price budget, dietary preferences,
  ordinals/selection, quantity, negation, and sentiments
- Pluggable interface for optional LLM/embedding fallback on low confidence
- Strictly isolates interpretation from business logic and financial calculations
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple


class Intent(str, Enum):
    """Supported conversational intents."""
    GREETING = "greeting"
    SEARCH_FOOD = "search_food"
    SELECT_OPTION = "select_option"
    PROVIDE_ADDRESS = "provide_address"
    APPLY_PROMO = "apply_promo"
    SKIP_STEP = "skip_step"
    CANCEL_ORDER = "cancel_order"
    MODIFY_ORDER = "modify_order"
    CHECK_STATUS = "check_status"
    AFFIRM = "affirm"
    DENY = "deny"
    HELP = "help"
    UNKNOWN = "unknown"


@dataclass
class ExtractedEntities:
    """Strongly-typed entity container extracted from conversational turns."""
    food_item: Optional[str] = None
    cuisine: Optional[str] = None
    dietary: Optional[str] = None           # 'veg', 'non-veg', 'vegan', 'jain'
    max_price: Optional[float] = None
    min_price: Optional[float] = None
    sort_by: Optional[str] = None           # 'rating_desc', 'price_asc', 'eta_asc'
    flavor: Optional[str] = None            # 'spicy', 'sweet', 'crispy', etc.
    selection_index: Optional[int] = None   # 1, 2, 3
    quantity: int = 1
    negations: List[str] = field(default_factory=list)      # e.g. ['non-veg', 'spicy']
    promo_code: Optional[str] = None
    use_saved_address: bool = False
    raw_address: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None and v != [] and v != 1}


@dataclass
class NLUResult:
    """Standardized NLU output envelope."""
    intent: Intent
    confidence: float
    entities: ExtractedEntities
    sentiment: str = "neutral"              # 'positive', 'neutral', 'negative'
    raw_text: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "intent": self.intent.value,
            "confidence": round(self.confidence, 2),
            "entities": self.entities.to_dict(),
            "sentiment": self.sentiment,
            "raw_text": self.raw_text,
        }


# ---------------------------------------------------------------------------
# Lexicon & Taxonomy Dictionaries
# ---------------------------------------------------------------------------

CUISINES = {
    "biryani": ["biryani", "biriyani", "briyani"],
    "chinese": ["chinese", "noodles", "fried rice", "manchurian", "momos"],
    "south-indian": ["south indian", "dosa", "idli", "idly", "vada", "sambar"],
    "north-indian": ["north indian", "paneer", "roti", "naan", "dal", "curry", "paratha", "parotta"],
    "italian": ["italian", "pizza", "pasta"],
    "fast-food": ["burger", "sandwich", "fries", "rolls"],
    "dessert": ["dessert", "ice cream", "sweet", "gulab jamun"],
}

FOOD_SYNONYMS = {
    "veg biryani": ["veg biryani", "vegetable biryani", "veg biriyani"],
    "chicken biryani": ["chicken biryani", "murgh biryani", "non veg biryani"],
    "hyderabadi veg biryani": ["hyderabadi veg biryani", "hyderabadi biryani"],
    "masala dosa": ["masala dosa", "masaledar dosa", "dosa"],
    "paneer butter masala": ["paneer butter masala", "paneer masala", "paneer butter", "paneer"],
    "pizza": ["pizza", "margherita", "veggie pizza"],
    "noodles": ["noodles", "hakka noodles", "veg noodles"],
}

ORDINALS = {
    "1": 1, "first": 1, "1st": 1, "one": 1, "option 1": 1, "the first one": 1, "number 1": 1,
    "2": 2, "second": 2, "2nd": 2, "two": 2, "option 2": 2, "the second one": 2, "number 2": 2,
    "3": 3, "third": 3, "3rd": 3, "three": 3, "option 3": 3, "the third one": 3, "number 3": 3,
}

POSITIVE_SENTIMENT = {"great", "good", "love", "awesome", "perfect", "please", "thanks", "thank you", "nice", "best"}
NEGATIVE_SENTIMENT = {"bad", "worst", "terrible", "hate", "horrible", "angry", "slow", "wrong", "cancel"}


# ---------------------------------------------------------------------------
# NLUEngine Implementation
# ---------------------------------------------------------------------------

class NLUEngine:
    """Hybrid NLU Engine providing fast deterministic rule/regex parsing

    with slot extraction and fallback boundary triggers.
    """

    def __init__(self, confidence_threshold: float = 0.65):
        self.confidence_threshold = confidence_threshold

    def parse(self, text: str, current_state: Optional[str] = None) -> NLUResult:
        """Parse raw user input into an NLUResult envelope."""
        clean_text = text.strip()
        lower = clean_text.lower()
        entities = ExtractedEntities()

        if not clean_text:
            return NLUResult(intent=Intent.UNKNOWN, confidence=0.0, entities=entities, raw_text=clean_text)

        # 1. Sentiment Detection
        words = set(re.findall(r"\b\w+\b", lower))
        sentiment = "neutral"
        if words & POSITIVE_SENTIMENT:
            sentiment = "positive"
        elif words & NEGATIVE_SENTIMENT:
            sentiment = "negative"

        # 2. Extract Dietary & Negation
        self._extract_dietary_and_negation(lower, entities)

        # 3. Extract Budget & Price Limits
        self._extract_budget(lower, entities)

        # 4. Extract Flavors & Attributes
        self._extract_flavors_and_sorting(lower, entities)

        # 5. Extract Cuisine & Food Items
        self._extract_food_and_cuisine(lower, entities)

        # 6. Extract Ordinals / Selection Index
        self._extract_selection_index(lower, entities)

        # 7. Intent Classification
        intent, confidence = self._classify_intent(lower, entities, current_state)

        return NLUResult(
            intent=intent,
            confidence=confidence,
            entities=entities,
            sentiment=sentiment,
            raw_text=clean_text,
        )

    def _extract_dietary_and_negation(self, text: str, entities: ExtractedEntities) -> None:
        """Extract dietary tags and handle explicit negations (e.g., 'not non-veg')."""
        # Negation patterns: 'not X', 'no X', 'without X', 'except X'
        neg_matches = re.findall(r"\b(?:not|no|without|except)\s+([a-zA-Z\-]+)", text)
        for neg in neg_matches:
            entities.negations.append(neg)

        # Dietary preference
        if "pure veg" in text or "strictly veg" in text or "vegetarian" in text:
            entities.dietary = "veg"
        elif re.search(r"\bnon[\-\s]?veg\b", text):
            if "non-veg" in entities.negations or "nonveg" in entities.negations:
                entities.dietary = "veg"
            else:
                entities.dietary = "non-veg"
        elif re.search(r"\bveg\b", text) and "veg" not in entities.negations:
            entities.dietary = "veg"
        elif "vegan" in text:
            entities.dietary = "vegan"
        elif "jain" in text:
            entities.dietary = "jain"
        elif any(meat in text for meat in ["chicken", "mutton", "fish", "meat", "egg", "prawns"]):
            entities.dietary = "non-veg"

    def _extract_budget(self, text: str, entities: ExtractedEntities) -> None:
        """Extract price constraints like 'under 500', 'below 200', 'cheap'."""
        # Regex for 'under 500', 'below 300', 'less than 250', 'within 400', '<= 500'
        match_under = re.search(r"\b(?:under|below|less than|within|max|budget of)\s*(?:rs\.?|inr|₹)?\s*(\d+)", text)
        if match_under:
            entities.max_price = float(match_under.group(1))

        match_cheap = re.search(r"\b(cheap|budget|affordable|inexpensive|low price|lowest price)\b", text)
        if match_cheap:
            entities.sort_by = "price_asc"
            if entities.max_price is None:
                entities.max_price = 250.0  # Safe default budget heuristic

    def _extract_flavors_and_sorting(self, text: str, entities: ExtractedEntities) -> None:
        """Extract flavor preferences ('spicy') and sorting directives ('best rated')."""
        if re.search(r"\b(spicy|hot|masaledar|extra spicy)\b", text):
            if "spicy" not in entities.negations:
                entities.flavor = "spicy"
        elif re.search(r"\b(sweet|dessert|mithai)\b", text):
            entities.flavor = "sweet"

        if re.search(r"\b(best rated|top rated|highest rated|popular|best)\b", text):
            entities.sort_by = "rating_desc"
        elif re.search(r"\b(quick|fast|fastest|urgent|eta)\b", text):
            entities.sort_by = "eta_asc"

    def _extract_food_and_cuisine(self, text: str, entities: ExtractedEntities) -> None:
        """Extract matching food items and cuisines from known taxonomies."""
        # 1. Exact canonical food items
        for canonical, synonyms in FOOD_SYNONYMS.items():
            for syn in synonyms:
                if re.search(rf"\b{re.escape(syn)}\b", text):
                    entities.food_item = canonical
                    break
            if entities.food_item:
                break

        # 2. Cuisines
        for cuisine_name, keywords in CUISINES.items():
            for kw in keywords:
                if re.search(rf"\b{re.escape(kw)}\b", text):
                    entities.cuisine = cuisine_name
                    if not entities.food_item:
                        entities.food_item = kw
                    break
            if entities.cuisine:
                break

    def _extract_selection_index(self, text: str, entities: ExtractedEntities) -> None:
        """Extract numeric or ordinal options (e.g. '1', 'the second one', 'option 3')."""
        clean = text.strip()
        if clean in ("1", "2", "3"):
            entities.selection_index = int(clean)
            return

        # Sort patterns by length descending so multi-word phrases match before single words (e.g. 'the second one' before 'one')
        sorted_ordinals = sorted(ORDINALS.items(), key=lambda kv: len(kv[0]), reverse=True)
        for phrase, idx in sorted_ordinals:
            if re.search(rf"\b{re.escape(phrase)}\b", text):
                entities.selection_index = idx
                return

    def _classify_intent(
        self,
        text: str,
        entities: ExtractedEntities,
        current_state: Optional[str],
    ) -> Tuple[Intent, float]:
        """Classify user intent with context-aware heuristics and confidence scoring."""
        # Greetings & Restart
        if re.search(r"^(hi|hello|hey|start|restart|menu|hola)\b", text):
            return Intent.GREETING, 0.99

        # Cancellation
        if re.search(r"\b(cancel|abort|stop order|cancel my order|dont want to order|terminate)\b", text):
            return Intent.CANCEL_ORDER, 0.95

        # Order Modification / Address Change
        if re.search(r"\b(change my address|change address|update address|wrong address|different address)\b", text):
            return Intent.MODIFY_ORDER, 0.90

        # Skip directive
        if text in ("skip", "next", "none", "no promo", "no code", "continue without code"):
            return Intent.SKIP_STEP, 0.98

        # Saved Address directive
        if text in ("same", "saved", "my address", "default address", "use saved address"):
            entities.use_saved_address = True
            return Intent.PROVIDE_ADDRESS, 0.98

        # Affirm / Deny
        if text in ("yes", "yep", "yeah", "sure", "ok", "confirm", "proceed"):
            return Intent.AFFIRM, 0.95
        if text in ("no", "nope", "dont", "cancel"):
            return Intent.DENY, 0.90

        # Help request
        if re.search(r"\b(help|support|agent|contact|customer care)\b", text):
            return Intent.HELP, 0.95

        # Check Order Status
        if re.search(r"\b(track|status|where is my food|where is my order|order status)\b", text):
            return Intent.CHECK_STATUS, 0.90

        # Context-dependent disambiguation based on current conversation state
        if current_state == "SELECTING":
            if entities.selection_index is not None:
                return Intent.SELECT_OPTION, 0.95
            if entities.food_item or entities.cuisine or entities.dietary:
                # User is changing their search query instead of picking 1/2/3
                return Intent.SEARCH_FOOD, 0.85

        if current_state == "ADDRESS":
            # If user provides text that isn't a food search, treat as address
            if not entities.food_item and len(text) >= 5:
                entities.raw_address = text
                return Intent.PROVIDE_ADDRESS, 0.88

        if current_state == "PROMO":
            # Typical promo codes: alphanumeric 4-12 uppercase characters
            candidate_code = text.replace(" ", "").upper()
            if re.match(r"^[A-Z0-9]{4,12}$", candidate_code):
                entities.promo_code = candidate_code
                return Intent.APPLY_PROMO, 0.92

        # General Food Search
        if entities.food_item or entities.cuisine or entities.dietary or entities.max_price or entities.flavor:
            confidence = 0.85
            if entities.food_item and entities.dietary:
                confidence = 0.95
            return Intent.SEARCH_FOOD, confidence

        # Fallback query: If text has length and no other intent, treat as prospective food search query
        if len(text) >= 2:
            entities.food_item = text
            return Intent.SEARCH_FOOD, 0.60

        return Intent.UNKNOWN, 0.30
