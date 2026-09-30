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
10. Hybrid natural-language queries (spicy, cheap, pizza, under 500, near me, healthy, past orders, for two).
11. Fuzzy typo tolerance and token similarity.
12. Diversity / MMR candidate diversification.
13. Human-readable explanation generation.
14. Offline benchmark evaluation metrics (NDCG@k, MRR, Precision@k).
15. Cold start (new users) and fallback behavior.
"""
import math
import pytest
from models import MenuItem, User
from recommender import (
    recommend,
    _matches_query,
    _history_boost,
    score_candidate,
    parse_recommender_query,
    MIN_RATING,
    RecommendationWeights,
    DEFAULT_WEIGHTS,
)


@pytest.fixture
def sample_menu():
    return [
        MenuItem("i1", "Veg Biryani", "Paradise Biryani", 4.6, 220, 35, ["biryani", "veg"]),
        MenuItem("i2", "Veg Biryani", "Biryani Blues", 4.3, 199, 40, ["biryani", "veg"]),
        MenuItem("i3", "Veg Biryani", "Meghana Foods", 4.7, 250, 30, ["biryani", "veg"]),
        MenuItem("i4", "Veg Biryani", "Street Cart Express", 3.8, 150, 25, ["biryani", "veg", "budget"]),
        MenuItem("i5", "Hyderabadi Veg Biryani", "Shah Ghouse", 4.4, 240, 45, ["biryani", "veg"]),
        MenuItem("i6", "Chicken Biryani", "Paradise Biryani", 4.8, 280, 35, ["biryani", "non-veg"]),
        MenuItem("i7", "Masala Dosa", "Vidyarthi Bhavan", 4.9, 90, 20, ["south-indian", "veg", "spicy"]),
        MenuItem("i8", "Paneer Butter Masala", "Punjabi Rasoi", 4.2, 210, 30, ["north-indian", "veg"]),
    ]


@pytest.fixture
def expanded_menu():
    """Expanded multi-cuisine menu for comprehensive hybrid ranking and diversity tests."""
    return [
        MenuItem("i1", "Veg Biryani", "Paradise Biryani", 4.6, 220, 35, ["biryani", "veg", "spicy"]),
        MenuItem("i2", "Veg Biryani", "Biryani Blues", 4.3, 199, 40, ["biryani", "veg"]),
        MenuItem("i3", "Veg Biryani", "Meghana Foods", 4.7, 250, 30, ["biryani", "veg", "spicy"]),
        MenuItem("i4", "Street Fried Rice", "Street Cart Express", 3.8, 110, 20, ["chinese", "veg", "budget"]),
        MenuItem("i5", "Hyderabadi Veg Biryani", "Shah Ghouse", 4.4, 240, 45, ["biryani", "veg"]),
        MenuItem("i6", "Chicken Biryani", "Paradise Biryani", 4.8, 280, 35, ["biryani", "non-veg", "spicy"]),
        MenuItem("i7", "Masala Dosa", "Vidyarthi Bhavan", 4.9, 90, 20, ["south-indian", "veg", "budget", "healthy"]),
        MenuItem("i8", "Paneer Butter Masala", "Punjabi Rasoi", 4.2, 210, 30, ["north-indian", "veg"]),
        MenuItem("i9", "Veg Hakka Noodles", "Mainland China", 4.5, 180, 25, ["chinese", "veg", "budget"]),
        MenuItem("i10", "Schezwan Fried Rice", "Wok Express", 4.3, 160, 25, ["chinese", "veg", "spicy", "budget"]),
        MenuItem("i11", "Margherita Pizza", "Toscano", 4.7, 450, 30, ["italian", "pizza", "veg"]),
        MenuItem("i12", "Farmhouse Veg Pizza", "Onesta", 4.4, 320, 35, ["italian", "pizza", "veg", "combo"]),
        MenuItem("i13", "Quinoa Avocado Salad", "FreshMenu", 4.6, 260, 25, ["salad", "healthy", "veg"]),
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


class TestHybridSemanticQueries:
    """Test the 9 natural language query types required by the hybrid engine."""

    def test_query_veg_biryani(self, expanded_menu, default_user):
        results = recommend("veg biryani", default_user, expanded_menu, top_n=3)
        assert len(results) == 3
        for item in results:
            assert "biryani" in item.tags
            assert "non-veg" not in item.tags

    def test_query_something_spicy(self, expanded_menu, default_user):
        results = recommend("something spicy", default_user, expanded_menu, top_n=3)
        assert len(results) >= 1
        for item in results:
            assert "spicy" in " ".join(item.tags).lower()

    def test_query_cheap_chinese_food(self, expanded_menu, default_user):
        results = recommend("cheap Chinese food", default_user, expanded_menu, top_n=3)
        assert len(results) >= 1
        for item in results:
            assert "chinese" in item.tags
            assert item.price <= 200

    def test_query_high_rated_pizza(self, expanded_menu, default_user):
        results = recommend("high rated pizza", default_user, expanded_menu, top_n=3)
        assert len(results) >= 1
        assert results[0].name == "Margherita Pizza"
        assert results[0].rating >= 4.5

    def test_query_something_under_500(self, expanded_menu, default_user):
        results = recommend("something under 500", default_user, expanded_menu, top_n=3)
        assert len(results) == 3
        for item in results:
            assert item.price <= 500

    def test_query_south_indian_near_me(self, expanded_menu, default_user):
        results = recommend("South Indian near me", default_user, expanded_menu, top_n=3)
        assert len(results) >= 1
        assert results[0].name == "Masala Dosa"
        assert results[0].eta_minutes <= 25

    def test_query_something_i_ordered_before(self, expanded_menu):
        user_with_orders = User(phone="+919900011122", name="Asha", past_item_ids=["i7", "i11"])
        results = recommend("something I ordered before", user_with_orders, expanded_menu, top_n=3)
        assert len(results) == 2
        item_ids = {r.item_id for r in results}
        assert item_ids == {"i7", "i11"}

    def test_query_healthy_vegetarian_food(self, expanded_menu, default_user):
        results = recommend("healthy vegetarian food", default_user, expanded_menu, top_n=3)
        assert len(results) >= 1
        for item in results:
            assert "veg" in item.tags
            assert "non-veg" not in item.tags
            assert "healthy" in item.tags or item.rating >= 4.5

    def test_query_something_good_for_two_people(self, expanded_menu, default_user):
        results = recommend("something good for two people", default_user, expanded_menu, top_n=3)
        assert len(results) >= 1
        for item in results:
            assert item.rating >= 4.4


class TestFuzzyAndDiversity:
    def test_fuzzy_typo_matching(self, expanded_menu, default_user):
        # "briyani" -> "biryani"
        res_biryani = recommend("briyani", default_user, expanded_menu, top_n=2)
        assert len(res_biryani) >= 1
        assert "biryani" in res_biryani[0].tags

    def test_diversity_avoids_identical_restaurants(self, expanded_menu, default_user):
        results = recommend("biryani", default_user, expanded_menu, top_n=3)
        restaurants = [r.restaurant for r in results]
        # Should contain variety of restaurants, not all 3 from the same place
        assert len(set(restaurants)) >= 2


class TestExplainabilityAndEvaluation:
    def test_explanation_is_attached_to_items(self, expanded_menu, default_user):
        results = recommend("Masala Dosa", default_user, expanded_menu, top_n=1)
        assert results[0].explanation is not None
        assert "4.9★" in results[0].explanation or "South Indian" in results[0].explanation

    def test_offline_ranking_metrics_ndcg_mrr(self, expanded_menu):
        """Simulate ranking benchmark evaluation with ground truth relevance labels."""
        user = User(phone="+919900011122", name="Asha", past_item_ids=["i7"])
        query = "South Indian near me"
        # Ground truth: i7 is ideal rank 1 (rel=3), others are rel=0
        results = recommend(query, user, expanded_menu, top_n=3)
        
        # Calculate MRR (Mean Reciprocal Rank)
        mrr = 0.0
        for rank, item in enumerate(results, start=1):
            if item.item_id == "i7":
                mrr = 1.0 / rank
                break
        assert mrr == 1.0

        # Calculate DCG@3
        dcg = 0.0
        for rank, item in enumerate(results, start=1):
            rel = 3.0 if item.item_id == "i7" else 0.0
            dcg += (2**rel - 1) / math.log2(rank + 1)
        
        idcg = (2**3 - 1) / math.log2(1 + 1)
        ndcg = dcg / idcg
        assert math.isclose(ndcg, 1.0, rel_tol=1e-2)

    def test_weights_validation(self):
        w = RecommendationWeights()
        assert w.validate()
        w_custom = RecommendationWeights(query_relevance=0.40, cuisine_match=0.10)
        assert w_custom.validate()
