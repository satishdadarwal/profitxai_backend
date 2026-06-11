# apps/websocket/fyers_feed.py
#
# FIXES HISTORY:
# 1. Redis ConnectionPool singleton
# 2. _get_access_token() — is_verified=True filter hata diya
# 3. restart_with_new_token() — token expire pe subscriptions maintain karo
# 4. onclose() mein self._fyers = None — fresh object on reconnect
# 5. _reconnect() mein None token fallback
# 6. ✅ Market hours check — connection timeout gracefully handle karo
# 7. ✅ Heartbeat improvements
# 8. ✅ FIX: _get_access_token() — master account ko priority do
#           Problem: Chanchal login kare to uska token feed ke liye pick ho sakta tha
#           Fix: settings.FYERS_APP_ID wala account HAMESHA prefer karo
#           Fallback: koi bhi active account (agar master na mile)

import json
import logging
import threading
import time
from datetime import datetime, time as dt_time

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────
# Redis Connection Pool Singleton
# ─────────────────────────────────────────────────────────────────
_redis_pool = None
_redis_client = None
_redis_lock = threading.Lock()

KEEPALIVE_SYMBOL = "NSE:NIFTY50-INDEX"

# ✅ Market hours configuration
MARKET_OPEN_TIME  = dt_time(9, 15)   # 9:15 AM
MARKET_CLOSE_TIME = dt_time(15, 30)  # 3:30 PM


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
                logger.info("FyersFeed: Redis connection pool created")
    return _redis_client


# ─────────────────────────────────────────────────────────────────

def _sanitize(symbol: str) -> str:
    safe = symbol.replace(":", "_").replace("-", "_").replace(" ", "_")
    return f"symbol_{safe}"


def _to_fyers_symbol(symbol: str) -> str:
    from apps.brokers.symbol_mapper import normalize_for_fyers
    return normalize_for_fyers(symbol)


def _is_market_open() -> bool:
    """Check if current time is within market hours (Mon-Fri 9:15 AM - 3:30 PM)"""
    now = datetime.now()
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    current_time = now.time()
    return MARKET_OPEN_TIME <= current_time <= MARKET_CLOSE_TIME


