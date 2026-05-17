# broker_adapters/zerodha/adapter.py
#
# Zerodha uses KiteConnect library.
# pip install kiteconnect

from typing import Dict, List
from broker_adapters.base import (
    BaseBrokerAdapter,
    OrderResult,
    PositionInfo,
    FundsInfo,
    CandleBar,
)
from broker_adapters.registry import BrokerRegistry


@BrokerRegistry.register
class ZerodhaAdapter(BaseBrokerAdapter):
    BROKER_SLUG = "zerodha"
    BROKER_NAME = "Zerodha"

    SUPPORTS_OPTIONS = True
    SUPPORTS_FUTURES = True
    SUPPORTS_EQUITY = True
    SUPPORTS_CRYPTO = False

    REQUIRED_CREDENTIAL_FIELDS = ["api_key", "access_token"]

    def __init__(self, credentials: Dict):
        super().__init__(credentials)
        try:
            from kiteconnect import KiteConnect

            self._kite = KiteConnect(api_key=credentials["api_key"])
            self._kite.set_access_token(credentials["access_token"])
        except ImportError:
            raise ImportError("kiteconnect not installed. pip install kiteconnect")

    # ── BaseBrokerAdapter implementation ────────────────────

    def verify_connection(self) -> Dict:
        try:
            profile = self._kite.profile()
            return {
                "success": True,
                "message": "Connected",
                "profile": {
                    "name": profile.get("user_name", ""),
                    "user_id": profile.get("user_id", ""),
                    "email": profile.get("email", ""),
                    "broker": profile.get("broker", ""),
                },
            }
        except Exception as e:
            return {"success": False, "message": str(e)}

    def get_funds(self) -> FundsInfo:
        margins = self._kite.margins()
        equity = margins.get("equity", {})
        net = equity.get("net", {})
        return FundsInfo(
            available=float(net.get("available", {}).get("cash", 0)),
            used=float(net.get("utilised", {}).get("debits", 0)),
            total=float(net.get("net", 0)),
            currency="INR",
            raw=equity,
        )

    def get_positions(self) -> List[PositionInfo]:
        positions = self._kite.positions()
        result = []
        for p in positions.get("net", []):
            qty = int(p.get("quantity", 0))
            if qty == 0:
                continue
            result.append(
                PositionInfo(
                    symbol=p.get("tradingsymbol", ""),
                    side="long" if qty > 0 else "short",
                    qty=abs(qty),
                    entry_price=float(p.get("average_price", 0)),
                    current_price=float(p.get("last_price", 0)),
                    pnl=float(p.get("pnl", 0)),
                    raw=p,
                )
            )
        return result

    def get_orders(self, status: str = "all") -> List[Dict]:
        orders = self._kite.orders()
        if status == "open":
            return [o for o in orders if o.get("status") == "OPEN"]
        if status == "closed":
            return [o for o in orders if o.get("status") in ("COMPLETE", "CANCELLED")]
        return orders

    def place_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        order_type: str = "market",
        price: float = 0,
        **kwargs,
    ) -> OrderResult:
        try:
            from kiteconnect import KiteConnect

            kite_order_type = (
                KiteConnect.ORDER_TYPE_MARKET
                if order_type == "market"
                else KiteConnect.ORDER_TYPE_LIMIT
            )
            kite_side = (
                KiteConnect.TRANSACTION_TYPE_BUY
                if side == "buy"
                else KiteConnect.TRANSACTION_TYPE_SELL
            )

            order_id = self._kite.place_order(
                variety=kwargs.get("variety", KiteConnect.VARIETY_REGULAR),
                exchange=kwargs.get("exchange", KiteConnect.EXCHANGE_NSE),
                tradingsymbol=symbol,
                transaction_type=kite_side,
                quantity=int(qty),
                product=kwargs.get("product", KiteConnect.PRODUCT_MIS),
                order_type=kite_order_type,
                price=price if order_type == "limit" else None,
            )
            return OrderResult(
                success=True, order_id=str(order_id), message="Order placed"
            )
        except Exception as e:
            return OrderResult(success=False, message=str(e))

    def cancel_order(self, order_id: str) -> OrderResult:
        try:
            from kiteconnect import KiteConnect

            self._kite.cancel_order(
                variety=KiteConnect.VARIETY_REGULAR,
                order_id=order_id,
            )
            return OrderResult(success=True, order_id=order_id, message="Cancelled")
        except Exception as e:
            return OrderResult(success=False, message=str(e))

    def get_quote(self, symbol: str) -> Dict:
        try:
            quotes = self._kite.quote([symbol])
            data = quotes.get(symbol, {})
            return {
                "ltp": float(data.get("last_price", 0)),
                "bid": float(data.get("depth", {}).get("buy", [{}])[0].get("price", 0)),
                "ask": float(
                    data.get("depth", {}).get("sell", [{}])[0].get("price", 0)
                ),
                "volume": int(data.get("volume", 0)),
                "change": float(data.get("net_change", 0)),
                "raw": data,
            }
        except Exception as e:
            return {"ltp": 0, "error": str(e)}

    def get_candles(
        self,
        symbol: str,
        resolution: str,
        from_ts: int,
        to_ts: int,
    ) -> List[CandleBar]:
        from datetime import datetime

        _interval_map = {
            "1m": "minute",
            "3m": "3minute",
            "5m": "5minute",
            "10m": "10minute",
            "15m": "15minute",
            "30m": "30minute",
            "1h": "60minute",
            "1d": "day",
        }
        interval = _interval_map.get(resolution, "15minute")
        from_dt = datetime.fromtimestamp(from_ts)
        to_dt = datetime.fromtimestamp(to_ts)
        try:
            from kiteconnect import KiteConnect

            # Need instrument_token — caller should pass via kwargs
            # For now use symbol as token (will need lookup in production)
            records = self._kite.historical_data(
                instrument_token=symbol,
                from_date=from_dt,
                to_date=to_dt,
                interval=interval,
            )
            return [
                CandleBar(
                    timestamp=int(r["date"].timestamp()),
                    open=float(r["open"]),
                    high=float(r["high"]),
                    low=float(r["low"]),
                    close=float(r["close"]),
                    volume=float(r["volume"]),
                )
                for r in records
            ]
        except Exception:
            return []
