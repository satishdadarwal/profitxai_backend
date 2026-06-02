# apps/websocket/dhan_feed.py
#
# Dhan Market Data Feed Manager
#
# Architecture: Polling-based singleton (Fyers WebSocket ki tarah)
# Dhan v2 /marketfeed/ohlc endpoint use karta hai (LTP + OHLC dono milte hain).
#
# Design principle:
#   Market data (ticks) = SHARED — 1 Dhan account se sabke liye
#   Orders/positions    = PER-USER — BrokerAdapterFactory user ka token use karta hai
#
# Flow:
#   DhanFeedManager.subscribe(symbol)
#   → _poll_loop() every 1.5s
#   → Dhan /v2/marketfeed/ohlc POST (master account token)
#   → Redis tick_snapshot → Django Channels → sabhi Flutter clients
#
# FIXES (2026-05-30):
#   ✅ FIX 1 — _load_credentials(): Master account pattern (Fyers ki tarah)
#              DHAN_MASTER_CLIENT_ID .env se — stable feed even if other
#              users login/logout karte rahe
#   ✅ FIX 2 — restart_with_new_token() added — token refresh ke baad
#              in-memory update turant hota hai, restart nahi chahiye
#   ✅ FIX 3 — _poll_loop(): token missing hone pe turant reload (not just every 60 cycles)
#   ✅ FIX 4 — IDX_I segment use karo INDEX ke liye (NSE_FNO WRONG tha)
#              Dhan official docs: Index segment = "IDX_I"
#              NIFTY=13, BANKNIFTY=25, SENSEX=51, FINNIFTY=27, MIDCPNIFTY=442
#   ✅ FIX 5 — /marketfeed/ohlc use karo — LTP ke saath open/high/low/close bhi milta hai
#   ✅ FIX 6 — Response parsing fix: {"data": {"IDX_I": {"13": {"last_price": ...}}}}
#              Keys are string securityIds, not exchange segments
#
# .env mein add karo:
#   DHAN_MASTER_CLIENT_ID=1000000001   # apna master Dhan account client ID
#
# SEBI note: Static IP whitelist zaruri hai (web.dhan.co → My Profile → Static IP)
# 403 error = IP whitelist nahi hua


import asyncio
import json
import logging
import threading
import time
from datetime import datetime, time as dt_time
from typing import Optional, Set

import requests

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# ✅ Redis Connection Pool Singleton
# ─────────────────────────────────────────────────────────────
_redis_pool = None
_redis_client = None
_redis_lock = threading.Lock()

# Market hours
MARKET_OPEN_TIME  = dt_time(9, 15)
MARKET_CLOSE_TIME = dt_time(15, 30)

DHAN_BASE_URL = "https://api.dhan.co/v2"

# Poll interval seconds (Dhan rate limit: 1 req/sec per endpoint)
POLL_INTERVAL = 3


# ─────────────────────────────────────────────────────────────
# ✅ FIX 4: Dhan official securityId map for indices
# Source: https://dhanhq.co/docs/v2/  (IDX_I segment)
# NIFTY=13, BANKNIFTY=25, SENSEX=51, FINNIFTY=27, MIDCPNIFTY=442
# ─────────────────────────────────────────────────────────────
# Internal symbol → (dhan_security_id, exchange_segment)
DHAN_INDEX_MAP = {
    # Fyers/internal format
    "NIFTY50-INDEX":     ("13",  "IDX_I"),
    "NIFTYBANK-INDEX":   ("25",  "IDX_I"),
    "SENSEX-INDEX":      ("51",  "IDX_I"),
    "FINNIFTY-INDEX":    ("27",  "IDX_I"),
    "MIDCPNIFTY-INDEX":  ("442", "IDX_I"),
    "NIFTYNXT50-INDEX":  ("29",  "IDX_I"),
    "BANKEX-INDEX":      ("194", "IDX_I"),  # BSE BANKEX
    # Short names
    "NIFTY50":    ("13",  "IDX_I"),
    "NIFTYBANK":  ("25",  "IDX_I"),
    "BANKNIFTY":  ("25",  "IDX_I"),
    "SENSEX":     ("51",  "IDX_I"),
    "FINNIFTY":   ("27",  "IDX_I"),
    "MIDCPNIFTY": ("442", "IDX_I"),
}

