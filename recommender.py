"""Hybrid Food Recommendation Engine for WhatsApp Chatbot.

Architecture:
1. Query Normalization & Tokenization (stopwords, casing, punctuation)
2. Lexicon & Entity Extraction (cuisines, flavors, budget, dietary, personal history)
3. Typo Tolerance & Fuzzy Matching (difflib SequenceMatcher)
4. Hard & Soft Candidate Filtering (dietary, rating threshold, budget constraints)
5. Multi-Signal Explainable Scoring Function:
   - S_query: Query token overlap + name/tag matching
   - S_cuisine: Cuisine taxonomy alignment
   - S_dietary: Vegetarian/vegan/non-veg compliance
   - S_rating: Normalized quality score (Bayesian / min-max bounded)
   - S_popularity: Social proof / vote volume
   - S_personalization: Historical re-order & restaurant affinity boost
   - S_price: Budget fit / affordability curve
   - S_location: Proximity & delivery ETA efficiency
6. Maximal Marginal Relevance (MMR) & Intra-List Diversity
7. Natural Language Explanation Generation for transparent recommendation rationale
8. Graceful Fallback & Cold-Start Handling
"""
from __future__ import annotations

import difflib
import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from models import MenuItem, User

MIN_RATING = 4.0
MAX_PRICE_BASELINE = 1000.0
MAX_VOTES_BASELINE = 1000
MAX_ETA_BASELINE = 60.0

# ---------------------------------------------------------------------------
# Lexicons, Taxonomies, and Synonyms
# ---------------------------------------------------------------------------

STOPWORDS: Set[str] = {
    "i", "want", "some", "something", "give", "me", "show", "food", "dish",
    "dishes", "near", "for", "the", "a", "an", "please", "can", "order",
    "get", "deliver", "to", "in", "at", "with", "and", "or", "of", "like",
    "need", "find", "suggest", "recommend", "options", "looking", "good"
}

CUISINE_TAXONOMY: Dict[str, List[str]] = {
    "biryani": ["biryani", "biriyani", "briyani"],
    "south-indian": ["south-indian", "south indian", "dosa", "idli", "idly", "vada", "sambar", "uttapam"],
    "north-indian": ["north-indian", "north indian", "paneer", "roti", "naan", "dal", "curry", "paratha", "punjabi"],
    "chinese": ["chinese", "noodles", "fried rice", "manchurian", "momos", "hakka"],
    "italian": ["italian", "pizza", "pasta", "margherita"],
    "fast-food": ["fast-food", "burger", "sandwich", "fries", "rolls", "street"],
    "dessert": ["dessert", "ice cream", "sweet", "gulab jamun", "halwa"],
}

FLAVOR_SYNONYMS: Dict[str, List[str]] = {
    "spicy": ["spicy", "hot", "masaledar", "tikha", "chilli", "extra spicy"],
    "healthy": ["healthy", "light", "salad", "diet", "low calorie", "fresh", "nutritious"],
    "sweet": ["sweet", "mithai", "dessert", "sugary"],
    "crispy": ["crispy", "crunchy", "fried"],
}

FOOD_SYNONYMS: Dict[str, List[str]] = {
    "veg biryani": ["veg biryani", "vegetable biryani", "veg biriyani", "veg briyani"],
    "chicken biryani": ["chicken biryani", "murgh biryani", "non veg biryani", "chicken biriyani"],
    "hyderabadi veg biryani": ["hyderabadi veg biryani", "hyderabadi biryani"],
    "masala dosa": ["masala dosa", "masaledar dosa", "dosa", "plain dosa", "ghee roast"],
    "paneer butter masala": ["paneer butter masala", "paneer masala", "paneer butter", "paneer makhani", "paneer"],
    "pizza": ["pizza", "margherita", "veggie pizza", "cheese pizza"],
}


# ---------------------------------------------------------------------------
# Scoring Weights Configuration
# ---------------------------------------------------------------------------

