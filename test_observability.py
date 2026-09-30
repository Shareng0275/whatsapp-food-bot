"""Automated tests for the Observability Module.

Tests:
1. MetricsCollector counters, gauges, histograms, and snapshots
2. Prometheus text exposition format
3. Event emission with structured logging and counter side-effects
4. Timer context manager with histogram integration
5. /health endpoint includes uptime_seconds
6. /readiness endpoint with deep dependency checks
7. /metrics endpoint (Prometheus text and JSON)
8. Webhook flow increments correct counters
9. Privacy: no PII leaks in metrics output
10. Thread safety: concurrent increments produce correct totals
"""
import os
import json
import logging
import time
import threading
import unittest
from io import StringIO

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["TWILIO_AUTH_TOKEN"] = "test_twilio_secret_token_12345"
os.environ["PAYMENT_WEBHOOK_SECRET"] = "test_payment_secret_key_67890"
os.environ["TWILIO_VALIDATE_SIGNATURE"] = "true"

from twilio.request_validator import RequestValidator
from database import Base, get_db
from db_models import UserRow, MenuItemRow, PromoCodeRow, OrderRow  # noqa: F401
from session_store import InMemorySessionStore
from bot import create_app
from observability import (
    MetricsCollector,
    Histogram,
    Timer,
    get_metrics_collector,
    reset_metrics_collector,
    emit_event,
    EVENT_WEBHOOK_RECEIVED,
    EVENT_STATE_TRANSITION,
    EVENT_ORDER_CREATED,
    EVENT_RATE_LIMIT,
)
from structured_logger import StructuredJsonFormatter


class TestHistogram(unittest.TestCase):
    """Unit tests for the Histogram helper."""

    def test_observe_within_buckets(self):
        h = Histogram("test_h", buckets=(10, 50, 100))
        h.observe(5)
        h.observe(25)
        h.observe(75)
        h.observe(200)
        snap = h.snapshot()
        self.assertEqual(snap["count"], 4)
        self.assertEqual(snap["buckets"]["le_10"], 1)
        self.assertEqual(snap["buckets"]["le_50"], 1)
        self.assertEqual(snap["buckets"]["le_100"], 1)
        self.assertEqual(snap["buckets"]["le_inf"], 1)

    def test_empty_histogram(self):
        h = Histogram("empty_h")
        snap = h.snapshot()
        self.assertEqual(snap["count"], 0)
        self.assertEqual(snap["sum"], 0.0)
        self.assertEqual(snap["avg"], 0.0)

    def test_average_calculation(self):
        h = Histogram("avg_h", buckets=(100,))
        h.observe(10)
        h.observe(30)
        snap = h.snapshot()
        self.assertAlmostEqual(snap["avg"], 20.0, places=1)

    def test_reset(self):
        h = Histogram("reset_h", buckets=(100,))
        h.observe(50)
        h.reset()
        snap = h.snapshot()
        self.assertEqual(snap["count"], 0)


class TestMetricsCollector(unittest.TestCase):
    """Unit tests for the MetricsCollector."""

    def setUp(self):
        self.mc = MetricsCollector()

    def test_increment_counter(self):
        self.mc.increment("test_count")
        self.mc.increment("test_count", 5)
        self.assertEqual(self.mc.get_counter("test_count"), 6)

    def test_missing_counter_returns_zero(self):
        self.assertEqual(self.mc.get_counter("nonexistent"), 0)

    def test_gauge_set_inc_dec(self):
        self.mc.set_gauge("active", 10.0)
        self.assertEqual(self.mc.get_gauge("active"), 10.0)
        self.mc.inc_gauge("active", 5.0)
        self.assertEqual(self.mc.get_gauge("active"), 15.0)
        self.mc.dec_gauge("active", 3.0)
        self.assertEqual(self.mc.get_gauge("active"), 12.0)

    def test_observe_histogram(self):
        self.mc.observe("webhook_latency_ms", 42.5)
        snap = self.mc.get_histogram("webhook_latency_ms")
        self.assertEqual(snap["count"], 1)
        self.assertAlmostEqual(snap["sum"], 42.5, places=1)

    def test_snapshot_structure(self):
        self.mc.increment("events.test")
        self.mc.set_gauge("in_flight", 2.0)
        snap = self.mc.snapshot()
        self.assertIn("timestamp", snap)
        self.assertIn("uptime_seconds", snap)
        self.assertIn("counters", snap)
        self.assertIn("gauges", snap)
        self.assertIn("histograms", snap)
        self.assertEqual(snap["counters"]["events.test"], 1)
        self.assertEqual(snap["gauges"]["in_flight"], 2.0)

    def test_prometheus_text_format(self):
        self.mc.increment("webhooks_total", 10)
        self.mc.set_gauge("active_users", 3.0)
        text = self.mc.prometheus_text()
        self.assertIn("whatsapp_bot_webhooks_total 10", text)
        self.assertIn("whatsapp_bot_active_users 3.0", text)
        self.assertIn("whatsapp_bot_uptime_seconds", text)
        # Histogram lines
        self.assertIn("whatsapp_bot_webhook_latency_ms_bucket", text)

    def test_thread_safety(self):
        """Concurrent increments should produce the exact total."""
        threads = []
        iterations = 1000
        thread_count = 10

        def inc():
            for _ in range(iterations):
                self.mc.increment("concurrent_counter")

        for _ in range(thread_count):
            t = threading.Thread(target=inc)
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        expected = thread_count * iterations
        self.assertEqual(self.mc.get_counter("concurrent_counter"), expected)

    def test_reset(self):
        self.mc.increment("resettable", 42)
        self.mc.set_gauge("resettable_g", 5.0)
        self.mc.reset()
        self.assertEqual(self.mc.get_counter("resettable"), 0)
        self.assertEqual(self.mc.get_gauge("resettable_g"), 0.0)


