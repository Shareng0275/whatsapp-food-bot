"""Enterprise database & repository tests for production features."""
import unittest
from datetime import datetime, timezone
from database import Base, get_db
from db_models import (
    UserRow,
    MenuItemRow,
    PromoCodeRow,
    OrderRow,
    OrderItemRow,
    UserAddressRow,
    ConversationRow,
    MessageRow,
    PromoRedemptionRow,
    IdempotencyRecordRow,
)
from repository import Repository, RepositoryError


class TestEnterpriseRepository(unittest.TestCase):
    def setUp(self):
        with get_db() as db:
            Base.metadata.drop_all(db.bind)
            Base.metadata.create_all(db.bind)

            # Seed test user & menu
            db.add(UserRow(phone="+919876543210", name="Priya", default_address="Old Address"))
            db.add(MenuItemRow(
                item_id="i1", name="Veg Biryani", restaurant="Paradise",
                rating=4.6, price=220.0, eta_minutes=35, tags="biryani,veg"
            ))
            db.add(PromoCodeRow(
                code="WELCOME50", description="50 off", discount_type="flat",
                discount_value=50.0, min_order_value=100.0, active=True
            ))
            db.commit()

    def test_user_address_book_and_default_flag(self):
        phone = "+919876543210"
        with get_db() as db:
            repo = Repository(db)

            # Add home address (default)
            addr1_id = repo.add_user_address(
                phone, full_address="Flat 101, Sunshine Heights, Bangalore",
                label="Home", is_default=True
            )
            # Add office address
            addr2_id = repo.add_user_address(
                phone, full_address="Tech Park, Block B, Bangalore",
                label="Office", is_default=False
            )

            addresses = repo.list_user_addresses(phone)
            self.assertEqual(len(addresses), 2)
            # Default address should be first
            self.assertEqual(addresses[0]["id"], addr1_id)
            self.assertTrue(addresses[0]["is_default"])

            # Verify UserRow.default_address was automatically synced
            user = repo.get_user(phone)
            self.assertEqual(user.default_address, "Flat 101, Sunshine Heights, Bangalore")

    def test_promo_redemptions_tracking(self):
        phone = "+919876543210"
        with get_db() as db:
            repo = Repository(db)

            # Initial count is 0
            self.assertEqual(repo.get_promo_redemption_count("WELCOME50", phone), 0)

            # Record redemption
            repo.record_promo_redemption("WELCOME50", phone, order_id="ord_1", discount=50.0)

            # Count is now 1
            self.assertEqual(repo.get_promo_redemption_count("WELCOME50", phone), 1)

    def test_durable_conversation_state_and_optimistic_locking(self):
        phone = "+919876543210"
        with get_db() as db:
            repo = Repository(db)

            # Initial state save (creates version 1)
            v1 = repo.save_conversation_state(phone, state="SELECTING", session_data_json='{"item": "i1"}', expected_version=1)
            self.assertEqual(v1, 1)

            # Successful update with matching version (advances to version 2)
            v2 = repo.save_conversation_state(phone, state="ADDRESS", session_data_json='{"item": "i1", "step": 2}', expected_version=1)
            self.assertEqual(v2, 2)

            # Concurrent conflict: Attempting update with outdated expected version 1 raises RepositoryError
            with self.assertRaises(RepositoryError):
                repo.save_conversation_state(phone, state="PROMO", session_data_json='{}', expected_version=1)

            # Valid update with version 2 succeeds
            v3 = repo.save_conversation_state(phone, state="PROMO", session_data_json='{}', expected_version=2)
            self.assertEqual(v3, 3)

    def test_message_audit_trail_logging(self):
        phone = "+919876543210"
        with get_db() as db:
            repo = Repository(db)

            msg_id = repo.log_audit_message(
                phone=phone, direction="inbound", body="I want veg biryani",
                twilio_message_sid="SM123456789", detected_intent="search_food",
                nlu_confidence=0.95
            )
            self.assertIsNotNone(msg_id)

    def test_idempotency_caching_and_retrieval(self):
        key = "twilio:SM_test_unique_key_123"
        with get_db() as db:
            repo = Repository(db)

            # Initially not present
            self.assertIsNone(repo.check_idempotency(key))

            # Save completed payload
            repo.save_idempotency(key, status="completed", response_payload="<Response><Message>Hi</Message></Response>")

            # Retrieve cached payload
            record = repo.check_idempotency(key)
            self.assertIsNotNone(record)
            self.assertEqual(record["status"], "completed")
            self.assertIn("<Response>", record["response_payload"])

    def test_transaction_rollback_on_failure(self):
        """Verify transaction atomicity and rollback on error."""
        with self.assertRaises(RuntimeError):
            with get_db() as db:
                repo = Repository(db)
                repo.create_user("+919999900001", name="RollbackUser")
                raise RuntimeError("Simulated crash mid-transaction")

        # Verify user was NOT committed
        with get_db() as db:
            repo = Repository(db)
            self.assertIsNone(repo.get_user("+919999900001"))


if __name__ == "__main__":
    unittest.main()