@dataclass
class RecommendationWeights:
    """Configurable weights for the multi-signal hybrid scoring formula.
    
    Total weights sum to 1.0 for normalized linear combination.
    """
    query_relevance: float = 0.35
    cuisine_match: float = 0.15
    dietary_match: float = 0.10
    rating_signal: float = 0.15
    popularity_signal: float = 0.05
    personalization_signal: float = 0.10
    price_fit: float = 0.05
    location_fit: float = 0.05

    def validate(self) -> bool:
        total = sum([
            self.query_relevance, self.cuisine_match, self.dietary_match,
            self.rating_signal, self.popularity_signal, self.personalization_signal,
            self.price_fit, self.location_fit
        ])
        return math.isclose(total, 1.0, rel_tol=1e-2)


DEFAULT_WEIGHTS = RecommendationWeights()


# ---------------------------------------------------------------------------
# Query Intent & Entity Extraction for Recommender
# ---------------------------------------------------------------------------

@dataclass
class RecommenderQueryContext:
    raw_query: str
    cleaned_query: str
    tokens: List[str] = field(default_factory=list)
    semantic_tokens: List[str] = field(default_factory=list)
    cuisine: Optional[str] = None
    dietary: Optional[str] = None           # 'veg', 'non-veg', 'vegan'
    flavor: Optional[str] = None            # 'spicy', 'healthy', 'sweet'
    max_price: Optional[float] = None
    wants_history: bool = False             # "something I ordered before"
    wants_cheap: bool = False               # "cheap", "budget"
    wants_top_rated: bool = False           # "best rated", "top rated"
    wants_near_me: bool = False             # "near me", "quick"
    group_size: int = 1                     # "for two people"


def parse_recommender_query(query: str) -> RecommenderQueryContext:
    """Extract recommendation entities and intents from free-form user queries."""
    raw = query.strip()
    cleaned = raw.lower()
    
    # Emoji to food taxonomy mapping
    emoji_map = {
        "🍕": " pizza ",
        "🍔": " burger ",
        "🍟": " fries ",
        "🍜": " noodles ",
        "🍲": " soup ",
        "🍛": " curry ",
        "🍚": " biryani ",
        "🥗": " salad ",
        "🍦": " dessert ",
        "🍰": " cake ",
        "🥟": " momos ",
        "🌯": " rolls ",
    }
    for emo, text_rep in emoji_map.items():
        if emo in cleaned:
            cleaned = cleaned.replace(emo, text_rep)

    # Strip non-alphanumeric except hyphens and spaces
    normalized = re.sub(r"[^a-z0-9\s\-]", " ", cleaned)
    tokens = [w for w in normalized.split() if w]
    semantic_tokens = [w for w in tokens if w not in STOPWORDS]

    ctx = RecommenderQueryContext(
        raw_query=raw,
        cleaned_query=cleaned.strip(),
        tokens=tokens,
        semantic_tokens=semantic_tokens
    )

    if not ctx.cleaned_query or not ctx.tokens:
        return ctx

    # 1. Dietary Detection
    if re.search(r"\b(non[\-\s]?veg|chicken|meat|mutton|fish|prawn|egg)\b", cleaned):
        ctx.dietary = "non-veg"
    elif re.search(r"\b(veg|vegetarian|pure veg)\b", cleaned):
        ctx.dietary = "veg"
    elif "vegan" in cleaned:
        ctx.dietary = "vegan"

    # 2. Cuisine Detection
    for cuisine_key, synonyms in CUISINE_TAXONOMY.items():
        for syn in synonyms:
            if re.search(rf"\b{re.escape(syn)}\b", cleaned):
                ctx.cuisine = cuisine_key
                break
        if ctx.cuisine:
            break

    # 3. Flavor / Attribute Detection
    for flavor_key, synonyms in FLAVOR_SYNONYMS.items():
        for syn in synonyms:
            if re.search(rf"\b{re.escape(syn)}\b", cleaned):
                ctx.flavor = flavor_key
                break
        if ctx.flavor:
            break

    # 4. Budget & Price Constraints
    match_under = re.search(r"\b(?:under|below|less than|within|max|budget of)\s*(?:rs\.?|inr|₹)?\s*(\d+)", cleaned)
    if match_under:
        ctx.max_price = float(match_under.group(1))

    if re.search(r"\b(cheap|budget|affordable|low price|inexpensive)\b", cleaned):
        ctx.wants_cheap = True
        if ctx.max_price is None:
            ctx.max_price = 250.0

    # 5. History / Re-order Intent
    if re.search(r"\b(ordered before|previous|history|past|reorder|again|my usual|last time)\b", cleaned):
        ctx.wants_history = True

    # 6. Top-Rated / Proximity / Group size
    if re.search(r"\b(best rated|top rated|highest rated|popular|best)\b", cleaned):
        ctx.wants_top_rated = True

    if re.search(r"\b(near me|nearby|quick|fast|eta)\b", cleaned):
        ctx.wants_near_me = True

    if re.search(r"\b(for two|for 2|two people|couple|combo|sharing)\b", cleaned):
        ctx.group_size = 2

    return ctx