class TestSingleton(unittest.TestCase):
    def setUp(self):
        reset_metrics_collector()

    def test_singleton_returns_same_instance(self):
        m1 = get_metrics_collector()
        m2 = get_metrics_collector()
        self.assertIs(m1, m2)

    def test_reset_creates_new_instance(self):
        m1 = get_metrics_collector()
        reset_metrics_collector()
        m2 = get_metrics_collector()
        self.assertIsNot(m1, m2)


class TestEmitEvent(unittest.TestCase):
    """Tests for the emit_event function."""

    def setUp(self):
        reset_metrics_collector()

    def test_emit_increments_counter(self):
        emit_event(EVENT_WEBHOOK_RECEIVED)
        mc = get_metrics_collector()
        self.assertEqual(mc.get_counter("events.webhook_received"), 1)

    def test_emit_logs_structured_json(self):
        stream = StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(StructuredJsonFormatter())

        test_logger = logging.getLogger("whatsapp_bot.observability")
        test_logger.addHandler(handler)
        test_logger.setLevel(logging.DEBUG)

        try:
            emit_event(EVENT_ORDER_CREATED, safe_user_id="usr_test", duration_ms=55.3,
                       metadata={"order_total": 250.0})
            log_lines = [json.loads(line) for line in stream.getvalue().strip().split("\n") if line.strip()]
            self.assertTrue(any(r.get("event_type") == "order_created" for r in log_lines))
            event_log = next(r for r in log_lines if r.get("event_type") == "order_created")
            self.assertEqual(event_log["safe_user_id"], "usr_test")
            self.assertAlmostEqual(event_log["duration_ms"], 55.3, places=1)
        finally:
            test_logger.removeHandler(handler)


class TestTimerContextManager(unittest.TestCase):
    """Tests for the Timer context manager."""

    def setUp(self):
        reset_metrics_collector()

    def test_timer_measures_elapsed(self):
        with Timer() as t:
            time.sleep(0.01)
        self.assertGreater(t.elapsed_ms, 5)

    def test_timer_observes_histogram(self):
        with Timer("test_timer_hist"):
            time.sleep(0.01)
        mc = get_metrics_collector()
        snap = mc.get_histogram("test_timer_hist")
        self.assertEqual(snap["count"], 1)
        self.assertGreater(snap["sum"], 5)


