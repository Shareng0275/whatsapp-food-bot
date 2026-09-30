"""Comprehensive test suite for Natural Language Understanding (NLU) Module."""
import unittest

from nlu import NLUEngine, Intent, NLUResult


class TestNLUEngine(unittest.TestCase):
    def setUp(self):
        self.nlu = NLUEngine(confidence_threshold=0.65)

    def test_greeting_intent(self):
        res = self.nlu.parse("Hi, I want to order food")
        self.assertEqual(res.intent, Intent.GREETING)
        self.assertGreaterEqual(res.confidence, 0.90)

    def test_food_query_basic(self):
        res = self.nlu.parse("show me biryani")
        self.assertEqual(res.intent, Intent.SEARCH_FOOD)
        self.assertEqual(res.entities.food_item, "biryani")
        self.assertEqual(res.entities.cuisine, "biryani")

    def test_dietary_and_budget_extraction(self):
        # "I need veg food under 500"
        res = self.nlu.parse("I need veg food under 500")
        self.assertEqual(res.intent, Intent.SEARCH_FOOD)
        self.assertEqual(res.entities.dietary, "veg")
        self.assertEqual(res.entities.max_price, 500.0)

    def test_cuisine_extraction(self):
        # "find Chinese near me"
        res = self.nlu.parse("find Chinese near me")
        self.assertEqual(res.intent, Intent.SEARCH_FOOD)
        self.assertEqual(res.entities.cuisine, "chinese")

    def test_budget_cheap_intent(self):
        # "I want something cheap"
        res = self.nlu.parse("I want something cheap")
        self.assertEqual(res.intent, Intent.SEARCH_FOOD)
        self.assertEqual(res.entities.sort_by, "price_asc")
        self.assertIsNotNone(res.entities.max_price)

    def test_rating_sorting_intent(self):
        # "give me the best rated pizza"
        res = self.nlu.parse("give me the best rated pizza")
        self.assertEqual(res.intent, Intent.SEARCH_FOOD)
        self.assertEqual(res.entities.food_item, "pizza")
        self.assertEqual(res.entities.sort_by, "rating_desc")

    def test_ordinal_selection_intent(self):
        # "I want the second one" in SELECTING state
        res = self.nlu.parse("I want the second one", current_state="SELECTING")
        self.assertEqual(res.intent, Intent.SELECT_OPTION)
        self.assertEqual(res.entities.selection_index, 2)

        # "number 3"
        res3 = self.nlu.parse("option 3", current_state="SELECTING")
        self.assertEqual(res3.intent, Intent.SELECT_OPTION)
        self.assertEqual(res3.entities.selection_index, 3)

        # plain "1"
        res1 = self.nlu.parse("1", current_state="SELECTING")
        self.assertEqual(res1.intent, Intent.SELECT_OPTION)
        self.assertEqual(res1.entities.selection_index, 1)

    def test_flavor_and_spiciness(self):
        # "I want something spicy"
        res = self.nlu.parse("I want something spicy")
        self.assertEqual(res.intent, Intent.SEARCH_FOOD)
        self.assertEqual(res.entities.flavor, "spicy")

    def test_negation_handling(self):
        # "actually show me something vegetarian, not non-veg"
        res = self.nlu.parse("actually show me something vegetarian, not non-veg")
        self.assertEqual(res.intent, Intent.SEARCH_FOOD)
        self.assertEqual(res.entities.dietary, "veg")
        self.assertIn("non-veg", res.entities.negations)

    def test_correction_query(self):
        # "no, I meant chicken biryani"
        res = self.nlu.parse("no, I meant chicken biryani")
        self.assertEqual(res.intent, Intent.SEARCH_FOOD)
        self.assertEqual(res.entities.food_item, "chicken biryani")
        self.assertEqual(res.entities.dietary, "non-veg")

    def test_cancellation_intent(self):
        # "cancel my order"
        res = self.nlu.parse("cancel my order")
        self.assertEqual(res.intent, Intent.CANCEL_ORDER)
        self.assertGreaterEqual(res.confidence, 0.90)

    def test_modification_intent(self):
        # "I want to change my address"
        res = self.nlu.parse("I want to change my address")
        self.assertEqual(res.intent, Intent.MODIFY_ORDER)

    def test_address_state_saved_address(self):
        # "same" or "use saved address" in ADDRESS state
        res = self.nlu.parse("same", current_state="ADDRESS")
        self.assertEqual(res.intent, Intent.PROVIDE_ADDRESS)
        self.assertTrue(res.entities.use_saved_address)

    def test_address_state_raw_address(self):
        res = self.nlu.parse("Flat 402, Green Glen Layout, Bangalore", current_state="ADDRESS")
        self.assertEqual(res.intent, Intent.PROVIDE_ADDRESS)
        self.assertIsNotNone(res.entities.raw_address)

    def test_promo_state_skip(self):
        res = self.nlu.parse("skip", current_state="PROMO")
        self.assertEqual(res.intent, Intent.SKIP_STEP)

    def test_promo_state_code(self):
        res = self.nlu.parse("SAVE15", current_state="PROMO")
        self.assertEqual(res.intent, Intent.APPLY_PROMO)
        self.assertEqual(res.entities.promo_code, "SAVE15")

    def test_sentiment_detection(self):
        res_pos = self.nlu.parse("This is great, thanks!")
        self.assertEqual(res_pos.sentiment, "positive")

        res_neg = self.nlu.parse("Terrible food and worst service")
        self.assertEqual(res_neg.sentiment, "negative")

        res_neu = self.nlu.parse("Veg Biryani")
        self.assertEqual(res_neu.sentiment, "neutral")

    def test_fallback_unknown(self):
        res = self.nlu.parse("")
        self.assertEqual(res.intent, Intent.UNKNOWN)
        self.assertEqual(res.confidence, 0.0)


if __name__ == "__main__":
    unittest.main()
