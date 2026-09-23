"""The actual assignment logic: match query -> filter by rating -> rank -> top 3.

Kept as pure functions (no I/O, no state) so they're trivial to unit test.
"""
from models import MenuItem, User

MIN_RATING = 4.0


def _matches_query(item: MenuItem, query: str) -> bool:
    q = query.strip().lower()
    if not q:
        return False
    haystack_words = set(f"{item.name} {' '.join(item.tags)}".lower().split())
    # Whole-word match, not substring — otherwise "veg" wrongly matches inside "non-veg".
    return all(word in haystack_words for word in q.split())


def _history_boost(item: MenuItem, user: User) -> float:
    """Small ranking bump if the user has ordered this exact item before,
    and a smaller bump if they've ordered from the same restaurant before."""
    if item.item_id in user.past_item_ids:
        return 0.5
    return 0.0


def recommend(query: str, user: User, menu: list[MenuItem], top_n: int = 3) -> list[MenuItem]:
    candidates = [item for item in menu if _matches_query(item, query)]
    candidates = [item for item in candidates if item.rating >= MIN_RATING]

    def score(item: MenuItem) -> float:
        return item.rating + _history_boost(item, user)

    candidates.sort(key=score, reverse=True)
    return candidates[:top_n]