# Reverse map: securityId → internal clean symbol (response parse ke liye)
# "13" → "NIFTY50-INDEX"
DHAN_ID_TO_SYMBOL = {v[0]: k for k, v in DHAN_INDEX_MAP.items() if "-INDEX" in k}


def _get_redis_client():
    global _redis_pool, _redis_client
    if _redis_client is None:
        with _redis_lock:
            if _redis_client is None:
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
                logger.info("DhanFeed: Redis connection pool created")
    return _redis_client


# ─────────────────────────────────────────────────────────────
# Symbol helpers
# ─────────────────────────────────────────────────────────────

def normalize_symbol(symbol: str) -> str:
    """NSE:NIFTY50-INDEX → NIFTY50-INDEX"""
    return (
        symbol
        .replace("DHAN:", "")
        .replace("NSE:", "")
        .replace("BSE:", "")
        .strip()
        .upper()
    )


def _symbol_to_group(symbol: str) -> str:
    """consumers.py ke _sanitize() se exactly match"""
    safe = symbol.replace(":", "_").replace("-", "_").replace(" ", "_")
    return f"symbol_{safe}"


def _symbol_to_dhan_info(raw_symbol: str):
    """
    ✅ FIX 4: Internal symbol → (security_id_str, exchange_segment)

    Index symbols ke liye IDX_I segment + numeric securityId use karo.
    FnO / Equity ke liye NSE_FNO / NSE_EQ (string symbol — production mein
    Dhan security master CSV se numeric ID fetch karo).

    Returns: (security_id: str, segment: str) ya None
    """
    clean = normalize_symbol(raw_symbol)

    # ── Index lookup (IDX_I) ──
    if clean in DHAN_INDEX_MAP:
        return DHAN_INDEX_MAP[clean]

    # ── Strip exchange prefix aur phir check ──
    sym = raw_symbol.upper().strip()
    if ":" in sym:
        _, sym = sym.split(":", 1)

    if sym in DHAN_INDEX_MAP:
        return DHAN_INDEX_MAP[sym]

    # ── Options: CE/PE ──
    if sym.endswith("CE") or sym.endswith("PE"):
        seg = "NSE_FNO"
        if any(x in sym for x in ("SENSEX", "BANKEX")):
            seg = "BSE_FNO"
        return sym, seg

    # ── Futures ──
    if "FUT" in sym:
        seg = "NSE_FNO"
        if any(x in sym for x in ("SENSEX", "BANKEX")):
            seg = "BSE_FNO"
        return sym, seg

    # ── Equity ──
    eq_sym = sym.replace("-EQ", "")
    return eq_sym, "NSE_EQ"


def _is_market_open() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    return MARKET_OPEN_TIME <= now.time() <= MARKET_CLOSE_TIME


# ─────────────────────────────────────────────────────────────
# DhanFeedManager
# ─────────────────────────────────────────────────────────────

