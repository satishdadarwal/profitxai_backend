# apps/websocket/push.py
#
#  Celery tasks ya Django views se WebSocket events push karne ke liye.
#  Sync code (Celery) se async channel layer call karna ho toh
#  async_to_sync wrapper zaroori hai.
#
#  Usage example (Celery task):
#
#    from apps.websocket.push import push_trade_update, push_market_update
#
#    push_trade_update(user_id=42, data={
#        "trade_id": 101, "symbol": "ETH", "side": "buy",
#        "price": 3400.0, "amount": 0.5, "status": "filled"
#    })
#
#    push_market_update(data={"symbol": "BTC", "price": 65000, "ts": 1710000000})

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer


# ─────────────────────────────────────────────────────────────────
#  Internal helper
# ─────────────────────────────────────────────────────────────────
def _send(group: str, message: dict):
    """Channel layer pe group_send karta hai — sync wrapper."""
    layer = get_channel_layer()
    async_to_sync(layer.group_send)(group, message)


# ─────────────────────────────────────────────────────────────────
#  Market pushes  (sabhi subscribers ko)
# ─────────────────────────────────────────────────────────────────
def push_market_update(data: dict):
    """
    Live price tick sabhi MarketConsumer clients ko bhejo.
    data = { "symbol": "BTC", "price": 65000.0, "ts": 1710000000 }
    """
    _send("market", {"type": "market_update", "data": data})


def push_orderbook_update(data: dict):
    """
    Top-of-book snapshot sabhi MarketConsumer clients ko.
    data = { "symbol": "BTC", "bids": [[64900, 1.2]], "asks": [[65100, 0.8]] }
    """
    _send("market", {"type": "orderbook_update", "data": data})


def push_symbol_update(symbol: str, data: dict):
    """
    Sirf us symbol ke subscribers ko update bhejo.
    """
    _send(f"symbol_{symbol.upper()}", {"type": "symbol_update", "data": data})


# ─────────────────────────────────────────────────────────────────
#  User-specific pushes  (sirf us user ke TradeConsumer ko)
# ─────────────────────────────────────────────────────────────────
def push_trade_update(user_id: int, data: dict):
    """
    Trade execution result ek user ko bhejo.
    data = { "trade_id": 101, "symbol": "ETH", "side": "buy",
             "price": 3400.0, "amount": 0.5, "status": "filled" }
    """
    _send(f"user_{user_id}", {"type": "trade_update", "data": data})


def push_order_update(user_id: int, data: dict):
    """
    Order status change ek user ko bhejo.
    data = { "order_id": 55, "status": "cancelled", "reason": "insufficient_funds" }
    """
    _send(f"user_{user_id}", {"type": "order_update", "data": data})


def push_balance_update(user_id: int, data: dict):
    """
    Wallet balance change ek user ko bhejo.
    data = { "currency": "USDT", "available": 9820.50, "locked": 179.50 }
    """
    _send(f"user_{user_id}", {"type": "balance_update", "data": data})


def push_notification(user_id: int, level: str, title: str, body: str):
    """
    In-app notification ek user ko bhejo.
    level = "info" | "warning" | "error"
    """
    _send(
        f"user_{user_id}",
        {
            "type": "notification",
            "data": {"level": level, "title": title, "body": body},
        },
    )


def push_pnl_update(user_id: int, data: dict):
    """
    Real-time unrealized PnL update ek user ko bhejo.

    Har price tick pe open positions ka PnL recalculate hokar
    Flutter ko push hota hai.

    data = {
        "positions": [
            {
                "position_id": "uuid",
                "symbol":        "NIFTY",
                "side":          "buy",
                "quantity":      50.0,
                "entry_price":   22450.0,
                "current_price": 22510.0,
                "unrealized_pnl": 3000.0,   # (current - entry) * qty
                "pnl_pct":       0.27,       # percent
                "stop_loss":     22200.0,
                "take_profit":   22700.0,
            }
        ],
        "total_unrealized_pnl": 3000.0,
        "ts": 1710000000,                    # Unix timestamp
    }
    """
    _send(f"user_{user_id}", {"type": "pnl_update", "data": data})