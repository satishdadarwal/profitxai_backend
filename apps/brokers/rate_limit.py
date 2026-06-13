"""
apps/brokers/rate_limit.py

Generic Redis-backed fixed-window rate limiter, keyed per broker name.

Rate limit is PER APP TOKEN (shared across all users on the same broker)
which is how broker APIs actually enforce limits.

    check(broker)              — non-blocking; returns True if allowed.
                                 Use for data fetches: skip on False.
    wait(broker, max_wait=2.0) — blocks up to max_wait sec for a free slot.
                                 Use for order placement: must not silently drop.

New broker? Add it to BROKER_RATE_LIMITS — zero other code changes needed.
"""

import logging
import time

from django.core.cache import cache

logger = logging.getLogger(__name__)

# Max requests per second, per broker.
# Conservative margins below official limits to absorb concurrent worker burst.
BROKER_RATE_LIMITS: dict[str, int] = {
    "fyers":    8,   # official ~10/sec; 2-req buffer
    "delta":   20,   # generous for public endpoints
    "dhan":     5,
    "zerodha":  8,
    "default":  5,   # unknown brokers
}


def _key(broker: str) -> str:
    return f"ratelimit:{broker}:{int(time.time())}"


def _limit(broker: str) -> int:
    return BROKER_RATE_LIMITS.get(broker, BROKER_RATE_LIMITS["default"])


def _increment(broker: str) -> int:
    """Atomically increment per-second counter; return new count."""
    key = _key(broker)
    cache.add(key, 0, timeout=2)   # NX: set 0 only if absent (atomic on Redis)
    return cache.incr(key)         # Redis INCR — atomic


def _current(broker: str) -> int:
    return int(cache.get(_key(broker)) or 0)


def check(broker: str) -> bool:
    """
    Consume one rate-limit slot. Returns True if the request is allowed.

    Increments the counter atomically. On False, the caller should skip
    the API call — the slot was still consumed (counts against this second).

    Logs at INFO so behaviour is visible during validation; downgrade to
    DEBUG once you've confirmed it's working correctly.
    """
    try:
        limit = _limit(broker)
        count = _increment(broker)
        allowed = count <= limit
        logger.info(
            "rate_limit | broker=%s | count=%d/%d | allowed=%s",
            broker, count, limit, allowed,
        )
        return allowed
    except Exception as exc:
        logger.error("rate_limit.check error | broker=%s | %s", broker, exc)
        return True   # fail open — limiter errors must not block trading


def wait(broker: str, max_wait: float = 2.0) -> None:
    """
    Poll (without consuming slots) until under limit, then consume one slot.
    After max_wait, allows the request through regardless.

    Use for order placement — must not silently drop a trade.
    """
    limit = _limit(broker)
    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        if _current(broker) < limit:
            _increment(broker)
            logger.info(
                "rate_limit.wait: slot acquired | broker=%s | count=%d/%d",
                broker, _current(broker), limit,
            )
            return
        time.sleep(0.05)
    logger.warning(
        "rate_limit.wait: max_wait=%.1fs exceeded | broker=%s | allowing through",
        max_wait, broker,
    )
    _increment(broker)   # consume slot even on timeout (count the call)