class DhanFeedManager:
    """
    Dhan market data feed — polling-based LTP fetcher.

    Usage:
        dhan_feed_manager.subscribe("NSE:NIFTY50-INDEX")
        dhan_feed_manager.start()
    """

    def __init__(self):
        self._subscribed: Set[str] = set()
        self._running = False
        self._lock = threading.Lock()
        self._poll_thread: Optional[threading.Thread] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._channel_layer = None

        # Credentials — start() se set hote hain
        self._client_id: str = ""
        self._access_token: str = ""

    # ── Layer & loop helpers ──────────────────────────────────

    def _get_layer(self):
        if self._channel_layer is None:
            from channels.layers import get_channel_layer
            self._channel_layer = get_channel_layer()
        return self._channel_layer

    def _start_loop(self):
        """Persistent event loop thread"""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _broadcast(self, group: str, payload: dict):
        """Thread-safe broadcast via run_coroutine_threadsafe"""
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
            future.result(timeout=2)
        except TimeoutError:
            logger.warning("DhanFeed broadcast timeout [%s]", group)
        except Exception as e:
            logger.error("DhanFeed broadcast error [%s]: %s", group, e)

    # ── Credentials ───────────────────────────────────────────

    def _load_credentials(self):
        """
        DB se Dhan master account ka token fetch karo.

        Priority:
          1. settings.DHAN_MASTER_CLIENT_ID se match karo (Fyers master pattern)
          2. Fallback: koi bhi active + verified account

        Har 60 poll cycle (~90s) mein call hota hai — stale token auto-refresh.
        """
        try:
            from apps.brokers.models import BrokerAccount
            from django.conf import settings as django_settings

            master_client_id = getattr(django_settings, "DHAN_MASTER_CLIENT_ID", "").strip()

            # ── Step 1: Master account — DHAN_MASTER_CLIENT_ID se match ──
            if master_client_id:
                master_account = (
                    BrokerAccount.objects.filter(
                        broker="dhan",
                        is_active=True,
                        is_verified=True,
                        dhan_client_id=master_client_id,
                    )
                    .exclude(dhan_access_token__isnull=True)
                    .exclude(dhan_access_token="")
                    .first()
                )
                if master_account:
                    self._client_id    = master_account.dhan_client_id or ""
                    self._access_token = master_account.dhan_access_token or ""
                    logger.info(
                        "DhanFeed: ✅ Master account token loaded | "
                        "account=%s | client_id=%s",
                        master_account.id, self._client_id,
                    )
                    return True

                logger.warning(
                    "DhanFeed: ⚠️  Master account (DHAN_MASTER_CLIENT_ID=%s) "
                    "not found or no token — falling back to any active account",
                    master_client_id,
                )

            # ── Step 2: Fallback — koi bhi active verified account ────────
            fallback_account = (
                BrokerAccount.objects.filter(
                    broker="dhan",
                    is_active=True,
                    is_verified=True,
                )
                .exclude(dhan_access_token__isnull=True)
                .exclude(dhan_access_token="")
                .order_by("-updated_at")
                .first()
            )
            if fallback_account:
                self._client_id    = fallback_account.dhan_client_id or ""
                self._access_token = fallback_account.dhan_access_token or ""
                logger.warning(
                    "DhanFeed: ⚠️  Using fallback account (NOT master) | "
                    "account=%s | client_id=%s — "
                    "Set DHAN_MASTER_CLIENT_ID in .env for stable feed.",
                    fallback_account.id, self._client_id,
                )
                return True

            logger.error("DhanFeed: No active Dhan account found — feed will pause")
            return False

        except Exception as e:
            logger.error("DhanFeed: credential load error: %s", e)
            return False

    def _headers(self) -> dict:
        return {
            "access-token": self._access_token,
            "client-id":    self._client_id,
            "Content-Type": "application/json",
            "Accept":       "application/json",
        }

    # ── Subscribe / unsubscribe ───────────────────────────────

    def subscribe(self, symbol: str):
        clean = normalize_symbol(symbol)
        with self._lock:
            self._subscribed.add(clean)
        logger.info("DhanFeed subscribed: %s", clean)
        if not self._running:
            self.start()

    def unsubscribe(self, symbol: str):
        clean = normalize_symbol(symbol)
        with self._lock:
            self._subscribed.discard(clean)
        logger.info("DhanFeed unsubscribed: %s", clean)

    # ── Lifecycle ─────────────────────────────────────────────

    def start(self):
        if self._running:
            return
        self._running = True

        # 1. Event loop thread
        self._loop_thread = threading.Thread(
            target=self._start_loop, daemon=True, name="dhan-loop"
        )
        self._loop_thread.start()

        # Loop ready hone ka wait
        for _ in range(20):
            if self._loop and self._loop.is_running():
                break
            time.sleep(0.05)

        # 2. Poll thread
        self._poll_thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="dhan-poll"
        )
        self._poll_thread.start()
        logger.info("DhanFeed started")

    def restart_with_new_token(self, client_id: str, access_token: str):
        """
        Naya Dhan token aane pe feed token in-memory update karo.
        Poll loop already har 90s mein DB se refresh karta hai, lekin yeh
        method turant next poll mein naya token use karwata hai.
        """
        logger.info("DhanFeed: restart_with_new_token called | client_id=%s", client_id)
        with self._lock:
            self._client_id    = client_id
            self._access_token = access_token
            sym_count = len(self._subscribed)

        logger.info(
            "DhanFeed: ✅ Token updated in-memory | symbols=%d — "
            "next poll will use new token immediately",
            sym_count,
        )

    def stop(self):
        self._running = False
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._poll_thread:
            self._poll_thread.join(timeout=5)
        logger.info("DhanFeed stopped")

    # ── Poll loop ─────────────────────────────────────────────

    def _poll_loop(self):
        logger.info("DhanFeed poll loop started")
        cred_refresh_counter = 0

        while self._running:
            cycle_start = time.time()

            try:
                # Credentials refresh:
                # 1. Har 60 cycle (~90s) pe DB se reload — token rotation ke liye
                # 2. Token missing ho toh turant reload karo
                need_reload = (
                    cred_refresh_counter % 60 == 0
                    or not self._access_token
                    or not self._client_id
                )
                if need_reload:
                    if not self._load_credentials():
                        time.sleep(5)
                        cred_refresh_counter += 1
                        continue
                cred_refresh_counter += 1

                with self._lock:
                    symbols = list(self._subscribed)

                if not symbols:
                    time.sleep(1)
                    continue

                # ✅ Market closed hone pe bhi subscribe list maintain karo
                # Sirf fetch band karo — taaki open hone pe turant data mile
                if not _is_market_open():
                    time.sleep(10)
                    continue

                quotes = self._fetch_ohlc_bulk(symbols)

                for raw_symbol in symbols:
                    clean_sym = normalize_symbol(raw_symbol)
                    data = quotes.get(clean_sym)
                    if not data:
                        continue

                    ltp = data.get("ltp", 0)
                    if not ltp:
                        continue

                    symbol_group = _symbol_to_group(clean_sym)

                    payload = {
                        "type": "market_update",
                        "data": {
                            "symbol":    clean_sym,
                            "ltp":       float(ltp),
                            "change":    float(data.get("change",     0)),
                            "changePct": float(data.get("change_pct", 0)),
                            "open":      float(data.get("open",       0)),
                            "high":      float(data.get("high",       0)),
                            "low":       float(data.get("low",        0)),
                            "prevClose": float(data.get("prev_close", 0)),
                            "volume":    float(data.get("volume",     0)),
                            "bid":       float(data.get("bid",        0)),
                            "ask":       float(data.get("ask",        0)),
                            "ts":        int(time.time()),
                            "broker":    "dhan",
                        },
                    }

                    # Broadcast to channel groups
                    self._broadcast("market", payload)
                    self._broadcast(symbol_group, payload)
                    self._publish_to_redis(payload["data"])

                    # feed_manager.py ka on_price_tick call karo
                    try:
                        from apps.brokers.feed_manager import on_price_tick
                        on_price_tick(clean_sym, float(ltp), payload["data"])
                    except Exception as ft_err:
                        logger.debug("DhanFeed: feed_manager.on_price_tick error: %s", ft_err)

            except Exception as e:
                logger.error("DhanFeed poll error: %s", e, exc_info=True)

            elapsed = time.time() - cycle_start
            time.sleep(max(0, POLL_INTERVAL - elapsed))

    # ── ✅ FIX 5: Dhan OHLC fetch (LTP + open/high/low/close) ─
    def _fetch_ohlc_bulk(self, symbols: list) -> dict:
        """
        ✅ FIX 4 + 5 + 6: Dhan v2 /marketfeed/ohlc — correct segment + securityId.

        Index ke liye:
            Request:  {"IDX_I": [13, 25, 51]}   ← numeric int IDs
            Response: {"data": {"IDX_I": {"13": {"last_price": 22500, "ohlc": {...}}}}}

        FnO ke liye:
            Request:  {"NSE_FNO": [49081, 49082]}
            Response: {"data": {"NSE_FNO": {"49081": {"last_price": 368.15, ...}}}}

        Returns: {clean_symbol: {ltp, open, high, low, prev_close, change, change_pct}}
        """
        if not self._access_token or not self._client_id:
            return {}

        # Group by exchange segment
        # by_exchange: {segment: [int_security_id, ...]}
        by_exchange: dict = {}
        # id_to_clean: {"IDX_I:13": "NIFTY50-INDEX"} — response parse ke liye
        id_to_clean: dict = {}

        for raw in symbols:
            info = _symbol_to_dhan_info(raw)
            if not info:
                continue
            security_id_str, segment = info
            clean = normalize_symbol(raw)

            # ✅ IDX_I ke liye numeric int, baaki ke liye bhi int
            try:
                security_id_int = int(security_id_str)
            except (ValueError, TypeError):
                # FnO string symbols (not yet numeric) — skip for now
                logger.debug("DhanFeed: non-numeric securityId=%s for %s — skipping", security_id_str, raw)
                continue

            by_exchange.setdefault(segment, []).append(security_id_int)
            id_to_clean[f"{segment}:{security_id_str}"] = clean

        if not by_exchange:
            return {}

        try:
            resp = requests.post(
                f"{DHAN_BASE_URL}/marketfeed/ohlc",
                headers=self._headers(),
                json=by_exchange,
                timeout=(3, 8),
            )
            resp.raise_for_status()
            raw_resp = resp.json()

            # ✅ FIX 6: Correct response parsing
            # {"data": {"IDX_I": {"13": {"last_price": 22500, "ohlc": {...}}}}}
            data_block = raw_resp.get("data", {})
            result = {}

            for segment, items in data_block.items():
                if not isinstance(items, dict):
                    continue
                for sec_id_str, item in items.items():
                    lookup_key = f"{segment}:{sec_id_str}"
                    clean = id_to_clean.get(lookup_key)
                    if not clean:
                        # Fallback: IDX_I reverse map
                        clean = DHAN_ID_TO_SYMBOL.get(sec_id_str)
                    if not clean:
                        logger.debug("DhanFeed: no clean symbol for %s", lookup_key)
                        continue

                    ltp = float(item.get("last_price", 0) or 0)
                    if ltp <= 0:
                        continue

                    ohlc = item.get("ohlc", {}) or {}
                    open_  = float(ohlc.get("open",  0) or ltp)
                    high   = float(ohlc.get("high",  0) or ltp)
                    low    = float(ohlc.get("low",   0) or ltp)
                    close  = float(ohlc.get("close", 0) or ltp)  # prev close

                    change     = round(ltp - close, 2) if close else 0
                    change_pct = round((change / close * 100) if close else 0, 2)

                    result[clean] = {
                        "ltp":        ltp,
                        "open":       open_,
                        "high":       high,
                        "low":        low,
                        "prev_close": close,
                        "change":     change,
                        "change_pct": change_pct,
                        "volume":     float(item.get("volume", 0) or 0),
                        "bid":        0.0,
                        "ask":        0.0,
                    }

            logger.debug(
                "DhanFeed: ohlc fetch | requested=%d | parsed=%d",
                sum(len(v) for v in by_exchange.values()), len(result),
            )
            return result

        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 403:
                logger.error(
                    "DhanFeed: 403 Forbidden — VPS IP whitelist nahi hua. "
                    "web.dhan.co → My Profile → Static IP mein add karo."
                )
            elif e.response is not None and e.response.status_code == 401:
                logger.error(
                    "DhanFeed: 401 Unauthorized — Dhan token expire ho gaya. "
                    "Naya access token generate karo."
                )
                # Force credential reload next cycle
                self._access_token = ""
            else:
                logger.error("DhanFeed: HTTP error: %s", e)
            return {}
        except Exception as e:
            logger.error("DhanFeed: fetch error: %s", e)
            return {}

    # ── Redis publish ─────────────────────────────────────────

    def _publish_to_redis(self, data: dict):
        """
        Redis publish + tick_snapshot hash update.
        consumers.py ka _send_price_snapshot() dono brokers ke liye
        same hash use karta hai — Dhan bhi compatible hai.
        """
        try:
            tick = {**data, "broker": "dhan"}
            payload = json.dumps(tick)
            r = _get_redis_client()
            pipe = r.pipeline()
            pipe.publish("ticks:normalized", payload)
            pipe.hset("tick_snapshot", data.get("symbol", ""), payload)
            pipe.expire("tick_snapshot", 7200)
            pipe.execute()
        except Exception as e:
            logger.error("DhanFeed: Redis publish error: %s", e)

    # ── Status ────────────────────────────────────────────────

    def status(self) -> dict:
        with self._lock:
            return {
                "running":      self._running,
                "loop_alive":   self._loop is not None and self._loop.is_running(),
                "subscribed":   list(self._subscribed),
                "count":        len(self._subscribed),
                "client_id":    self._client_id,
                "market_open":  _is_market_open(),
            }


# ── Singleton ─────────────────────────────────────────────────
dhan_feed_manager = DhanFeedManager()