class TestObservabilityEndpoints(unittest.TestCase):
    """Integration tests for /health, /readiness, and /metrics endpoints."""

    def setUp(self):
        reset_metrics_collector()
        self.twilio_token = "test_twilio_secret_token_12345"
        os.environ["TWILIO_AUTH_TOKEN"] = self.twilio_token
        os.environ["PAYMENT_WEBHOOK_SECRET"] = "test_payment_secret_key_67890"
        os.environ["TWILIO_VALIDATE_SIGNATURE"] = "true"

        with get_db() as db:
            Base.metadata.drop_all(db.bind)
            Base.metadata.create_all(db.bind)

            db.add(MenuItemRow(
                item_id="i1", name="Veg Biryani", restaurant="Paradise Biryani",
                rating=4.6, price=220.0, eta_minutes=35, tags="biryani,veg",
            ))
            db.add(UserRow(
                phone="+919900011122", name="Asha",
                default_address="204, Palm Residency, Indiranagar",
                past_item_ids="",
            ))
            db.commit()

        self.session_store = InMemorySessionStore()
        self.app = create_app(session_store=self.session_store)
        self.client = self.app.test_client()
        self.validator = RequestValidator(self.twilio_token)

    def _sign_twilio(self, url, params):
        return self.validator.compute_signature(url, params)

    def test_health_includes_uptime(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("uptime_seconds", data)
        self.assertGreaterEqual(data["uptime_seconds"], 0)

    def test_readiness_returns_checks(self):
        resp = self.client.get("/readiness")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ready"])
        self.assertEqual(data["checks"]["database"], "ready")
        self.assertEqual(data["checks"]["session_store"], "ready")
        self.assertIn("twilio", data["checks"])
        self.assertIn("payment_gateway", data["checks"])

    def test_metrics_prometheus_format(self):
        resp = self.client.get("/metrics")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/plain", resp.content_type)
        text = resp.get_data(as_text=True)
        self.assertIn("whatsapp_bot_uptime_seconds", text)

    def test_metrics_json_format(self):
        resp = self.client.get("/metrics", headers={"Accept": "application/json"})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("counters", data)
        self.assertIn("histograms", data)
        self.assertIn("uptime_seconds", data)

    def test_webhook_increments_counters(self):
        """A successful webhook call should increment webhooks_total and messages_processed."""
        url = "http://localhost/whatsapp"
        data = {"From": "whatsapp:+919900011122", "Body": "hi"}
        headers = {"X-Twilio-Signature": self._sign_twilio(url, data)}
        resp = self.client.post("/whatsapp", data=data, headers=headers)
        self.assertEqual(resp.status_code, 200)

        mc = get_metrics_collector()
        self.assertGreaterEqual(mc.get_counter("webhooks_total"), 1)
        self.assertGreaterEqual(mc.get_counter("messages_processed"), 1)
        self.assertGreaterEqual(mc.get_counter("events.webhook_received"), 1)
        self.assertGreaterEqual(mc.get_counter("events.state_transition"), 1)

    def test_webhook_tracks_latency_histogram(self):
        url = "http://localhost/whatsapp"
        data = {"From": "whatsapp:+919900011122", "Body": "hi"}
        headers = {"X-Twilio-Signature": self._sign_twilio(url, data)}
        self.client.post("/whatsapp", data=data, headers=headers)

        mc = get_metrics_collector()
        snap = mc.get_histogram("webhook_latency_ms")
        self.assertEqual(snap["count"], 1)
        self.assertGreater(snap["sum"], 0)

    def test_conversation_started_counter(self):
        """Sending 'hi' should increment conversations_started counter."""
        url = "http://localhost/whatsapp"
        data = {"From": "whatsapp:+919900011122", "Body": "hi"}
        headers = {"X-Twilio-Signature": self._sign_twilio(url, data)}
        self.client.post("/whatsapp", data=data, headers=headers)

        mc = get_metrics_collector()
        self.assertGreaterEqual(mc.get_counter("conversations_started"), 1)

    def test_no_pii_in_metrics(self):
        """Phone numbers should never appear in /metrics output."""
        url = "http://localhost/whatsapp"
        data = {"From": "whatsapp:+919900011122", "Body": "hi"}
        headers = {"X-Twilio-Signature": self._sign_twilio(url, data)}
        self.client.post("/whatsapp", data=data, headers=headers)

        resp = self.client.get("/metrics")
        text = resp.get_data(as_text=True)
        self.assertNotIn("+919900011122", text)
        self.assertNotIn("9900011122", text)

    def test_no_pii_in_json_metrics(self):
        """Phone numbers should never appear in JSON /metrics output."""
        url = "http://localhost/whatsapp"
        data = {"From": "whatsapp:+919900011122", "Body": "hi"}
        headers = {"X-Twilio-Signature": self._sign_twilio(url, data)}
        self.client.post("/whatsapp", data=data, headers=headers)

        resp = self.client.get("/metrics", headers={"Accept": "application/json"})
        raw = resp.get_data(as_text=True)
        self.assertNotIn("+919900011122", raw)

    def test_readiness_no_credentials_leak(self):
        """Readiness endpoint should not expose database URLs or credentials."""
        resp = self.client.get("/readiness")
        raw = resp.get_data(as_text=True)
        self.assertNotIn("sqlite", raw)
        self.assertNotIn("password", raw)
        self.assertNotIn("token", raw)


if __name__ == "__main__":
    unittest.main()
