# apps/brokers/feed_manager.py
#
# CHANGES:
# ✅ on_price_tick() mein _update_position_pnl() add kiya
#    → Har Fyers/Delta tick pe open positions ka unrealized PnL
#      recalculate hota hai aur user ko WebSocket push hota hai.
# ✅ _update_ltp_cache() — RiskManager price freshness check ke liye
# ✅ Throttle: per-user 1s interval — Flutter pe flood nahi hoga

import logging
import time
from decimal import Decimal

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

logger = logging.getLogger(__name__)

_feeds: dict = {}
_user_symbols: dict = {}   # user_id → set of symbols
_last_pnl_push: dict = {}  # user_id → last push Unix timestamp
PNL_PUSH_INTERVAL = 1.0    # seconds — throttle per user


def _sanitize(symbol: str) -> str:
    safe = symbol.replace(":", "_").replace("-", "_").replace(" ", "_")
    return f"symbol_{safe}"


def on_price_tick(symbol: str, ltp: float, extra_data: dict = None):
    """
    Fyers/Delta tick callback.
    1. Market broadcast  — sabhi clients ko price update.
    2. LTP cache update  — RiskManager staleness check ke liye.
    3. Position PnL push — open positions ka unrealized PnL Flutter ko.
    """
    try:
        channel_layer = get_channel_layer()
        if not channel_layer:
            return

        payload = dict(extra_data or {})
        payload.update({
            "type":      "market_update",
            "symbol":    symbol,
            "ltp":       ltp,
            "change":    payload.get("change", 0),
            "changePct": payload.get("changePct", 0),
            "open":      payload.get("open", 0),
            "high":      payload.get("high", 0),
            "low":       payload.get("low", 0),
            "prevClose": payload.get("prevClose", 0),
            "bid":       payload.get("bid", 0),
            "ask":       payload.get("ask", 0),
            "volume":    payload.get("volume", 0),
        })

        async_to_sync(channel_layer.group_send)(
            "market", {"type": "market_update", "data": payload}
        )

        _update_ltp_cache(symbol, ltp)
        _update_position_pnl(symbol, ltp)

    except Exception as e:
        logger.error("on_price_tick error | symbol=%s | %s", symbol, e)


def _update_ltp_cache(symbol: str, ltp: float):
    """Redis mein LTP + timestamp — RiskManager._check_price_freshness() ke liye."""
    try:
        from django.core.cache import cache
        cache.set(f"ltp_ts:{symbol}", time.time(), timeout=60)
        cache.set(f"ltp:{symbol}", ltp, timeout=60)
    except Exception as e:
        logger.debug("_update_ltp_cache error | symbol=%s | %s", symbol, e)