# ─────────────────────────────────────────────────────────────────
class FyersFeedManager:

    def __init__(self):
        self._fyers = None
        self._subscribed: set[str] = set()
        self._lock = threading.Lock()
        self._started = False
        self._connected = False
        self._channel_layer = None
        self._current_token: str | None = None
        self._heartbeat_stop: threading.Event | None = None
        self._reconnect_attempts = 0
        self._max_reconnect_attempts = 3
        # ✅ FIX: market group throttle — har tick pe broadcast band karo
        # "8 of 11 channels over capacity in group market" fix
        # Sirf 1 second mein ek baar market group ko bhejo
        self._market_last_broadcast: dict[str, float] = {}  # symbol -> timestamp
        self._market_broadcast_interval = 1.0  # seconds

    # ── Channel layer ─────────────────────────────────────────
    def _get_channel_layer(self):
        if self._channel_layer is None:
            self._channel_layer = get_channel_layer()
        return self._channel_layer

    # ── ✅ FIX 8: Master account token — hamesha priority ─────
    def _get_access_token(self) -> str | None:
        """
        ✅ FIX 8: Master account ko HAMESHA prefer karo.

        Problem jo thi:
          - Chanchal login karta hai → BrokerAccount(user=chanchal, updated_at=now)
          - _get_access_token() → order_by("-updated_at") → Chanchal ka account pick hota
          - Master ka token feed ke liye use nahi hota
          - Chanchal logout kare / token expire ho → feed band

        Solution:
          1. Pehle settings.FYERS_APP_ID wala account dhundo (master)
          2. Wahan bhi na mile → koi bhi active account (graceful fallback)
        """
        try:
            from apps.brokers.models import BrokerAccount
            from django.conf import settings as django_settings

            master_app_id = getattr(django_settings, "FYERS_APP_ID", "").strip()

            # ── Step 1: Master account — app_id match karo ───
            if master_app_id:
                master_account = (
                    BrokerAccount.objects
                    .filter(
                        broker="fyers",
                        is_active=True,
                        app_id=master_app_id,
                    )
                    .exclude(access_token__isnull=True)
                    .exclude(access_token="")
                    .order_by("-updated_at")
                    .first()
                )

                if master_account:
                    token = f"{master_account.app_id}:{master_account.access_token}"
                    logger.info(
                        "FyersFeed: ✅ Master account token loaded | "
                        "account=%s | user=%s",
                        master_account.id, master_account.user_id,
                    )
                    return token

                logger.warning(
                    "FyersFeed: ⚠️  Master account (app_id=%s) not found or no token — "
                    "falling back to any active account",
                    master_app_id,
                )

            # ── Step 2: Fallback — koi bhi active account ────
            fallback_account = (
                BrokerAccount.objects
                .filter(broker="fyers", is_active=True)
                .exclude(access_token__isnull=True)
                .exclude(access_token="")
                .order_by("-updated_at")
                .first()
            )

            if not fallback_account:
                logger.error("FyersFeed: No active Fyers account with token found")
                return None

            token = f"{fallback_account.app_id}:{fallback_account.access_token}"
            logger.warning(
                "FyersFeed: ⚠️  Using fallback account (NOT master) | "
                "account=%s | user=%s | app_id=%s — "
                "Set FYERS_APP_ID in .env and ensure master account is logged in.",
                fallback_account.id,
                fallback_account.user_id,
                fallback_account.app_id,
            )
            return token

        except Exception as e:
            logger.error("FyersFeed: DB error getting token: %s", e)
            return None

    # ── Start ─────────────────────────────────────────────────
    def start(self, token: str | None = None):
        with self._lock:
            if self._started:
                logger.debug("FyersFeed: Already started — skipping")
                return
            self._started = True

        use_token = token or self._get_access_token()
        if not use_token:
            logger.error("FyersFeed: Cannot start — no token")
            with self._lock:
                self._started = False
            return

        self._current_token = use_token

        if not self._subscribed:
            try:
                from apps.orders.models import Order
                open_symbols = Order.objects.filter(
                    mode=Order.Mode.PAPER,
                    status=Order.Status.OPEN,
                ).values_list('symbol_display', flat=True).distinct()
                for sym in open_symbols:
                    # Crypto symbols skip karo — Fyers support nahi karta
                    if "-USDT" in sym.upper() or "-USD" in sym.upper() or "USDT" in sym.upper():
                        logger.info("FyersFeed: Pre-load skipping crypto: %s", sym)
                        continue
                    self._subscribed.add(_to_fyers_symbol(sym))
                if self._subscribed:
                    logger.info(
                        "FyersFeed: Pre-loaded %d open trade symbols",
                        len(self._subscribed),
                    )
                else:
                    logger.info("FyersFeed: No open trades — subscribed set empty")
            except Exception as e:
                logger.error("FyersFeed: Pre-load symbols failed: %s", e)

        logger.info("FyersFeed: Subscribed set at start: %s", self._subscribed)
        thread = threading.Thread(target=self._run, args=(use_token,), daemon=True)
        thread.start()

    # ── Token update ke baad WS restart ──────────────────────
    def restart_with_new_token(self, new_token: str):
        """
        ✅ FIX 8 extended: restart_with_new_token sirf master token pe hi karo.

        Agar koi user login karta hai (Chanchal), uska token yahan nahi aana chahiye.
        Ye method sirf views.py ke _start_feed_after_token() se call hoti hai,
        jo sirf OAuth complete hone pe call hoti hai.

        Lekin agar Chanchal login kare aur uska token yahan aaye,
        toh _get_access_token() mein master check already sahi token return karega
        next reconnect pe.
        """
        logger.info("FyersFeed: restart_with_new_token — gracefully restarting WS")

        with self._lock:
            saved_subscriptions = set(self._subscribed)
            self._current_token = new_token

        if self._heartbeat_stop is not None:
            self._heartbeat_stop.set()

        old_fyers = self._fyers
        if old_fyers is not None:
            try:
                old_fyers.close_connection()
                logger.info("FyersFeed: Old WS connection closed")
            except Exception as e:
                logger.warning("FyersFeed: close_connection error (ignoring): %s", e)

        with self._lock:
            self._started = False
            self._connected = False
            self._fyers = None
            self._subscribed = saved_subscriptions

        time.sleep(2)
        logger.info(
            "FyersFeed: Restarting with new token | symbols=%d",
            len(saved_subscriptions),
        )
        self.start(token=new_token)

    # ── Ping — SDK ka apna __ping() use karo ─────────────────
    def _send_ping(self) -> bool:
        fyers_obj = self._fyers
        if fyers_obj is None:
            return False

        if not self._connected:
            logger.debug("FyersFeed: Heartbeat — not connected yet, skipping")
            return False

        # Strategy 1: SDK ka apna __ping() call karo
        sdk_ping = getattr(fyers_obj, '_FyersDataSocket__ping', None)
        if sdk_ping is not None and callable(sdk_ping):
            try:
                sdk_ping()
                logger.debug("FyersFeed: Heartbeat — SDK __ping() called ✅")
                return True
            except Exception as e:
                logger.warning("FyersFeed: SDK __ping() failed: %s", e)

        # Strategy 2: __ws_object pe seedha binary frame
        try:
            import websocket as _ws_module
            ws_obj = getattr(fyers_obj, '_FyersDataSocket__ws_object', None)
            if ws_obj is not None:
                sock = getattr(ws_obj, 'sock', None)
                if sock is not None and getattr(sock, 'connected', False):
                    ws_obj.send(
                        bytes([0, 1, 11]),
                        opcode=_ws_module.ABNF.OPCODE_BINARY,
                    )
                    logger.debug("FyersFeed: Heartbeat — binary ping via ws_object ✅")
                    return True
        except Exception as e:
            logger.warning("FyersFeed: Heartbeat ws_object ping error: %s", e)

        return False

    # ── WebSocket run loop ────────────────────────────────────
    def _run(self, token: str):
        try:
            from fyers_apiv3.FyersWebsocket import data_ws
        except ImportError:
            logger.error("FyersFeed: fyers_apiv3 not installed")
            with self._lock:
                self._started = False
            return

        if self._heartbeat_stop is not None:
            self._heartbeat_stop.set()

        _heartbeat_stop = threading.Event()
        self._heartbeat_stop = _heartbeat_stop

        def _heartbeat_loop():
            logger.info("FyersFeed: Heartbeat thread started ✅")
            _heartbeat_stop.wait(timeout=3)
            if not _heartbeat_stop.is_set():
                result = self._send_ping()
                logger.info(
                    "FyersFeed: First ping result=%s | self._connected=%s",
                    result, self._connected,
                )
            while not _heartbeat_stop.is_set():
                _heartbeat_stop.wait(timeout=12)
                if _heartbeat_stop.is_set():
                    break
                self._send_ping()
            logger.info("FyersFeed: Heartbeat thread stopped")

        def onmessage(msg):
            self._on_fyers_message(msg)

        def onerror(err):
            logger.error("FyersFeed WS error: %s", err)
            if isinstance(err, dict):
                code = err.get("code")
                if code in (-99, -300):
                    logger.warning(
                        "FyersFeed: Token error (code=%s) — triggering emergency refresh",
                        code,
                    )
                    _trigger_emergency_token_refresh()
                    return
            if not _is_market_open():
                logger.info("FyersFeed: Market is closed — connection error expected")
                _heartbeat_stop.set()
                with self._lock:
                    self._started = False
                    self._connected = False
                return

        def onclose(msg):
            logger.warning("FyersFeed WS closed: %s", msg)
            _heartbeat_stop.set()
            with self._lock:
                self._started = False
                self._connected = False
                self._fyers = None

            if not _is_market_open():
                logger.info("FyersFeed: Market closed — not reconnecting")
                self._reconnect_attempts = 0
                return

            def _reconnect():
                self._reconnect_attempts += 1
                if self._reconnect_attempts > self._max_reconnect_attempts:
                    logger.warning(
                        "FyersFeed: Max reconnect attempts reached (%d) — giving up",
                        self._max_reconnect_attempts,
                    )
                    self._reconnect_attempts = 0
                    return

                wait_time = min(5 * self._reconnect_attempts, 30)
                logger.info(
                    "FyersFeed: Reconnect attempt %d/%d — waiting %ds",
                    self._reconnect_attempts,
                    self._max_reconnect_attempts,
                    wait_time,
                )
                time.sleep(wait_time)

                # ✅ FIX 8: Reconnect pe bhi master token use karo
                fresh_token = self._get_access_token() or self._current_token
                if fresh_token:
                    self.start(token=fresh_token)
                else:
                    logger.error("FyersFeed: No token available — reconnect aborted")

            threading.Thread(target=_reconnect, daemon=True).start()

        def onopen():
            logger.info("FyersFeed: Fyers WS connected ✅")
            self._reconnect_attempts = 0
            with self._lock:
                self._connected = True
                symbols = list(self._subscribed)

            logger.info("FyersFeed: onopen — symbols to subscribe: %s", symbols)

            if symbols:
                logger.info(
                    "FyersFeed: Subscribing %d symbols on connect: %s",
                    len(symbols), symbols[:5],
                )
                self._fyers.subscribe(
                    symbols=symbols,
                    data_type="SymbolUpdate",
                )
            else:
                logger.warning(
                    "FyersFeed: No user symbols — subscribing %s as keepalive",
                    KEEPALIVE_SYMBOL,
                )
                self._fyers.subscribe(
                    symbols=[KEEPALIVE_SYMBOL],
                    data_type="SymbolUpdate",
                )

            _heartbeat_stop.clear()
            hb_thread = threading.Thread(target=_heartbeat_loop, daemon=True)
            hb_thread.start()

        try:
            if not _is_market_open():
                logger.warning(
                    "FyersFeed: Market is currently closed — not connecting."
                )
                with self._lock:
                    self._started = False
                return

            self._fyers = data_ws.FyersDataSocket(
                access_token=token,
                log_path="",
                litemode=False,
                write_to_file=False,
                reconnect=True,
                on_connect=onopen,
                on_close=onclose,
                on_error=onerror,
                on_message=onmessage,
            )
            self._fyers.connect()

        except Exception as e:
            logger.error("FyersFeed: connect() failed: %s", e)
            with self._lock:
                self._started = False
                self._connected = False
            if not _is_market_open():
                logger.info("FyersFeed: Connection failed — market is closed")

    # ── Subscribe / Unsubscribe ───────────────────────────────
    def subscribe(self, symbol: str):
        try:
            from apps.market.delta_service import is_crypto_symbol
            if is_crypto_symbol(symbol):
                logger.info("FyersFeed: Skipping crypto: %s", symbol)
                return
        except ImportError:
            if "-USDT" in symbol.upper() or "-USD" in symbol.upper():
                logger.info("FyersFeed: Skipping crypto: %s", symbol)
                return

        fyers_symbol = _to_fyers_symbol(symbol)

        with self._lock:
            if fyers_symbol in self._subscribed:
                return
            self._subscribed.add(fyers_symbol)
            is_connected = self._connected

        logger.info("FyersFeed: Subscribing %s (connected=%s)", fyers_symbol, is_connected)

        if not self._started:
            self.start()

        if is_connected and self._fyers is not None:
            try:
                self._fyers.subscribe(
                    symbols=[fyers_symbol],
                    data_type="SymbolUpdate",
                )
                logger.info("FyersFeed: Live subscribe sent for %s", fyers_symbol)
            except Exception as e:
                logger.error("FyersFeed subscribe error: %s", e)
        else:
            logger.info("FyersFeed: %s queued — will subscribe on connect", fyers_symbol)

    def unsubscribe(self, symbol: str):
        fyers_symbol = _to_fyers_symbol(symbol)
        with self._lock:
            self._subscribed.discard(fyers_symbol)
        if self._fyers is not None:
            try:
                self._fyers.unsubscribe(symbols=[fyers_symbol])
            except Exception as e:
                logger.error("FyersFeed unsubscribe error: %s", e)

    def subscribe_many(self, symbols: list[str]):
        for symbol in symbols:
            self.subscribe(symbol)

    # ── Message handler ───────────────────────────────────────
    def _on_fyers_message(self, msg):
        try:
            if isinstance(msg, str):
                try:
                    msg = json.loads(msg)
                except json.JSONDecodeError:
                    logger.warning("FyersFeed: Non-JSON string: %s", msg[:200])
                    return

            if not isinstance(msg, dict):
                logger.warning("FyersFeed: Unexpected type: %s", type(msg).__name__)
                return

            msg_type = msg.get("type", "")
            if msg_type in ("cn", "sub", "ful", "error", "pong"):
                return

            symbol_raw = msg.get("symbol", "")
            if not symbol_raw:
                return

            ltp = float(msg.get("lp") or msg.get("ltp") or 0)
            if ltp <= 0:
                logger.debug(
                    "FyersFeed: Zero ltp for %s | keys=%s", symbol_raw, list(msg.keys())
                )
                return

            normalized = {
                "symbol":    symbol_raw,
                "ltp":       ltp,
                "change":    float(msg.get("ch", 0) or 0),
                "changePct": float(msg.get("chp", 0) or 0),
                "open":      float(msg.get("open_price", 0) or msg.get("o", 0) or 0),
                "high":      float(msg.get("high_price", 0) or msg.get("h", 0) or 0),
                "low":       float(msg.get("low_price", 0) or msg.get("l", 0) or 0),
                "prevClose": float(msg.get("prev_close_price", 0) or msg.get("pc", 0) or 0),
                "volume":    float(msg.get("volume", 0) or msg.get("v", 0) or 0),
                "bid":       float(msg.get("bid", 0) or msg.get("bp1", 0) or 0),
                "ask":       float(msg.get("ask", 0) or msg.get("sp1", 0) or 0),
                "ts":        int(msg.get("exch_feed_time", 0) or 0),
            }

            logger.info("FyersFeed TICK ✅ | symbol=%s | ltp=%.2f", symbol_raw, ltp)

            group_name = _sanitize(symbol_raw)
            self._broadcast(group_name, "market_update", normalized)
            # ✅ FIX: market group throttle — 1s per symbol interval
            # Prevents "X of Y channels over capacity in group market" warnings
            # Per-symbol groups (symbol_*) still get every tick for precision
            now = time.time()
            last = self._market_last_broadcast.get(symbol_raw, 0)
            if now - last >= self._market_broadcast_interval:
                self._broadcast("market", "market_update", normalized)
                self._market_last_broadcast[symbol_raw] = now
            self._update_asset_price(symbol_raw, ltp)
            self._check_sl_tp(symbol_raw, ltp)
            self._publish_to_redis(normalized)

        except Exception as e:
            logger.error(
                "FyersFeed _on_fyers_message CRASHED: %s | msg=%s",
                e, str(msg)[:300],
                exc_info=True,
            )

    # ── DB price update ───────────────────────────────────────
    def _update_asset_price(self, fyers_symbol: str, ltp: float):
        try:
            from decimal import Decimal
            from apps.market.models import Asset
            from apps.orders.models import Order

            ltp_dec = Decimal(str(ltp))

            sym_upper = fyers_symbol.upper()
            if sym_upper.endswith("CE") or sym_upper.endswith("PE"):
                asset_type = Asset.AssetType.OPTIONS
            elif "FUT" in sym_upper:
                asset_type = Asset.AssetType.FUTURES
            elif "-EQ" in sym_upper:
                asset_type = Asset.AssetType.EQUITY
            else:
                asset_type = Asset.AssetType.EQUITY

            if fyers_symbol.startswith("NSE:"):
                exchange = "NSE"
            elif fyers_symbol.startswith("BSE:"):
                exchange = "BSE"
            elif fyers_symbol.startswith("MCX:"):
                exchange = "MCX"
            else:
                exchange = ""

            asset, created = Asset.objects.get_or_create(
                symbol=fyers_symbol,
                defaults={
                    "name":       fyers_symbol,
                    "exchange":   exchange,
                    "asset_type": asset_type,
                    "is_active":  True,
                    "last_price": ltp_dec,
                },
            )
            if not created:
                Asset.objects.filter(pk=asset.pk).update(last_price=ltp_dec)

            symbol_suffix = fyers_symbol.split(":")[-1]
            Order.objects.filter(
                mode=Order.Mode.PAPER,
                status=Order.Status.OPEN,
                symbol_display=symbol_suffix,
            ).update(current_price=ltp_dec)

        except Exception as e:
            logger.error("FyersFeed: DB price update failed: %s", e)

    # ── Broadcast (channel layer) ─────────────────────────────
    def _broadcast(self, group: str, msg_type: str, data: dict):
        try:
            layer = self._get_channel_layer()
            if not layer:
                return
            import asyncio

            # ✅ FIX: interpreter shutdown error avoid karne ke liye
            # async_to_sync() background thread mein fail hota hai
            # Solution: existing loop use karo, ya new loop mein run karo
            try:
                loop = asyncio.get_event_loop()
                if loop.is_closed():
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

            if loop.is_running():
                # Daphne/ASGI loop already running — thread-safe submit
                asyncio.run_coroutine_threadsafe(
                    layer.group_send(group, {"type": msg_type, "data": data}),
                    loop,
                )
            else:
                loop.run_until_complete(
                    layer.group_send(group, {"type": msg_type, "data": data})
                )
        except (RuntimeError, Exception) as e:
            err_str = str(e)
            # Shutdown-related errors — silently ignore
            if any(x in err_str for x in [
                "interpreter shutdown",
                "cannot schedule",
                "Event loop is closed",
                "no running event loop",
            ]):
                return  # Silent — normal during restart
            logger.error("FyersFeed broadcast error [%s]: %s", group, e)

    # ── Redis publish ─────────────────────────────────────────
    def _publish_to_redis(self, normalized: dict):
        try:
            tick = {**normalized, "broker": "fyers"}
            payload = json.dumps(tick)
            r = _get_redis_client()
            pipe = r.pipeline()
            pipe.publish("ticks:normalized", payload)
            pipe.hset("tick_snapshot", normalized.get("symbol", ""), payload)
            pipe.expire("tick_snapshot", 7200)
            pipe.execute()
        except Exception as e:
            logger.error("FyersFeed: Redis publish error: %s", e)

    # ── SL/TP check ───────────────────────────────────────────
    def _check_sl_tp(self, fyers_symbol: str, ltp: float):
        try:
            from decimal import Decimal
            from apps.orders.models import Order
            from apps.paper_trading.services import close_trade

            open_trades = Order.objects.filter(
                mode=Order.Mode.PAPER,
                status=Order.Status.OPEN,
            ).select_related("user")

            for trade in open_trades:
                trade_sym = trade.symbol_display or ""
                if not (
                    trade_sym == fyers_symbol
                    or trade_sym in fyers_symbol
                    or fyers_symbol.endswith(trade_sym)
                ):
                    continue

                price = Decimal(str(ltp))
                sl    = trade.sl_price
                tp    = trade.target_price
                side  = trade.side

                hit_sl = False
                hit_tp = False

                if side in ('buy', 'long'):
                    if sl and price <= sl:
                        hit_sl = True
                    elif tp and price >= tp:
                        hit_tp = True
                else:
                    if sl and price >= sl:
                        hit_sl = True
                    elif tp and price <= tp:
                        hit_tp = True

                if hit_sl or hit_tp:
                    reason     = "sl_hit" if hit_sl else "tp_hit"
                    exit_price = sl if hit_sl else tp
                    try:
                        close_trade(
                            trade_id=str(trade.id),
                            exit_price=exit_price,
                            reason=reason,
                        )
                        logger.info(
                            "SL/TP hit | trade=%s | symbol=%s | reason=%s | exit=%.2f",
                            trade.id, fyers_symbol, reason, float(exit_price),
                        )
                    except Exception as e:
                        logger.error("close_trade failed | trade=%s | %s", trade.id, e)

        except Exception as e:
            logger.error("_check_sl_tp crashed: %s", e)

    # ── Status ────────────────────────────────────────────────
    def status(self) -> dict:
        fyers_is_connected = getattr(self._fyers, 'is_connected', None)
        return {
            "started":           self._started,
            "connected":         self._connected,
            "fyers_alive":       self._fyers is not None,
            "fyers_is_connected": fyers_is_connected,
            "subscribed":        list(self._subscribed),
            "sub_count":         len(self._subscribed),
            "token_set":         bool(self._current_token),
            "heartbeat_running": (
                self._heartbeat_stop is not None
                and not self._heartbeat_stop.is_set()
            ),
            "market_open": _is_market_open(),
        }

    def stop(self):
        if self._heartbeat_stop is not None:
            self._heartbeat_stop.set()
        if self._fyers is not None:
            try:
                self._fyers.close_connection()
            except Exception:
                pass
        with self._lock:
            self._started = False
            self._connected = False
            self._fyers = None


