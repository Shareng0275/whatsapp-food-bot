"""Rate Limiting & Abuse Protection Module.

Supports:
- Redis-backed sliding-window rate limiting for multi-worker production setups
- Thread-safe in-memory sliding-window fallback
- Per-user WhatsApp message rate limiting
- IP-based webhook flood protection
- Promo-code brute-force prevention
- Payment initiation limits
"""
from abc import ABC, abstractmethod
import collections
import logging
import os
import time
import threading
from typing import Optional, Any, Tuple

logger = logging.getLogger("whatsapp_bot.rate_limiter")


class RateLimiter(ABC):
    """Abstract Base Class for rate limiting."""

    @abstractmethod
    def is_allowed(self, key: str, max_requests: int, window_seconds: int) -> Tuple[bool, int]:
        """Check if request for key is allowed under max_requests within window_seconds.

        Returns (is_allowed: bool, retry_after: int seconds).
        """
        pass

    @abstractmethod
    def reset(self, key: str) -> None:
        """Reset rate limit counter for a specific key."""
        pass

    @abstractmethod
    def clear(self) -> None:
        """Clear all rate limit data."""
        pass


class InMemoryRateLimiter(RateLimiter):
    """Thread-safe sliding-window rate limiter using in-memory timestamp deques."""

    def __init__(self):
        self._windows: dict[str, collections.deque] = collections.defaultdict(collections.deque)
        self._lock = threading.RLock()

    def is_allowed(self, key: str, max_requests: int, window_seconds: int) -> Tuple[bool, int]:
        now = time.time()
        cutoff = now - window_seconds

        with self._lock:
            timestamps = self._windows[key]

            # Evict timestamps outside the sliding window
            while timestamps and timestamps[0] <= cutoff:
                timestamps.popleft()

            if len(timestamps) >= max_requests:
                oldest = timestamps[0]
                retry_after = max(1, int(oldest + window_seconds - now))
                return False, retry_after

            timestamps.append(now)
            return True, 0

    def reset(self, key: str) -> None:
        with self._lock:
            self._windows.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._windows.clear()


class RedisRateLimiter(RateLimiter):
    """Redis-backed sliding-window rate limiter using sorted sets (ZSET).

    Includes automatic in-memory fallback if Redis connection fails.
    """

    def __init__(self, redis_client: Any, key_prefix: str = "wa_ratelimit:"):
        self.redis = redis_client
        self.key_prefix = key_prefix
        self._fallback = InMemoryRateLimiter()

    def _key(self, key: str) -> str:
        return f"{self.key_prefix}{key}"

    def is_allowed(self, key: str, max_requests: int, window_seconds: int) -> Tuple[bool, int]:
        now = time.time()
        cutoff = now - window_seconds
        r_key = self._key(key)

        try:
            pipeline = self.redis.pipeline(transaction=True)
            # 1. Remove old timestamps
            pipeline.zremrangebyscore(r_key, "-inf", cutoff)
            # 2. Add current timestamp
            pipeline.zadd(r_key, {str(now): now})
            # 3. Count remaining items
            pipeline.zcard(r_key)
            # 4. Set TTL on the sliding window key
            pipeline.expire(r_key, window_seconds + 1)
            # 5. Get oldest timestamp for retry_after calculation
            pipeline.zrange(r_key, 0, 0, withscores=True)

            results = pipeline.execute()
            count = results[2]
            oldest_entries = results[4]

            if count > max_requests:
                # Exceeded: remove the item we just added
                self.redis.zrem(r_key, str(now))
                retry_after = window_seconds
                if oldest_entries:
                    oldest_ts = oldest_entries[0][1]
                    retry_after = max(1, int(oldest_ts + window_seconds - now))
                return False, retry_after

            return True, 0

        except Exception as e:
            logger.warning("Redis rate limiter failed for %s (%s); falling back to in-memory limiter.", key, e)
            return self._fallback.is_allowed(key, max_requests, window_seconds)

    def reset(self, key: str) -> None:
        try:
            self.redis.delete(self._key(key))
        except Exception:
            pass
        self._fallback.reset(key)

    def clear(self) -> None:
        try:
            for k in self.redis.scan_iter(f"{self.key_prefix}*"):
                self.redis.delete(k)
        except Exception:
            pass
        self._fallback.clear()


def get_rate_limiter(store_type: str = "memory", redis_client: Any = None) -> RateLimiter:
    """Factory to return appropriate RateLimiter instance."""
    if store_type == "redis" and redis_client:
        return RedisRateLimiter(redis_client)
    return InMemoryRateLimiter()
