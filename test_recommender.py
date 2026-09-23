"""Automated tests for recommender.py.

Requirements tested:
1. Exact dish query.
2. Case-insensitive query.
3. Tag matching.
4. Rating < 4.0 excluded.
5. Maximum top_n respected.
6. Previous exact item receives history boost.
7. Empty result.
8. Query with multiple words.
9. Ensure "veg" does not accidentally match "non-veg" because of substring behavior.
"""
import pytest
from models import MenuItem, User
from recommender import recommend, _matches_query, _history_boost, MIN_RATING


@pytest.fixture
def sample_menu():
    return [
        MenuItem("i1", "Veg Biryani", "Paradise Biryani", 4.6, 220, 35, ["biryani", "veg"]),
        MenuItem("i2", "Veg Biryani", "Biryani Blues", 4.3, 199, 40, ["biryani", "veg"]),
        MenuItem("i3", "Veg Biryani", "Meghana Foods", 4.7, 250, 30, ["biryani", "veg"]),
        MenuItem("i4", "Veg Biryani", "Street Cart Express", 3.8, 150, 25, ["biryani", "veg", "budget"]),
        MenuItem("i5", "Hyderabadi Veg Biryani", "Shah Ghouse", 4.4, 240, 45, ["biryani", "veg"]),
        MenuItem("i6", "Chicken Biryani", "Paradise Biryani", 4.8, 280, 35, ["biryani", "non-veg"]),
        MenuItem("i7", "Masala Dosa", "Vidyarthi Bhavan", 4.9, 90, 20, ["south-indian", "veg"]),
        MenuItem("i8", "Paneer Butter Masala", "Punjabi Rasoi", 4.2, 210, 30, ["north-indian", "veg"]),
    ]


@pytest.fixture
def default_user():
    return User(phone="+919900011122", name="Asha", past_item_ids=[])


@pytest.fixture
def user_with_history():
    return User(phone="+919900011122", name="Asha", past_item_ids=["i2"])


class TestRecommender:
    def test_1_exact_dish_query(self, sample_menu, default_user):
        """1. Exact dish query matches items with that exact name."""
        results = recommend("Masala Dosa", default_user, sample_menu)
        assert len(results) == 1
        assert results[0].item_id == "i7"
        assert results[0].name == "Masala Dosa"

    def test_2_case_insensitive_query(self, sample_menu, default_user):
        """2. Case-insensitive query matches regardless of uppercase/lowercase."""
        results_lower = recommend("masala dosa", default_user, sample_menu)
        results_upper = recommend("MASALA DOSA", default_user, sample_menu)
        results_mixed = recommend("MaSaLa DoSa", default_user, sample_menu)
        assert results_lower == results_upper == results_mixed
        assert len(results_lower) == 1
        assert results_lower[0].name == "Masala Dosa"

    def test_3_tag_matching(self, sample_menu, default_user):
        """3. Tag matching finds items by their tags (e.g. 'south-indian', 'north-indian')."""
        results_south = recommend("south-indian", default_user, sample_menu)
        assert len(results_south) == 1
        assert results_south[0].item_id == "i7"

        results_north = recommend("north-indian", default_user, sample_menu)
        assert len(results_north) == 1
        assert results_north[0].item_id == "i8"

    def test_4_rating_below_4_excluded(self, sample_menu, default_user):
        """4. Rating < 4.0 excluded (e.g. Street Cart Express with 3.8 is filtered out)."""
        assert MIN_RATING == 4.0
        results = recommend("Veg Biryani", default_user, sample_menu, top_n=10)
        item_ids = [r.item_id for r in results]
        assert "i4" not in item_ids  # i4 has rating 3.8
        for r in results:
            assert r.rating >= 4.0

    def test_5_maximum_top_n_respected(self, sample_menu, default_user):
        """5. Maximum top_n respected."""
        results_1 = recommend("Veg Biryani", default_user, sample_menu, top_n=1)
        assert len(results_1) == 1

        results_2 = recommend("Veg Biryani", default_user, sample_menu, top_n=2)
        assert len(results_2) == 2

        results_default = recommend("Veg Biryani", default_user, sample_menu)
        assert len(results_default) == 3

    def test_6_previous_exact_item_receives_history_boost(self, sample_menu, user_with_history):
        """6. Previous exact item receives history boost (0.5 score boost)."""
        # i2 has rating 4.3 + 0.5 boost = 4.8, beating i3 (4.7) and i1 (4.6)
        results = recommend("Veg Biryani", user_with_history, sample_menu, top_n=3)
        assert results[0].item_id == "i2"
        assert _history_boost(sample_menu[1], user_with_history) == 0.5
        assert _history_boost(sample_menu[0], user_with_history) == 0.0

    def test_7_empty_result(self, sample_menu, default_user):
        """7. Empty result when no items match or query is empty."""
        assert recommend("Pizza", default_user, sample_menu) == []
        assert recommend("Sushi", default_user, sample_menu) == []
        assert recommend("", default_user, sample_menu) == []
        assert recommend("   ", default_user, sample_menu) == []

    def test_8_query_with_multiple_words(self, sample_menu, default_user):
        """8. Query with multiple words (e.g. 'Hyderabadi Veg Biryani')."""
        results = recommend("Hyderabadi Veg Biryani", default_user, sample_menu)
        assert len(results) == 1
        assert results[0].item_id == "i5"
        assert results[0].name == "Hyderabadi Veg Biryani"

    def test_9_veg_does_not_match_non_veg(self, sample_menu, default_user):
        """9. Ensure 'veg' does not accidentally match 'non-veg' because of substring behavior."""
        chicken_biryani = sample_menu[5]  # Chicken Biryani, tags: ["biryani", "non-veg"]
        assert "non-veg" in chicken_biryani.tags
        assert not _matches_query(chicken_biryani, "veg")

        # When searching "veg", Chicken Biryani must not be in results
        results = recommend("veg", default_user, sample_menu, top_n=10)
        item_ids = [r.item_id for r in results]
        assert "i6" not in item_ids
        for r in results:
            assert "non-veg" not in r.tags