# ── Global singleton ──────────────────────────────────────────
feed_manager = FyersFeedManager()


# ── Redis feed:subscribe listener (sirf web/daphne process mein) ──
def _start_feed_subscribe_listener():
    """
    Redis 'feed:subscribe' channel listen karo.
    Celery workers yahan publish karte hain — hum yahan subscribe karte hain.
    Sirf web process mein start hona chahiye (DJANGO_SETTINGS_MODULE check).
    """
    import os
    # Celery workers mein ye thread start mat karo
    if os.environ.get('CELERY_WORKER_RUNNING'):
        return

    def _listener():
        import redis as redis_lib
        from django.conf import settings as _settings
        logger.info("FyersFeed: feed:subscribe Redis listener started ✅")
        while True:
            try:
                r = redis_lib.from_url(_settings.REDIS_URL, decode_responses=True)
                pubsub = r.pubsub()
                pubsub.subscribe("feed:subscribe", "feed:restart_token")
                for message in pubsub.listen():
                    if message["type"] != "message":
                        continue
                    try:
                        data = json.loads(message["data"])
                        channel = message.get("channel", "feed:subscribe")
                        if channel == "feed:restart_token":
                            token = data.get("token")
                            if token:
                                logger.info("FyersFeed: Redis restart_token request received")
                                feed_manager.restart_with_new_token(token)
                        else:
                            symbols = data.get("symbols", [])
                            if symbols:
                                logger.info(
                                    "FyersFeed: Redis subscribe request | symbols=%s", symbols
                                )
                                feed_manager.subscribe_many(symbols)
                    except Exception as e:
                        logger.error("FyersFeed: feed:subscribe parse error: %s", e)
            except Exception as e:
                logger.error("FyersFeed: feed:subscribe listener crashed: %s — retrying in 5s", e)
                time.sleep(5)

    t = threading.Thread(target=_listener, daemon=True)
    t.start()


# ── Emergency token refresh helper ────────────────────────────
def _trigger_emergency_token_refresh():
    try:
        from apps.brokers.tasks import auto_refresh_fyers_tokens
        auto_refresh_fyers_tokens.apply_async(queue="default")
        logger.info("FyersFeed: Emergency token refresh task queued")
    except Exception as e:
        logger.error("FyersFeed: Emergency refresh failed: %s", e)