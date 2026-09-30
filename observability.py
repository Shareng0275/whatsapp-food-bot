"""Observability Module for WhatsApp Food Ordering Bot.

Provides:
- Thread-safe in-process counters and histograms for technical and bot metrics
- Standardized event emission with structured logging integration
- Privacy-safe analytics (no PII in metrics)
- Prometheus-compatible /metrics text output
- Dashboard-ready metric snapshots via JSON
- Latency histogram with configurable bucket boundaries

Architecture:
    MetricsCollector (singleton per process)
    ├── Counters  — monotonically increasing integers
    ├── Gauges    — point-in-time values (e.g., active conversations)
    ├── Histograms — latency distribution buckets
    └── EventEmitter — structured log events tied to metrics

Privacy:
    All metric labels use safe_user_id (hashed), never raw phone numbers.
    No message body content is ever stored in metrics.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from structured_logger import generate_safe_user_id, get_correlation_id

logger = logging.getLogger("whatsapp_bot.observability")


# ---------------------------------------------------------------------------
# Histogram helper
# ---------------------------------------------------------------------------

class Histogram:
    """Simple histogram with configurable bucket boundaries for latency tracking."""

    DEFAULT_BUCKETS = (5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000)

    def __init__(self, name: str, buckets: tuple[float, ...] | None = None):
        self.name = name
        self.buckets = sorted(buckets or self.DEFAULT_BUCKETS)
        self._lock = threading.Lock()
        self._counts: Dict[float, int] = {b: 0 for b in self.buckets}
        self._counts[float("inf")] = 0
        self._sum: float = 0.0
        self._count: int = 0

    def observe(self, value: float) -> None:
        """Record a single observation (e.g., latency in milliseconds)."""
        with self._lock:
            self._sum += value
            self._count += 1
            for bucket in self.buckets:
                if value <= bucket:
                    self._counts[bucket] += 1
                    return
            self._counts[float("inf")] += 1

    def snapshot(self) -> Dict[str, Any]:
        """Return a point-in-time snapshot of the histogram."""
        with self._lock:
            return {
                "name": self.name,
                "count": self._count,
                "sum": round(self._sum, 2),
                "avg": round(self._sum / self._count, 2) if self._count > 0 else 0.0,
                "buckets": {
                    f"le_{int(b) if b != float('inf') else 'inf'}": c
                    for b, c in self._counts.items()
                },
            }

    def reset(self) -> None:
        with self._lock:
            for b in self._counts:
                self._counts[b] = 0
            self._sum = 0.0
            self._count = 0


# ---------------------------------------------------------------------------
# Metrics Collector (Singleton per process)
# ---------------------------------------------------------------------------

class MetricsCollector:
    """Thread-safe in-process metrics collector.

    Tracks counters, gauges, and histograms for both technical infrastructure
    and conversational bot metrics.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._counters: Dict[str, int] = defaultdict(int)
        self._gauges: Dict[str, float] = defaultdict(float)
        self._histograms: Dict[str, Histogram] = {}
        self._start_time = time.time()

        # Pre-register standard histograms
        self._histograms["webhook_latency_ms"] = Histogram("webhook_latency_ms")
        self._histograms["db_query_latency_ms"] = Histogram(
            "db_query_latency_ms", buckets=(1, 5, 10, 25, 50, 100, 250, 500)
        )
        self._histograms["recommendation_latency_ms"] = Histogram(
            "recommendation_latency_ms", buckets=(1, 5, 10, 25, 50, 100)
        )

    # -- Counters ------------------------------------------------------------

    def increment(self, name: str, value: int = 1) -> None:
        """Increment a counter by the given value."""
        with self._lock:
            self._counters[name] += value

    def get_counter(self, name: str) -> int:
        """Get current counter value."""
        with self._lock:
            return self._counters.get(name, 0)

    # -- Gauges --------------------------------------------------------------

    def set_gauge(self, name: str, value: float) -> None:
        """Set a gauge to an absolute value."""
        with self._lock:
            self._gauges[name] = value

    def inc_gauge(self, name: str, delta: float = 1.0) -> None:
        """Increment a gauge."""
        with self._lock:
            self._gauges[name] += delta

    def dec_gauge(self, name: str, delta: float = 1.0) -> None:
        """Decrement a gauge."""
        with self._lock:
            self._gauges[name] -= delta

    def get_gauge(self, name: str) -> float:
        """Get current gauge value."""
        with self._lock:
            return self._gauges.get(name, 0.0)

    # -- Histograms ----------------------------------------------------------

    def observe(self, histogram_name: str, value: float) -> None:
        """Record a value in a histogram."""
        with self._lock:
            if histogram_name not in self._histograms:
                self._histograms[histogram_name] = Histogram(histogram_name)
        self._histograms[histogram_name].observe(value)

    def get_histogram(self, histogram_name: str) -> Dict[str, Any]:
        """Get histogram snapshot."""
        h = self._histograms.get(histogram_name)
        return h.snapshot() if h else {}

    # -- Snapshot & Export ---------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        """Return a full metrics snapshot as a JSON-serializable dict."""
        with self._lock:
            counters = dict(self._counters)
            gauges = dict(self._gauges)

        histograms = {
            name: h.snapshot() for name, h in self._histograms.items()
        }

        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "uptime_seconds": round(time.time() - self._start_time, 1),
            "counters": counters,
            "gauges": gauges,
            "histograms": histograms,
        }

    def prometheus_text(self) -> str:
        """Export metrics in Prometheus text exposition format."""
        lines: List[str] = []

        with self._lock:
            for name, value in sorted(self._counters.items()):
                safe_name = name.replace(".", "_").replace("-", "_")
                lines.append(f"# TYPE whatsapp_bot_{safe_name} counter")
                lines.append(f"whatsapp_bot_{safe_name} {value}")

            for name, value in sorted(self._gauges.items()):
                safe_name = name.replace(".", "_").replace("-", "_")
                lines.append(f"# TYPE whatsapp_bot_{safe_name} gauge")
                lines.append(f"whatsapp_bot_{safe_name} {value}")

        for name, hist in self._histograms.items():
            snap = hist.snapshot()
            safe_name = name.replace(".", "_").replace("-", "_")
            lines.append(f"# TYPE whatsapp_bot_{safe_name} histogram")
            cumulative = 0
            for bucket_label, count in snap["buckets"].items():
                cumulative += count
                le_val = bucket_label.replace("le_", "")
                le_val = "+Inf" if le_val == "inf" else le_val
                lines.append(f'whatsapp_bot_{safe_name}_bucket{{le="{le_val}"}} {cumulative}')
            lines.append(f"whatsapp_bot_{safe_name}_sum {snap['sum']}")
            lines.append(f"whatsapp_bot_{safe_name}_count {snap['count']}")

        # Uptime gauge
        uptime = round(time.time() - self._start_time, 1)
        lines.append("# TYPE whatsapp_bot_uptime_seconds gauge")
        lines.append(f"whatsapp_bot_uptime_seconds {uptime}")

        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        """Reset all metrics (primarily for testing)."""
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
        for h in self._histograms.values():
            h.reset()


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------

