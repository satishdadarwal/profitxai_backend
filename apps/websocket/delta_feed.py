# apps/websocket/delta_feed.py
#
# FIXES:
# 1. Persistent async thread — new_event_loop() har call pe nahi
# 2. run_coroutine_threadsafe — thread-safe Redis broadcast
# 3. Correct group name — consumers.py _sanitize() se match
# 4. Adaptive sleep — fetch time ke hisaab se adjust hota hai
# 5. ✅ Redis ConnectionPool — har tick pe naya connection nahi banega

import asyncio
import json
import logging
import threading
import time
from typing import Set

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# ✅ Redis Connection Pool Singleton
# Module level pe ek baar banta hai — har tick pe reuse hota hai
# ─────────────────────────────────────────────────────────────
_redis_pool = None
_redis_client = None
_redis_lock = threading.Lock()


def _get_redis_client():
    global _redis_pool, _redis_client
    if _redis_client is None:
        with _redis_lock:
            if _redis_client is None:  # double-checked locking
                import redis as redis_lib
                from django.conf import settings as django_settings
                _redis_pool = redis_lib.ConnectionPool.from_url(
                    django_settings.REDIS_URL,
                    decode_responses=True,
                    max_connections=10,
                    socket_keepalive=True,
                    socket_connect_timeout=2,
                    socket_timeout=2,
                )
                _redis_client = redis_lib.Redis(connection_pool=_redis_pool)
                logger.info("DeltaFeed: Redis connection pool created")
    return _redis_client


# ─────────────────────────────────────────────────────────────

def normalize_symbol(symbol: str) -> str:
    """DELTA:BTC-USDT → BTC-USDT"""
    return symbol.replace("DELTA:", "").replace("NSE:", "").replace("BSE:", "").strip().upper()


def _symbol_to_group(symbol: str) -> str:
    """consumers.py ke _sanitize() se exactly match"""
    safe = symbol.replace(":", "_").replace("-", "_").replace(" ", "_")
    return f"symbol_{safe}"