def _update_position_pnl(symbol: str, ltp: float):
    """
    Is symbol ki open positions ka unrealized PnL recalculate karo.

    Steps:
      1. DB se OPEN positions fetch (asset__symbol match).
      2. Position.current_price + unrealized_pnl DB mein save karo.
      3. Throttle (1s/user) — WS push karo.

    DB save hamesha hota hai; WS push throttled hai taaki Flutter
    pe tick flood na ho.
    """
    try:
        from apps.orders.models import Position
        from apps.websocket.push import push_pnl_update

        ltp_decimal = Decimal(str(ltp))

        open_positions = list(
            Position.objects.filter(
                status=Position.Status.OPEN,
                asset__symbol=symbol,
            ).select_related("user", "asset")
        )

        if not open_positions:
            return

        by_user: dict = {}
        for pos in open_positions:
            by_user.setdefault(pos.user_id, []).append(pos)

        now = time.time()

        for user_id, positions in by_user.items():
            pos_payloads = []
            total_unrealized = Decimal("0")

            for pos in positions:
                unrealized = pos.calculate_pnl(ltp_decimal)
                total_unrealized += unrealized

                try:
                    pos.current_price  = ltp_decimal
                    pos.unrealized_pnl = unrealized
                    pos.save(update_fields=["current_price", "unrealized_pnl", "updated_at"])
                except Exception as save_err:
                    logger.error(
                        "_update_position_pnl: save failed | pos=%s | %s",
                        pos.id, save_err,
                    )
                    continue

                try:
                    pnl_pct = round(float(pos.pnl_percentage), 2)
                except Exception:
                    pnl_pct = 0.0

                pos_payloads.append({
                    "position_id":    str(pos.id),
                    "symbol":         symbol,
                    "side":           pos.side,
                    "quantity":       float(pos.remaining_qty),
                    "entry_price":    float(pos.avg_entry_price),
                    "current_price":  float(ltp_decimal),
                    "unrealized_pnl": float(unrealized),
                    "pnl_pct":        pnl_pct,
                    "stop_loss":      float(pos.stop_loss) if pos.stop_loss else None,
                    "take_profit":    float(pos.take_profit) if pos.take_profit else None,
                    "mode":           pos.mode,
                })

            if not pos_payloads:
                continue

            # Throttle check
            if now - _last_pnl_push.get(user_id, 0) < PNL_PUSH_INTERVAL:
                continue

            _last_pnl_push[user_id] = now

            push_pnl_update(
                user_id=user_id,
                data={
                    "positions":            pos_payloads,
                    "total_unrealized_pnl": float(total_unrealized),
                    "ts":                   int(now),
                },
            )

            logger.debug(
                "pnl_update pushed | user=%s | symbol=%s | count=%d | total=%.2f",
                user_id, symbol, len(pos_payloads), float(total_unrealized),
            )

    except Exception as e:
        logger.error("_update_position_pnl error | symbol=%s | %s", symbol, e)


# ─────────────────────────────────────────────────────────────────
#  Feed lifecycle
# ─────────────────────────────────────────────────────────────────

def start_feed_for_account(broker_account_id: int):
    if broker_account_id in _feeds:
        return
    try:
        from apps.brokers.models import BrokerAccount
        from apps.websocket.fyers_feed import feed_manager as fyers_feed_manager

        BrokerAccount.objects.get(id=broker_account_id, broker="fyers", is_active=True)

        for sym in [
            "NSE:NIFTY50-INDEX", "NSE:NIFTYBANK-INDEX",
            "NSE:FINNIFTY-INDEX", "NSE:MIDCPNIFTY-INDEX", "BSE:SENSEX-INDEX",
        ]:
            fyers_feed_manager.subscribe(sym)

        _feeds[broker_account_id] = fyers_feed_manager
        logger.info("Feed started | account=%s", broker_account_id)
    except Exception as e:
        logger.error("start_feed_for_account error: %s", e)


def stop_feed_for_account(broker_account_id: int):
    feed = _feeds.pop(broker_account_id, None)
    if feed:
        feed.stop()
        logger.info("Feed stopped | account=%s", broker_account_id)


def subscribe_symbol(broker_account_id: int, symbol: str):
    feed = _feeds.get(broker_account_id)
    if not feed:
        start_feed_for_account(broker_account_id)
        feed = _feeds.get(broker_account_id)
        if not feed:
            return
    feed.subscribe(symbol)
    try:
        from apps.brokers.models import BrokerAccount
        account = BrokerAccount.objects.get(id=broker_account_id)
        _user_symbols.setdefault(account.user.id, set()).add(symbol)
    except Exception:
        pass
    logger.info("Symbol subscribed | account=%s | symbol=%s", broker_account_id, symbol)


def unsubscribe_symbol(broker_account_id: int, symbol: str):
    feed = _feeds.get(broker_account_id)
    if feed:
        try:
            feed.unsubscribe(symbol)
        except Exception as e:
            logger.warning("Unsubscribe failed | symbol=%s | %s", symbol, e)
    try:
        from apps.brokers.models import BrokerAccount
        account = BrokerAccount.objects.get(id=broker_account_id)
        _user_symbols.get(account.user.id, set()).discard(symbol)
    except Exception:
        pass
    logger.info("Symbol unsubscribed | account=%s | symbol=%s", broker_account_id, symbol)


def get_subscribed_symbols(user_id: int) -> list:
    return list(_user_symbols.get(user_id, set()))