# ---------------------------------------------------------------------------
# Signal Calculation Functions
# ---------------------------------------------------------------------------

def _fuzzy_token_match(query_token: str, target_tokens: Set[str], threshold: float = 0.80) -> float:
    """Return highest fuzzy similarity score of query token against a set of target tokens."""
    best = 0.0
    for target in target_tokens:
        if query_token == target:
            return 1.0
        # Check substring match if meaningful
        if len(query_token) >= 4 and (query_token in target or target in query_token):
            best = max(best, 0.90)
            continue
        ratio = difflib.SequenceMatcher(None, query_token, target).ratio()
        if ratio >= threshold and ratio > best:
            best = ratio
    return best


def _compute_query_relevance(item: MenuItem, ctx: RecommenderQueryContext) -> float:
    """Compute query relevance score in [0.0, 1.0]."""
    if not ctx.cleaned_query:
        return 0.0

    item_name_lower = item.name.lower()
    item_tags_lower = [t.lower() for t in item.tags]
    item_words = set(f"{item_name_lower} {' '.join(item_tags_lower)}".split())

    # Check exact phrase match
    if ctx.cleaned_query == item_name_lower or ctx.cleaned_query in item_name_lower:
        return 1.0

    # Check canonical synonym mapping
    for canonical, syns in FOOD_SYNONYMS.items():
        if any(s in ctx.cleaned_query for s in syns):
            if canonical in item_name_lower or any(s in item_name_lower for s in syns):
                return 1.0

    tokens_to_match = ctx.semantic_tokens if ctx.semantic_tokens else ctx.tokens
    if not tokens_to_match:
        return 0.5  # Generic exploratory query ("something", "food")

    # Match each token (exact or fuzzy)
    match_scores: List[float] = []
    for token in tokens_to_match:
        score = _fuzzy_token_match(token, item_words)
        match_scores.append(score)

    # Average match score + bonus for full coverage
    avg_score = sum(match_scores) / len(match_scores) if match_scores else 0.0
    if all(s >= 0.80 for s in match_scores):
        avg_score = max(avg_score, 0.95)

    return min(avg_score, 1.0)


def _compute_cuisine_score(item: MenuItem, ctx: RecommenderQueryContext) -> float:
    """Compute cuisine alignment score in [0.0, 1.0]."""
    if not ctx.cuisine:
        return 0.5  # No specific cuisine requested

    item_tags = [t.lower() for t in item.tags]
    item_name = item.name.lower()
    item_cuisine = (item.cuisine or "").lower()

    if item_cuisine == ctx.cuisine or ctx.cuisine in item_tags or ctx.cuisine in item_name:
        return 1.0

    # Check taxonomy matches
    synonyms = CUISINE_TAXONOMY.get(ctx.cuisine, [])
    if any(s in item_name or any(s in t for t in item_tags) for s in synonyms):
        return 1.0

    return 0.0


def _compute_dietary_score(item: MenuItem, ctx: RecommenderQueryContext, user: User) -> float:
    """Compute dietary preference compliance score in [0.0, 1.0]."""
    item_tags = [t.lower() for t in item.tags]
    is_item_non_veg = "non-veg" in item_tags or "chicken" in item.name.lower() or "meat" in item.name.lower()
    is_item_veg = "veg" in item_tags and not is_item_non_veg

    # Explicit query preference takes top precedence
    if ctx.dietary == "veg":
        return 0.0 if is_item_non_veg else 1.0
    if ctx.dietary == "non-veg":
        return 1.0 if is_item_non_veg else 0.3

    # User profile default preference
    user_diet = getattr(user, "dietary_preference", None) or getattr(user, "dietary", None)
    if user_diet == "veg":
        return 0.0 if is_item_non_veg else 1.0

    return 0.5