_collector: Optional[MetricsCollector] = None
_collector_lock = threading.Lock()


def get_metrics_collector() -> MetricsCollector:
    """Return the process-wide MetricsCollector singleton."""
    global _collector
    if _collector is None:
        with _collector_lock:
            if _collector is None:
                _collector = MetricsCollector()
    return _collector


def reset_metrics_collector() -> None:
    """Reset the singleton (for testing)."""
    global _collector
    with _collector_lock:
        _collector = None


# ---------------------------------------------------------------------------
# Standardized Event Types
# ---------------------------------------------------------------------------

# Technical Events
EVENT_WEBHOOK_RECEIVED = "webhook_received"
EVENT_WEBHOOK_RESPONSE = "webhook_response"
EVENT_AUTH_FAILURE = "auth_failure"
EVENT_RATE_LIMIT = "rate_limit"
EVENT_DB_ERROR = "db_error"
EVENT_BOT_ERROR = "bot_error"
EVENT_TWILIO_API_CALL = "twilio_api_call"
EVENT_MEDIA_REJECTED = "media_rejected"
EVENT_DUPLICATE_WEBHOOK = "duplicate_webhook"

# Bot / Business Events
EVENT_CONVERSATION_STARTED = "conversation_started"
EVENT_CONVERSATION_COMPLETED = "conversation_completed"
EVENT_SEARCH_REQUEST = "search_request"
EVENT_RECOMMENDATION_GENERATED = "recommendation_generated"
EVENT_ITEM_SELECTED = "item_selected"
EVENT_ADDRESS_SUBMITTED = "address_submitted"
EVENT_PROMO_ATTEMPTED = "promo_attempted"
EVENT_PROMO_SUCCESS = "promo_success"
EVENT_PROMO_FAILURE = "promo_failure"
EVENT_ORDER_CREATED = "order_created"
EVENT_ORDER_CANCELLED = "order_cancelled"
EVENT_CONVERSATION_ABANDONED = "conversation_abandoned"
EVENT_FALLBACK_RESPONSE = "fallback_response"
EVENT_LOW_CONFIDENCE_NLU = "low_confidence_nlu"
EVENT_STATE_TRANSITION = "state_transition"
EVENT_PAYMENT_INITIATED = "payment_initiated"
EVENT_PAYMENT_SUCCESS = "payment_success"
EVENT_PAYMENT_FAILURE = "payment_failure"


