# broker_adapters/fyers/adapter.py

import hashlib
import requests
from typing import Dict, List, Optional
from broker_adapters.base import (
    BaseBrokerAdapter,
    OrderResult,
    PositionInfo,
    FundsInfo,
    CandleBar,
)
from broker_adapters.registry import BrokerRegistry


@BrokerRegistry.register
class FyersAdapter(BaseBrokerAdapter):
    BROKER_SLUG = "fyers"
    BROKER_NAME = "Fyers"

    SUPPORTS_OPTIONS = True
    SUPPORTS_FUTURES = True
    SUPPORTS_EQUITY = True
    SUPPORTS_CRYPTO = False

    REQUIRED_CREDENTIAL_FIELDS = ["app_id", "access_token"]

    BASE_URL = "https://api-t1.fyers.in"
    DATA_URL = "https://api-t1.fyers.in/data"
    AUTH_BASE_URL = "https://api-t2.fyers.in/api/v3"

    # Fyers order status codes
    # 1=Cancelled, 2=Traded, 4=Transit, 5=Rejected, 6=Pending, 20=PendingModification
    _STATUS_MAP = {
        1:  "cancelled",
        2:  "complete",
        4:  "open",       # in transit / partially filled
        5:  "rejected",
        6:  "open",       # pending
        20: "open",       # pending modification
    }

    # ── Internal helpers ────────────────────────────────────

    def _auth_header(self) -> str:
        return f"{self.credentials['app_id']}:{self.credentials['access_token']}"

    def _get(self, url: str, params: dict | None = None) -> dict:
        r = requests.get(
            url,
            headers={
                "Authorization": self._auth_header(),
                "Content-Type": "application/json",
            },
            params=params or {},
            # ✅ FIX: (connect_timeout, read_timeout) tuple
            # timeout=15 (int) sirf READ timeout tha — connect infinite tha.
            # Hung TCP connection = worker OS timeout tak frozen (75-300s on Linux).
            # connect=3s: Fyers server reachable hai ya nahi — jaldi pata chale
            # read=8s:    4 TFs x 8s = 32s worst case, but with Yahoo fallback
            #             budget = (3+8) x 4 TFs = 44s max, task kills at 14s
            #             Realistic: 4 TFs x ~1-2s = 4-8s — fits in 12s soft limit
            timeout=(3, 8),
        )
        r.raise_for_status()
        return r.json()

    def _post(self, url: str, data: dict) -> dict:
        r = requests.post(
            url,
            headers={
                "Authorization": self._auth_header(),
                "Content-Type": "application/json",
            },
            json=data,
            # ✅ FIX: tuple timeout — order/auth calls thoda zyada read time chahiye
            timeout=(3, 10),
        )
        r.raise_for_status()
        return r.json()

    def _delete(self, url: str, data: dict) -> dict:
        r = requests.delete(
            url,
            headers={
                "Authorization": self._auth_header(),
                "Content-Type": "application/json",
            },
            json=data,
            # ✅ FIX: tuple timeout
            timeout=(3, 10),
        )
        r.raise_for_status()
        return r.json()

    # ── BaseBrokerAdapter implementation ────────────────────

    def verify_connection(self) -> Dict:
        try:
            resp = self._get(f"{self.BASE_URL}/api/v3/profile")
            if resp.get("s") == "ok":
                return {
                    "success": True,
                    "message": "Connected",
                    "profile": resp.get("data", {}),
                }
            return {"success": False, "message": resp.get("message", "Failed")}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def get_funds(self) -> FundsInfo:
        resp = self._get(f"{self.BASE_URL}/api/v3/funds")

        # Fyers returns fund_limit as a LIST of {id, title, equityAmount, commodityAmount}
        fund_limit = resp.get("fund_limit", [])

        available = 0.0
        used      = 0.0
        total     = 0.0

        if isinstance(fund_limit, list):
            for item in fund_limit:
                title  = item.get("title", "")
                amount = float(item.get("equityAmount", 0) or 0)
                if title == "Available Balance":
                    available = amount
                elif title == "Utilized Amount":
                    used = amount
                elif title == "Total Balance":
                    total = amount
            if total == 0:
                total = available + used
        elif isinstance(fund_limit, dict):
            available = float(fund_limit.get("equity_amount", 0) or 0)
            used      = float(fund_limit.get("utilized_amount", 0) or 0)
            total     = float(fund_limit.get("total_amount", 0) or 0)

        return FundsInfo(
            available=available,
            used=used,
            total=total,
            currency="INR",
            raw=resp,
        )

    def get_positions(self) -> List[PositionInfo]:
        resp = self._get(f"{self.BASE_URL}/api/v3/positions")
        rows = resp.get("netPositions", [])
        result = []
        for p in rows:
            result.append(
                PositionInfo(
                    symbol=p.get("symbol", ""),
                    side="long" if float(p.get("netQty", 0)) > 0 else "short",
                    qty=abs(float(p.get("netQty", 0))),
                    entry_price=float(p.get("avgPrice", 0)),
                    current_price=float(p.get("ltp", 0)),
                    pnl=float(p.get("pl", 0)),
                    raw=p,
                )
            )
        return result

    def get_orders(self, status: str = "all") -> List[Dict]:
        resp = self._get(f"{self.BASE_URL}/api/v3/orders")
        orders = resp.get("orderBook", [])
        if status == "open":
            orders = [o for o in orders if o.get("status") in (6, "6")]
        elif status == "closed":
            orders = [o for o in orders if o.get("status") not in (6, "6")]
        return orders

    def get_order_status(self, order_id: str) -> Dict:
        """
        Single order ka status fetch karo order_id se.

        Returns:
            {
                "success": bool,
                "order_id": str,
                "status": "open" | "complete" | "cancelled" | "rejected" | "unknown",
                "filled_qty": float,
                "pending_qty": float,
                "price": float,
                "avg_price": float,
                "symbol": str,
                "message": str,       # only on failure
                "raw": dict,
            }
        """
        try:
            resp = self._get(
                f"{self.BASE_URL}/api/v3/orders",
                params={"id": order_id},
            )

            if resp.get("s") != "ok":
                return {
                    "success": False,
                    "order_id": order_id,
                    "status": "unknown",
                    "message": resp.get("message", "API error"),
                    "raw": resp,
                }

            # Fyers returns a list even for a single order query
            order_book = resp.get("orderBook", [])
            order = next(
                (o for o in order_book if str(o.get("id")) == str(order_id)),
                None,
            )

            if order is None:
                # Fallback: try the order history endpoint
                hist = self._get(
                    f"{self.BASE_URL}/api/v3/orders/history",
                    params={"id": order_id},
                )
                history_list = hist.get("orderBook", [])
                order = history_list[0] if history_list else None

            if order is None:
                return {
                    "success": False,
                    "order_id": order_id,
                    "status": "unknown",
                    "message": "Order not found",
                    "raw": {},
                }

            raw_status = order.get("status")
            try:
                raw_status_int = int(raw_status)
            except (TypeError, ValueError):
                raw_status_int = -1

            normalized_status = self._STATUS_MAP.get(raw_status_int, "unknown")

            return {
                "success": True,
                "order_id": order_id,
                "status": normalized_status,
                "filled_qty": float(order.get("filledQty", 0) or 0),
                "pending_qty": float(order.get("remainingQuantity", 0) or 0),
                "price": float(order.get("limitPrice", 0) or 0),
                "avg_price": float(order.get("tradedPrice", 0) or 0),
                "symbol": order.get("symbol", ""),
                "raw": order,
            }

        except Exception as e:
            return {
                "success": False,
                "order_id": order_id,
                "status": "unknown",
                "message": str(e),
                "raw": {},
            }

    def place_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        order_type: str = "market",
        price: float = 0,
        **kwargs,
    ) -> OrderResult:
        payload = {
            "symbol": symbol,
            "qty": int(qty),
            "type": 2 if order_type == "market" else 1,  # Fyers v3: 1=Limit, 2=Market, 3=SL, 4=SL-M
            "side": 1 if side == "buy" else -1,
            "productType": kwargs.get("product_type", "INTRADAY"),
            "limitPrice": price,
            "stopPrice": kwargs.get("stop_price", 0),
            "validity": "DAY",
            "disclosedQty": 0,
            "offlineOrder": False,
        }
        try:
            resp = self._post(f"{self.BASE_URL}/api/v3/orders", payload)
            if resp.get("s") == "ok":
                return OrderResult(
                    success=True,
                    order_id=str(resp.get("id", "")),
                    message="Order placed",
                    raw=resp,
                )
            return OrderResult(
                success=False, message=resp.get("message", "Failed"), raw=resp
            )
        except Exception as e:
            return OrderResult(success=False, message=str(e))

    def cancel_order(self, order_id: str) -> OrderResult:
        try:
            resp = self._delete(
                f"{self.BASE_URL}/api/v3/orders",
                {"id": order_id},
            )
            if resp.get("s") == "ok":
                return OrderResult(
                    success=True, order_id=order_id, message="Cancelled", raw=resp
                )
            return OrderResult(
                success=False, message=resp.get("message", "Failed"), raw=resp
            )
        except Exception as e:
            return OrderResult(success=False, message=str(e))

    def get_quote(self, symbol: str) -> Dict:
        resp = self._get(f"{self.DATA_URL}/quotes", params={"symbols": symbol})
        quotes = resp.get("d", [])
        if quotes:
            q = quotes[0].get("v", {})
            return {
                "ltp": float(q.get("lp", 0)),
                "bid": float(q.get("bp1", 0)),
                "ask": float(q.get("sp1", 0)),
                "volume": int(q.get("vol", 0)),
                "change": float(q.get("chp", 0)),
                "raw": q,
            }
        return {}

    def get_bulk_quotes(self, symbols: List[str]) -> Dict[str, Dict]:
        """
        Multiple symbols ke liye ek hi API call mein quotes fetch karo.

        Args:
            symbols: List of symbols like ['NSE:NIFTY50-INDEX', 'NSE:RELIANCE-EQ']

        Returns:
            Dict mapping symbol -> quote data
        """
        if not symbols:
            return {}

        try:
            symbols_str = ",".join(symbols)
            resp = self._get(f"{self.DATA_URL}/quotes", params={"symbols": symbols_str})

            result = {}
            quotes = resp.get("d", [])

            for item in quotes:
                symbol = item.get("n", "")
                v = item.get("v", {})

                if symbol:
                    result[symbol] = {
                        "symbol": symbol,
                        "ltp": float(v.get("lp", 0)),
                        "bid": float(v.get("bp1", 0)),
                        "ask": float(v.get("sp1", 0)),
                        "volume": int(v.get("vol", 0)),
                        "change": float(v.get("ch", 0)),
                        "change_pct": float(v.get("chp", 0)),
                        "high": float(v.get("high_price", 0)),
                        "low": float(v.get("low_price", 0)),
                        "open": float(v.get("open_price", 0)),
                        "prev_close": float(v.get("prev_close_price", 0)),
                        "raw": v,
                    }

            return result

        except Exception as e:
            import logging
            logging.getLogger(__name__).error(
                f"Bulk quotes failed for {len(symbols)} symbols: {e}"
            )
            return {}

    def get_candles(
        self,
        symbol: str,
        resolution: str,
        from_ts: int,
        to_ts: int,
    ) -> List[CandleBar]:
        from apps.brokers.symbol_mapper import normalize_for_fyers
        from datetime import datetime, timedelta
        import logging
        _log = logging.getLogger(__name__)
        normalized_symbol = normalize_for_fyers(symbol)

        CHUNK_DAYS = 90
        all_candles = []
        chunk_start = datetime.fromtimestamp(from_ts)
        end_dt = datetime.fromtimestamp(to_ts)

        while chunk_start < end_dt:
            chunk_end = min(chunk_start + timedelta(days=CHUNK_DAYS), end_dt)
            from_date = chunk_start.strftime("%Y-%m-%d")
            to_date = chunk_end.strftime("%Y-%m-%d")
            try:
                resp = self._get(
                    f"{self.DATA_URL}/history",
                    params={
                        "symbol": normalized_symbol,
                        "resolution": resolution,
                        "date_format": "1",
                        "range_from": from_date,
                        "range_to": to_date,
                        "cont_flag": "1",
                    },
                )
                candles_raw = resp.get("candles", [])
                all_candles.extend([
                    CandleBar(
                        timestamp=int(row[0]),
                        open=float(row[1]),
                        high=float(row[2]),
                        low=float(row[3]),
                        close=float(row[4]),
                        volume=float(row[5]),
                    )
                    for row in candles_raw
                    if len(row) >= 6
                ])
                _log.info("Fyers chunk | %s | %s to %s | bars=%d", normalized_symbol, from_date, to_date, len(candles_raw))
            except Exception as e:
                _log.warning("Fyers chunk error | %s | %s to %s | %s", symbol, from_date, to_date, e)
            chunk_start = chunk_end + timedelta(days=1)

        seen = set()
        unique = []
        for c in sorted(all_candles, key=lambda x: x.timestamp):
            if c.timestamp not in seen:
                seen.add(c.timestamp)
                unique.append(c)
        _log.info("Fyers candles total | %s | bars=%d", normalized_symbol, len(unique))
        return unique
    def refresh_token(self) -> Dict:
        """Fyers token refresh using refresh_token from credentials."""
        refresh_tok = self.credentials.get("refresh_token", "")
        if not refresh_tok:
            return {"success": False, "message": "No refresh_token in credentials"}
        try:
            app_id = self.credentials["app_id"]
            app_secret = self.credentials.get("app_secret", "")
            app_id_hash = hashlib.sha256(f"{app_id}:{app_secret}".encode()).hexdigest()
            resp = requests.post(
                f"{self.AUTH_BASE_URL}/validate-refresh-token",
                json={
                    "grant_type": "refresh_token",
                    "appIdHash": app_id_hash,
                    "refresh_token": refresh_tok,
                    "pin": self.credentials.get("pin", ""),
                },
                # ✅ FIX: tuple timeout — token rotation ke liye thoda zyada time
                timeout=(3, 12),
            )
            data = resp.json()
            if data.get("s") == "ok":
                return {"success": True, "access_token": data.get("access_token", "")}
            return {"success": False, "message": data.get("message", "Refresh failed")}
        except Exception as e:
            return {"success": False, "message": str(e)}
    def place_gtt(
        self,
        symbol: str,
        qty: int,
        side: str,           # "buy" or "sell"
        trigger_price: float,
        limit_price: float,
        product_type: str = "INTRADAY",
    ) -> dict:
        """
        GTT (Good Till Triggered) order place karo.
        SL ya Target ke liye use karo.
        """
        payload = {
            "symbol": symbol,
            "qty": qty,
            "side": 1 if side == "buy" else -1,
            "type": 1,  # Limit order
            "limitPrice": limit_price,
            "stopPrice": trigger_price,
            "productType": product_type,
            "validity": "GTT",
            "disclosedQty": 0,
            "offlineOrder": False,
        }
        try:
            resp = self._post(f"{self.BASE_URL}/api/v3/orders/gtt", payload)
            return resp
        except Exception as e:
            return {"s": "error", "message": str(e)}

    def place_gtt(
        self,
        symbol: str,
        qty: int,
        side: str,           # "buy" or "sell"
        trigger_price: float,
        limit_price: float,
        product_type: str = "INTRADAY",
    ) -> dict:
        """
        GTT (Good Till Triggered) order place karo.
        SL ya Target ke liye use karo.
        """
        payload = {
            "symbol": symbol,
            "qty": qty,
            "side": 1 if side == "buy" else -1,
            "type": 1,  # Limit order
            "limitPrice": limit_price,
            "stopPrice": trigger_price,
            "productType": product_type,
            "validity": "GTT",
            "disclosedQty": 0,
            "offlineOrder": False,
        }
        try:
            resp = self._post(f"{self.BASE_URL}/api/v3/orders/gtt", payload)
            return resp
        except Exception as e:
            return {"s": "error", "message": str(e)}