def _compute_rating_score(item: MenuItem) -> float:
    """Normalize rating (typically 3.0 to 5.0) into [0.0, 1.0]."""
    clamped = max(3.0, min(5.0, item.rating))
    return (clamped - 3.0) / (5.0 - 3.0)


def _compute_popularity_score(item: MenuItem) -> float:
    """Compute log-scaled popularity / vote signal in [0.0, 1.0]."""
    votes = getattr(item, "votes", 50)
    log_votes = math.log(1 + max(0, votes))
    log_max = math.log(1 + MAX_VOTES_BASELINE)
    return min(1.0, log_votes / log_max)


def _compute_personalization_score(item: MenuItem, user: User, ctx: RecommenderQueryContext) -> float:
    """Compute user history and re-order affinity score in [0.0, 1.0]."""
    is_past_order = item.item_id in user.past_item_ids
    if ctx.wants_history:
        return 1.0 if is_past_order else 0.0

    if is_past_order:
        # Boost for repeat purchase
        order_count = user.past_item_ids.count(item.item_id)
        return min(1.0, 0.7 + (order_count * 0.15))

    return 0.0


def _compute_price_score(item: MenuItem, ctx: RecommenderQueryContext) -> float:
    """Compute budget alignment and affordability score in [0.0, 1.0]."""
    if ctx.max_price is not None:
        if item.price <= ctx.max_price:
            # Reward staying nicely within budget
            return 1.0 - (item.price / (ctx.max_price * 1.5))
        else:
            # Penalty for exceeding max budget
            overshoot = (item.price - ctx.max_price) / ctx.max_price
            return max(0.0, 0.5 - overshoot)

    if ctx.wants_cheap:
        # Inverted price curve: cheaper items score higher
        return max(0.0, 1.0 - (item.price / 500.0))

    # Standard normalized affordability
    return max(0.0, 1.0 - (item.price / MAX_PRICE_BASELINE))


def _compute_location_score(item: MenuItem, ctx: RecommenderQueryContext) -> float:
    """Compute delivery speed and proximity score in [0.0, 1.0]."""
    eta = max(10, min(60, item.eta_minutes))
    score = 1.0 - ((eta - 10) / (MAX_ETA_BASELINE - 10))
    if ctx.wants_near_me:
        return score * 1.2  # amplify speed
    return min(1.0, max(0.0, score))


# ---------------------------------------------------------------------------
# Candidate Scoring & Explanation
# ---------------------------------------------------------------------------

@dataclass
class ScoredCandidate:
    item: MenuItem
    total_score: float
    signal_scores: Dict[str, float]
    explanation: str


def _generate_explanation(item: MenuItem, signals: Dict[str, float], user: User, ctx: RecommenderQueryContext) -> str:
    """Generate human-readable rationales for why this food item was recommended."""
    reasons = []

    # 1. Personalization reason
    if item.item_id in user.past_item_ids:
        reasons.append("Ordered before ⭐")

    # 2. Rating & Popularity reason
    if item.rating >= 4.7:
        reasons.append(f"Top rated ({item.rating}★)")
    elif item.rating >= 4.3:
        reasons.append(f"{item.rating}★ rating")

    # 3. Cuisine / Flavor reason
    if ctx.flavor and ctx.flavor in " ".join(item.tags).lower():
        reasons.append(f"Popular {ctx.flavor} choice")
    elif ctx.cuisine and ctx.cuisine in " ".join(item.tags).lower():
        reasons.append(f"Authentic {ctx.cuisine.replace('-', ' ').title()}")

    # 4. Budget / Delivery reason
    if ctx.wants_cheap or (ctx.max_price and item.price <= ctx.max_price):
        reasons.append(f"Budget friendly (₹{int(item.price)})")
    elif item.eta_minutes <= 25:
        reasons.append(f"Fast delivery ({item.eta_minutes} min)")

    if not reasons:
        reasons.append(f"{item.restaurant} • {item.rating}★")

    return " • ".join(reasons[:2])