class DeltaFeedManager:
    def __init__(self):
        self._subscribed: Set[str] = set()
        self._running = False
        self._lock = threading.Lock()

        # ✅ Ek persistent async loop — har broadcast pe naya loop nahi
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._poll_thread: threading.Thread | None = None
        self._channel_layer = None

    def _get_layer(self):
        if self._channel_layer is None:
            from channels.layers import get_channel_layer
            self._channel_layer = get_channel_layer()
        return self._channel_layer

    def _start_loop(self):
        """Persistent event loop — ek baar start, hamesha chalta rahe"""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _broadcast(self, group: str, payload: dict):
        """Thread-safe broadcast — run_coroutine_threadsafe use karo"""
        if self._loop is None or not self._loop.is_running():
            return
        try:
            layer = self._get_layer()
            if not layer:
                return
            future = asyncio.run_coroutine_threadsafe(
                layer.group_send(group, payload),
                self._loop,
            )
            future.result(timeout=2)  # 2s timeout — Redis slow ho toh skip
        except TimeoutError:
            logger.warning("Delta broadcast timeout [%s]", group)
        except Exception as e:
            logger.error("Delta broadcast error [%s]: %s", group, e)

    def subscribe(self, symbol: str):
        clean = normalize_symbol(symbol)
        with self._lock:
            self._subscribed.add(clean)
        logger.info("Delta subscribed: %s", clean)
        if not self._running:
            self.start()

    def unsubscribe(self, symbol: str):
        clean = normalize_symbol(symbol)
        with self._lock:
            self._subscribed.discard(clean)
        logger.info("Delta unsubscribed: %s", clean)

    def start(self):
        if self._running:
            return
        self._running = True

        # 1. Persistent event loop thread
        self._loop_thread = threading.Thread(
            target=self._start_loop, daemon=True, name="delta-loop"
        )
        self._loop_thread.start()

        # Loop ready hone ka wait
        for _ in range(20):
            if self._loop and self._loop.is_running():
                break
            time.sleep(0.05)

        # 2. Poll thread
        self._poll_thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="delta-poll"
        )
        self._poll_thread.start()
        logger.info("Delta feed started")

    def stop(self):
        self._running = False
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._poll_thread:
            self._poll_thread.join(timeout=5)
        logger.info("Delta feed stopped")

    def _poll_loop(self):
        from apps.market.delta_service import fetch_delta_tickers_bulk

        logger.info("Delta poll loop started")

        while self._running:
            cycle_start = time.time()
            try:
                with self._lock:
                    symbols = list(self._subscribed)

                if not symbols:
                    time.sleep(1)
                    continue

                quotes = fetch_delta_tickers_bulk(symbols)

                if not quotes:
                    logger.warning("Delta: no quotes for %s", symbols)
                    time.sleep(2)
                    continue

                for symbol in symbols:
                    data = quotes.get(symbol.upper())
                    if not data:
                        continue

                    ltp = data.get("ltp", 0)
                    if not ltp:
                        continue

                    normalized = normalize_symbol(symbol)
                    symbol_group = _symbol_to_group(normalized)

                    payload = {
                        "type": "market_update",
                        "data": {
                            "symbol": normalized,
                            "ltp": float(ltp),
                            "change": float(data.get("change", 0)),
                            "changePct": float(data.get("chg_pct", 0)),
                            "open": float(data.get("open", 0)),
                            "high": float(data.get("high", 0)),
                            "low": float(data.get("low", 0)),
                            "prevClose": float(data.get("prev_close", 0)),
                            "volume": float(data.get("volume", 0)),
                            "bid": float(data.get("bid", 0)),
                            "ask": float(data.get("ask", 0)),
                            "ts": int(time.time()),
                        },
                    }

                    self._broadcast("market", payload)
                    self._broadcast(symbol_group, payload)
                    self._publish_to_redis(payload["data"])
                    try:
                        from apps.brokers.feed_manager import on_price_tick
                        on_price_tick(normalized, float(ltp))
                    except Exception as _fe:
                        pass

                    logger.info(
                        "Delta tick | %s | ltp=%.4f | group=%s",
                        normalized, ltp, symbol_group,
                    )

            except Exception as e:
                logger.error("Delta poll error: %s", e, exc_info=True)

            # ✅ Adaptive sleep — fetch time minus karo
            elapsed = time.time() - cycle_start
            time.sleep(max(0, 2.0 - elapsed))

    def status(self):
        with self._lock:
            return {
                "running": self._running,
                "loop_alive": self._loop is not None and self._loop.is_running(),
                "subscribed": list(self._subscribed),
                "count": len(self._subscribed),
            }

    # ── ✅ Redis publish — CONNECTION POOL use karta hai ──────
    def _publish_to_redis(self, data: dict):
        """
        ✅ FIX: Connection pool use karta hai.
        Pehle har tick pe naya connection ban raha tha →
        TIME_WAIT leak → Error 10048 (port exhaustion).
        Ab ek pool se connection reuse hota hai.

        ✅ Issue #6 FIX: Delta ticks bhi snapshot hash mein cache karo.
        Fyers feed ki tarah same key (tick_snapshot) use karo taaki
        consumers.py ka _send_price_snapshot dono brokers ke liye kaam kare.
        """
        try:
            tick = {**data, "broker": "delta"}
            payload = json.dumps(tick)
            r = _get_redis_client()
            pipe = r.pipeline()
            pipe.publish("ticks:normalized", payload)
            pipe.hset("tick_snapshot", data.get("symbol", ""), payload)
            pipe.expire("tick_snapshot", 7200)   # 2 hours TTL
            pipe.execute()
        except Exception as e:
            logger.error("DeltaFeed: Redis publish error: %s", e)


delta_feed_manager = DeltaFeedManager()