# ---------------------------------------------------------------------------
# Event Emitter — structured log + metric in one call
# ---------------------------------------------------------------------------

def emit_event(
    event_type: str,
    *,
    safe_user_id: str = "",
    duration_ms: float | None = None,
    metadata: Dict[str, Any] | None = None,
    level: int = logging.INFO,
) -> None:
    """Emit a structured observability event.

    Simultaneously:
    1. Logs a structured JSON event via the whatsapp_bot logger
    2. Increments the corresponding counter in the MetricsCollector

    Parameters
    ----------
    event_type : str
        One of the EVENT_* constants above.
    safe_user_id : str
        Privacy-safe pseudonymous user identifier.
    duration_ms : float, optional
        Latency measurement to attach to the event.
    metadata : dict, optional
        Additional key-value pairs to include in the structured log.
    level : int
        Logging level (default: INFO).
    """
    collector = get_metrics_collector()
    collector.increment(f"events.{event_type}")

    extra: Dict[str, Any] = {
        "event_type": event_type,
        "correlation_id": get_correlation_id(),
    }
    if safe_user_id:
        extra["safe_user_id"] = safe_user_id
    if duration_ms is not None:
        extra["duration_ms"] = round(duration_ms, 2)
    if metadata:
        extra.update(metadata)

    logger.log(level, f"Event: {event_type}", extra=extra)


# ---------------------------------------------------------------------------
# Convenience: timed context manager
# ---------------------------------------------------------------------------

class Timer:
    """Context manager to measure elapsed time and optionally observe into a histogram.

    Usage::

        with Timer("webhook_latency_ms") as t:
            do_work()
        print(t.elapsed_ms)
    """

    def __init__(self, histogram_name: str | None = None):
        self.histogram_name = histogram_name
        self.start: float = 0.0
        self.elapsed_ms: float = 0.0

    def __enter__(self) -> "Timer":
        self.start = time.time()
        return self

    def __exit__(self, *args: Any) -> None:
        self.elapsed_ms = round((time.time() - self.start) * 1000, 2)
        if self.histogram_name:
            get_metrics_collector().observe(self.histogram_name, self.elapsed_ms)