def score_candidate(
    item: MenuItem,
    user: User,
    ctx: RecommenderQueryContext,
    weights: RecommendationWeights = DEFAULT_WEIGHTS
) -> ScoredCandidate:
    """Calculate multi-signal score and transparent explanation for a candidate."""
    s_query = _compute_query_relevance(item, ctx)
    s_cuisine = _compute_cuisine_score(item, ctx)
    s_dietary = _compute_dietary_score(item, ctx, user)
    s_rating = _compute_rating_score(item)
    s_popularity = _compute_popularity_score(item)
    s_personalization = _compute_personalization_score(item, user, ctx)
    s_price = _compute_price_score(item, ctx)
    s_location = _compute_location_score(item, ctx)

    total = (
        weights.query_relevance * s_query
        + weights.cuisine_match * s_cuisine
        + weights.dietary_match * s_dietary
        + weights.rating_signal * s_rating
        + weights.popularity_signal * s_popularity
        + weights.personalization_signal * s_personalization
        + weights.price_fit * s_price
        + weights.location_fit * s_location
    )

    signals = {
        "query_relevance": s_query,
        "cuisine_match": s_cuisine,
        "dietary_match": s_dietary,
        "rating_signal": s_rating,
        "popularity_signal": s_popularity,
        "personalization_signal": s_personalization,
        "price_fit": s_price,
        "location_fit": s_location,
    }

    explanation = _generate_explanation(item, signals, user, ctx)

    return ScoredCandidate(
        item=item,
        total_score=round(total, 4),
        signal_scores=signals,
        explanation=explanation
    )


# ---------------------------------------------------------------------------
# Diversity & Maximal Marginal Relevance (MMR)
# ---------------------------------------------------------------------------

def _apply_diversity(scored: List[ScoredCandidate], top_n: int, diversity_penalty: float = 0.15) -> List[MenuItem]:
    """Select top_n candidates while penalizing redundant restaurants and dish types."""
    if len(scored) <= top_n:
        res = []
        for s in scored:
            item = s.item
            item.score = s.total_score
            item.explanation = s.explanation
            res.append(item)
        return res

    selected: List[ScoredCandidate] = []
    candidates = list(scored)

    while len(selected) < top_n and candidates:
        if not selected:
            # Pick best scoring item first
            best = candidates.pop(0)
            selected.append(best)
            continue

        # Score remaining candidates with diversity penalty
        best_candidate = None
        best_mmr_score = -float("inf")
        best_idx = -1

        for idx, cand in enumerate(candidates):
            penalty = 0.0
            for sel in selected:
                # Same restaurant penalty
                if cand.item.restaurant == sel.item.restaurant:
                    penalty += diversity_penalty
                # Same dish name penalty
                if cand.item.name.lower() == sel.item.name.lower():
                    penalty += (diversity_penalty * 0.8)

            mmr_score = cand.total_score - penalty
            if mmr_score > best_mmr_score:
                best_mmr_score = mmr_score
                best_candidate = cand
                best_idx = idx

        if best_candidate and best_idx >= 0:
            selected.append(candidates.pop(best_idx))
        else:
            selected.append(candidates.pop(0))

    result = []
    for s in selected:
        item = s.item
        item.score = s.total_score
        item.explanation = s.explanation
        result.append(item)
    return result


# ---------------------------------------------------------------------------
# Backward-Compatible Helper Functions (Preserving Existing Unit Tests)
# ---------------------------------------------------------------------------

def _matches_query(item: MenuItem, query: str) -> bool:
    """Exact/whole-word matching logic preserved for backward-compatibility."""
    q = query.strip().lower()
    if not q:
        return False
    haystack_words = set(f"{item.name} {' '.join(item.tags)}".lower().split())
    return all(word in haystack_words for word in q.split())


def _history_boost(item: MenuItem, user: User) -> float:
    """Legacy 0.5 rating bump function preserved for backward-compatibility."""
    if item.item_id in user.past_item_ids:
        return 0.5
    return 0.0


# ---------------------------------------------------------------------------
# Main Recommendation Engine Entrypoint
# ---------------------------------------------------------------------------

