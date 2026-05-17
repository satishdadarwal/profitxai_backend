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
MARKET_OPEN_TIME = dt_time(9, 15)   # 9:15 AM
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
    
    # Weekend check
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

    # ── Channel layer ─────────────────────────────────────────
    def _get_channel_layer(self):
        if self._channel_layer is None:
            self._channel_layer = get_channel_layer()
        return self._channel_layer

    # ── Fyers token (DB se latest) ────────────────────────────
    def _get_access_token(self) -> str | None:
        try:
            from apps.brokers.models import BrokerAccount
            account = (
                BrokerAccount.objects
                .filter(broker="fyers", is_active=True)
                .exclude(access_token__isnull=True)
                .exclude(access_token="")
                .order_by("-updated_at")
                .first()
            )
            if not account:
                logger.error("FyersFeed: No active Fyers account with token found")
                return None
            token = f"{account.app_id}:{account.access_token}"
            logger.info(
                "FyersFeed: Token loaded | account=%s | user=%s | is_verified=%s",
                account.id, account.user_id, account.is_verified,
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
                from apps.paper_trading.models import PaperTrade
                open_symbols = PaperTrade.objects.filter(
                    status='open'
                ).values_list('symbol', flat=True).distinct()
                for sym in open_symbols:
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
        """
        Log se confirm hua ki SDK mein ye methods/attrs hain:
          - _FyersDataSocket__ping   (SDK ka internal ping method)
          - _FyersDataSocket__ws_object  (WebSocketApp — connect pe set hota hai)
          - is_connected  (public bool)
          - ping_thread   (SDK ka apna ping thread)

        Strategy:
          1. SDK ka __ping() directly call karo — safest
          2. Fallback: __ws_object.sock pe binary frame bhejo
        """
        fyers_obj = self._fyers
        if fyers_obj is None:
            return False

        # ✅ SDK is_connected() sirf auth complete pe True hota hai.
        # Apna self._connected flag use karo — onopen() pe set hota hai.
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
                else:
                    logger.debug(
                        "FyersFeed: Heartbeat — ws_object exists but sock not connected"
                    )
            else:
                logger.debug("FyersFeed: Heartbeat — __ws_object is None (connecting...)")
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

            # SDK ko is_connected=True karne do — pehle 3 sec wait
            _heartbeat_stop.wait(timeout=3)
            if not _heartbeat_stop.is_set():
                result = self._send_ping()
                logger.info(
                    "FyersFeed: First ping result=%s | self._connected=%s",
                    result,
                    self._connected,
                )

            # Har 12 sec pe ping (Fyers 19 sec mein drop karta hai)
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
                # -99 = Token expired, -300 = Token invalid
                if code in (-99, -300):
                    logger.warning(
                        "FyersFeed: Token error (code=%s) — triggering emergency refresh",
                        code,
                    )
                    _trigger_emergency_token_refresh()
                    return

            # ✅ Market closed check — WinError 10060 expected after 3:30 PM
            if not _is_market_open():
                logger.info("FyersFeed: Market is closed — connection error expected, stopping heartbeat")
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

            # ✅ Market closed check before reconnect
            if not _is_market_open():
                logger.info(
                    "FyersFeed: Market closed — will not attempt reconnect until market opens"
                )
                self._reconnect_attempts = 0
                return

            def _reconnect():
                # Exponential backoff with limit
                self._reconnect_attempts += 1
                if self._reconnect_attempts > self._max_reconnect_attempts:
                    logger.warning(
                        "FyersFeed: Max reconnect attempts reached (%d) — giving up",
                        self._max_reconnect_attempts
                    )
                    self._reconnect_attempts = 0
                    return
                
                wait_time = min(5 * self._reconnect_attempts, 30)
                logger.info(
                    "FyersFeed: Reconnect attempt %d/%d — waiting %ds",
                    self._reconnect_attempts, 
                    self._max_reconnect_attempts,
                    wait_time
                )
                time.sleep(wait_time)
                
                fresh_token = self._get_access_token() or self._current_token
                if fresh_token:
                    self.start(token=fresh_token)
                else:
                    logger.error("FyersFeed: No token available — reconnect aborted")

            threading.Thread(target=_reconnect, daemon=True).start()

        def onopen():
            logger.info("FyersFeed: Fyers WS connected ✅")
            self._reconnect_attempts = 0  # Reset on successful connect
            
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

            # Heartbeat start karo
            _heartbeat_stop.clear()
            hb_thread = threading.Thread(target=_heartbeat_loop, daemon=True)
            hb_thread.start()

        try:
            # ✅ Market hours check before attempting connection
            if not _is_market_open():
                logger.warning(
                    "FyersFeed: Market is currently closed (Mon-Fri 9:15-15:30) — "
                    "connection will fail. Marking as started but not attempting connect."
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
            
            # ✅ Check market hours in exception case
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
                "symbol": symbol_raw,
                "ltp": ltp,
                "change": float(msg.get("ch", 0) or 0),
                "changePct": float(msg.get("chp", 0) or 0),
                "open": float(msg.get("open_price", 0) or msg.get("o", 0) or 0),
                "high": float(msg.get("high_price", 0) or msg.get("h", 0) or 0),
                "low": float(msg.get("low_price", 0) or msg.get("l", 0) or 0),
                "prevClose": float(
                    msg.get("prev_close_price", 0) or msg.get("pc", 0) or 0
                ),
                "volume": float(msg.get("volume", 0) or msg.get("v", 0) or 0),
                "bid": float(msg.get("bid", 0) or msg.get("bp1", 0) or 0),
                "ask": float(msg.get("ask", 0) or msg.get("sp1", 0) or 0),
                "ts": int(msg.get("exch_feed_time", 0) or 0),
            }

            logger.info("FyersFeed TICK ✅ | symbol=%s | ltp=%.2f", symbol_raw, ltp)

            group_name = _sanitize(symbol_raw)
            self._broadcast(group_name, "market_update", normalized)
            self._broadcast("market", "market_update", normalized)
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
            from apps.paper_trading.models import PaperTrade

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
                    "name": fyers_symbol,
                    "exchange": exchange,
                    "asset_type": asset_type,
                    "is_active": True,
                    "last_price": ltp_dec,
                }
            )

            if not created:
                Asset.objects.filter(pk=asset.pk).update(last_price=ltp_dec)

            symbol_suffix = fyers_symbol.split(":")[-1]
            PaperTrade.objects.filter(
                status="open",
                symbol=symbol_suffix,
            ).update(current_price=ltp_dec)

        except Exception as e:
            logger.error("FyersFeed: DB price update failed: %s", e)

    # ── Broadcast (channel layer) ─────────────────────────────
    def _broadcast(self, group: str, msg_type: str, data: dict):
        try:
            layer = self._get_channel_layer()
            if not layer:
                return
            async_to_sync(layer.group_send)(
                group,
                {"type": msg_type, "data": data},
            )
        except Exception as e:
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
            from apps.paper_trading.models import PaperTrade
            from apps.paper_trading.services import close_trade

            open_trades = PaperTrade.objects.filter(
                status="open"
            ).select_related("account")

            for trade in open_trades:
                if not (
                    trade.symbol == fyers_symbol
                    or trade.symbol in fyers_symbol
                    or fyers_symbol.endswith(trade.symbol)
                ):
                    continue

                price = Decimal(str(ltp))
                sl    = trade.stop_loss
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
                            trade.id, fyers_symbol, reason, float(exit_price)
                        )
                    except Exception as e:
                        logger.error("close_trade failed | trade=%s | %s", trade.id, e)

        except Exception as e:
            logger.error("_check_sl_tp crashed: %s", e)

    # ── Status ────────────────────────────────────────────────
    def status(self) -> dict:
        fyers_is_connected = getattr(self._fyers, 'is_connected', None)
        return {
            "started": self._started,
            "connected": self._connected,
            "fyers_alive": self._fyers is not None,
            "fyers_is_connected": fyers_is_connected,
            "subscribed": list(self._subscribed),
            "sub_count": len(self._subscribed),
            "token_set": bool(self._current_token),
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


# ── Emergency token refresh helper ────────────────────────────
def _trigger_emergency_token_refresh():
    try:
        from apps.brokers.tasks import auto_refresh_fyers_tokens
        auto_refresh_fyers_tokens.apply_async(queue="default")
        logger.info("FyersFeed: Emergency token refresh task queued")
    except Exception as e:
        logger.error("FyersFeed: Emergency refresh failed: %s", e)