def recommend(
    query: str,
    user: User,
    menu: list[MenuItem],
    top_n: int = 3,
    weights: RecommendationWeights = DEFAULT_WEIGHTS,
    min_rating: float = MIN_RATING,
) -> list[MenuItem]:
    """Hybrid food recommendation engine with multi-signal ranking, diversity,

    and explainable scoring.
    """
    ctx = parse_recommender_query(query)
    if not ctx.cleaned_query or not ctx.tokens:
        return []

    # 1. Hard Filtering Stage
    filtered_candidates: List[MenuItem] = []
    for item in menu:
        item_tags = [t.lower() for t in item.tags]
        item_name = item.name.lower()
        is_non_veg = "non-veg" in item_tags or any(m in item_name for m in ["chicken", "mutton", "fish", "meat", "egg"])

        # Hard Dietary Exclusion: "veg" query MUST never return non-veg
        if ctx.dietary == "veg" and is_non_veg:
            continue

        # Hard Budget Cap
        if ctx.max_price and item.price > ctx.max_price:
            continue

        # Hard History Request ("something I ordered before")
        if ctx.wants_history and item.item_id not in user.past_item_ids:
            continue

        # Initial Rating Filter
        if item.rating < min_rating:
            continue

        filtered_candidates.append(item)

    # 2. Candidate Selection:
    # First check for items that match all non-stopword query tokens (exact/full match)
    exact_matches: List[MenuItem] = [
        item for item in filtered_candidates if _matches_query(item, query)
    ]

    if exact_matches and not (ctx.wants_cheap or ctx.wants_history or ctx.flavor or ctx.max_price or ctx.wants_top_rated or ctx.wants_near_me):
        relevant = exact_matches
    else:
        # Perform semantic / attribute / taxonomy matching
        relevant = []
        for item in filtered_candidates:
            q_rel = _compute_query_relevance(item, ctx)
            c_rel = _compute_cuisine_score(item, ctx)
            item_tags_str = " ".join(item.tags).lower()

            # If user explicitly specified a cuisine, enforce cuisine match
            if ctx.cuisine and c_rel < 0.8:
                continue

            # If user explicitly requested history, enforce past item
            if ctx.wants_history and item.item_id not in user.past_item_ids:
                continue

            # Check matching conditions
            is_match = False
            if exact_matches and item in exact_matches:
                is_match = True
            elif ctx.cuisine and c_rel >= 0.8:
                is_match = True
            elif ctx.flavor and ctx.flavor in item_tags_str:
                is_match = True
            elif ctx.wants_history and item.item_id in user.past_item_ids:
                is_match = True
            elif q_rel >= 0.70:
                is_match = True
            elif ctx.max_price is not None and not ctx.cuisine and q_rel >= 0.0:
                is_match = True
            elif ctx.group_size > 1 and not ctx.cuisine:
                is_match = True
            elif ctx.wants_top_rated and not ctx.cuisine and item.rating >= 4.5:
                is_match = True
            elif ctx.wants_cheap and not ctx.cuisine:
                is_match = True

            if is_match:
                relevant.append(item)

    # 3. Fallback: If still 0 matches, attempt relaxed fuzzy search
    if not relevant and not ctx.wants_history:
        for item in menu:
            item_tags_str = " ".join(item.tags).lower()
            is_non_veg = "non-veg" in item_tags_str or "chicken" in item.name.lower()
            if ctx.dietary == "veg" and is_non_veg:
                continue
            if ctx.cuisine and _compute_cuisine_score(item, ctx) < 0.8:
                continue
            if _compute_query_relevance(item, ctx) >= 0.50 or not ctx.semantic_tokens:
                relevant.append(item)

    if not relevant:
        return []

    # 4. Multi-Signal Scoring
    scored_list: List[ScoredCandidate] = []
    for item in relevant:
        # Compute hybrid score
        candidate = score_candidate(item, user, ctx, weights)
        
        # Check legacy history boost compatibility
        if item.item_id in user.past_item_ids and not ctx.wants_history:
            # Add boost proportional to 0.5 rating bump
            candidate.total_score += 0.10
        
        scored_list.append(candidate)

    # Sort candidates by total score descending
    scored_list.sort(key=lambda c: c.total_score, reverse=True)

    # 5. Diversity & Maximal Marginal Relevance (MMR)
    # If the user queried an exact single dish (e.g. "Masala Dosa"), preserve exact rank without excessive penalty
    is_exact_query = len(relevant) <= top_n or any(_matches_query(c.item, query) for c in scored_list)
    penalty = 0.05 if is_exact_query else 0.15

    results = _apply_diversity(scored_list, top_n=top_n, diversity_penalty=penalty)
    return results[:top_n]
