# broker_adapters/delta/adapter.py

import logging
import time
import hmac
import hashlib
import requests
from typing import Dict, List
from broker_adapters.base import (
    BaseBrokerAdapter,
    OrderResult,
    PositionInfo,
    FundsInfo,
    CandleBar,
)
from broker_adapters.registry import BrokerRegistry

logger = logging.getLogger(__name__)


@BrokerRegistry.register
class DeltaAdapter(BaseBrokerAdapter):
    BROKER_SLUG = "delta"
    BROKER_NAME = "Delta India"

    SUPPORTS_OPTIONS = False
    SUPPORTS_FUTURES = True
    SUPPORTS_EQUITY = False
    SUPPORTS_CRYPTO = True

    REQUIRED_CREDENTIAL_FIELDS = ["api_key", "api_secret"]

    _INDIA_MAIN = "https://api.india.delta.exchange"
    _INDIA_TEST = "https://cdn-ind.testnet.deltaex.org"

    SYMBOL_MAP = {
        "BTC-USDT": "BTCUSD",
        "ETH-USDT": "ETHUSD",
        "SOL-USDT": "SOLUSD",
        "BNB-USDT": "BNBUSD",
        "XRP-USDT": "XRPUSD",
        "ADA-USDT": "ADAUSD",
        "DOGE-USDT": "DOGEUSD",
        "AVAX-USDT": "AVAXUSD",
        "LTC-USDT": "LTCUSD",
        "BTC-USD": "BTCUSD",
        "ETH-USD": "ETHUSD",
        "SOL-USD": "SOLUSD",
    }

    # Delta order states -> normalized
    _STATUS_MAP = {
        "open":              "open",
        "pending":           "open",
        "partially_filled":  "open",
        "filled":            "complete",
        "closed":            "complete",
        "cancelled":         "cancelled",
        "rejected":          "rejected",
    }

    def __init__(self, credentials: Dict):
        super().__init__(credentials)
        self._testnet = credentials.get("testnet", False)
        self._base = self._INDIA_TEST if self._testnet else self._INDIA_MAIN
        self._candle_url = self._base

    def _convert_symbol(self, symbol: str) -> str:
        symbol_upper = symbol.upper()
        if symbol_upper in self.SYMBOL_MAP:
            return self.SYMBOL_MAP[symbol_upper]
        if "-USDT" in symbol_upper:
            return symbol_upper.replace("-USDT", "USD")
        if "-USD" in symbol_upper:
            return symbol_upper.replace("-", "")
        return symbol_upper

    def _sign(self, method: str, path: str, body: str = "") -> Dict[str, str]:
        ts = str(int(time.time()))
        payload = method + ts + path + body
        signature = hmac.new(
            self.credentials["api_secret"].encode(),
            payload.encode(),
            hashlib.sha256,
        ).hexdigest()
        return {
            "api-key": self.credentials["api_key"],
            "timestamp": ts,
            "signature": signature,
            "Content-Type": "application/json",
        }

    def _get(self, path: str, base: str = None, params: dict = None) -> dict:
        url = (base or self._base) + path
        # ✅ FIX: (connect_timeout, read_timeout) tuple
        # timeout=15 (int) sirf READ timeout tha — connect infinite tha.
        # Hung TCP = worker OS timeout tak frozen (75-300s on Linux).
        r = requests.get(
            url, headers=self._sign("GET", path), params=params, timeout=(3, 8)
        )
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, data: dict) -> dict:
        import json as _json
        body = _json.dumps(data)
        # ✅ FIX: tuple timeout
        r = requests.post(
            self._base + path,
            headers=self._sign("POST", path, body),
            data=body,
            timeout=(3, 10),
        )
        r.raise_for_status()
        return r.json()

    def _delete(self, path: str, data: dict = None) -> dict:
        import json as _json
        body = _json.dumps(data or {})
        # ✅ FIX: tuple timeout
        r = requests.delete(
            self._base + path,
            headers=self._sign("DELETE", path, body),
            data=body,
            timeout=(3, 10),
        )
        r.raise_for_status()
        return r.json()

    def _d(self, v) -> float:
        try:
            return float(v or 0)
        except Exception:
            return 0.0

    def verify_connection(self) -> Dict:
        try:
            # ✅ FIX: tuple timeout
            ticker = requests.get(f"{self._base}/v2/tickers/BTCUSD", timeout=(3, 8))
            ticker.raise_for_status()
            wallets = self._get("/v2/wallet/balances")
            if wallets.get("success"):
                btc_price = self._d(ticker.json().get("result", {}).get("close"))
                return {"success": True, "message": "Connected", "btc_price": btc_price}
            return {
                "success": False,
                "message": wallets.get("error", {}).get("message", "Auth failed"),
            }
        except Exception as e:
            return {"success": False, "message": str(e)}

    def get_funds(self) -> FundsInfo:
        resp = self._get("/v2/wallet/balances")
        wallets = resp.get("result", [])

        # ✅ FIX: Delta India API INR use karta hai (USDT nahi)
        # Delta Global (non-India) USDT use karta hai.
        # Priority: INR → USDT → USD → pehla wallet jo balance > 0 ho
        _PREFERRED = ("INR", "USDT", "USD")

        wallet = {}
        # 1. Preferred currency order mein dhundho
        for sym in _PREFERRED:
            found = next((w for w in wallets if w.get("asset_symbol") == sym), None)
            if found and (self._d(found.get("balance")) > 0 or self._d(found.get("available_balance")) > 0):
                wallet = found
                currency = sym
                break

        # 2. Fallback: pehla wallet jo balance > 0 ho
        if not wallet:
            for w in wallets:
                if self._d(w.get("balance")) > 0 or self._d(w.get("available_balance")) > 0:
                    wallet = w
                    currency = w.get("asset_symbol", "USDT")
                    break
            else:
                # Koi balance nahi — pehla wallet return karo
                wallet = wallets[0] if wallets else {}
                currency = wallet.get("asset_symbol", "INR")

        logger.info(
            "Delta funds | currency=%s | available=%s | balance=%s",
            currency,
            wallet.get("available_balance"),
            wallet.get("balance"),
        )

        return FundsInfo(
            available=self._d(wallet.get("available_balance")),
            used=self._d(wallet.get("blocked_margin")),
            total=self._d(wallet.get("balance")),
            currency=currency,
            raw=wallet,
        )

    def get_positions(self) -> List[PositionInfo]:
        resp = self._get("/v2/positions/margined")
        rows = resp.get("result", [])
        result = []
        for p in rows:
            size = self._d(p.get("size"))
            if size == 0:
                continue
            result.append(
                PositionInfo(
                    symbol=p.get("product_symbol", ""),
                    side="long" if size > 0 else "short",
                    qty=abs(size),
                    entry_price=self._d(p.get("entry_price")),
                    current_price=self._d(p.get("mark_price")),
                    pnl=self._d(p.get("unrealized_pnl")),
                    raw=p,
                )
            )
        return result

    def get_orders(self, status: str = "all") -> List[Dict]:
        params = {}
        if status == "open":
            params["state"] = "open"
        if status == "closed":
            params["state"] = "closed"
        resp = self._get("/v2/orders", params=params)
        return resp.get("result", [])

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
            resp = self._get(f"/v2/orders/{order_id}")

            if not resp.get("success"):
                return {
                    "success": False,
                    "order_id": order_id,
                    "status": "unknown",
                    "message": resp.get("error", {}).get("message", "API error"),
                    "raw": resp,
                }

            order = resp.get("result", {})

            if not order:
                return {
                    "success": False,
                    "order_id": order_id,
                    "status": "unknown",
                    "message": "Order not found",
                    "raw": {},
                }

            raw_state = (order.get("state") or "").lower()
            normalized_status = self._STATUS_MAP.get(raw_state, "unknown")

            # Delta gives size (total) and unfilled_size (remaining)
            total_qty = self._d(order.get("size"))
            pending_qty = self._d(order.get("unfilled_size"))
            filled_qty = total_qty - pending_qty

            return {
                "success": True,
                "order_id": order_id,
                "status": normalized_status,
                "filled_qty": filled_qty,
                "pending_qty": pending_qty,
                "price": self._d(order.get("limit_price") or order.get("stop_price")),
                "avg_price": self._d(order.get("average_fill_price")),
                "symbol": order.get("product_symbol", ""),
                "raw": order,
            }

        except Exception as e:
            logger.exception(f"get_order_status error: order_id={order_id}")
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
        delta_symbol = self._convert_symbol(symbol)
        payload = {
            "product_symbol": delta_symbol,
            "side": side,
            "size": int(qty),
            "order_type": "market_order" if order_type == "market" else "limit_order",
        }
        if order_type == "limit":
            payload["limit_price"] = str(price)
        try:
            resp = self._post("/v2/orders", payload)
            if resp.get("success"):
                return OrderResult(
                    success=True,
                    order_id=str(resp["result"].get("id", "")),
                    message="Order placed",
                    raw=resp,
                )
            return OrderResult(
                success=False,
                message=resp.get("error", {}).get("message", "Failed"),
                raw=resp,
            )
        except Exception as e:
            return OrderResult(success=False, message=str(e))

    def cancel_order(self, order_id: str) -> OrderResult:
        try:
            resp = self._delete(f"/v2/orders/{order_id}")
            if resp.get("success"):
                return OrderResult(
                    success=True, order_id=order_id, message="Cancelled", raw=resp
                )
            return OrderResult(
                success=False, message=str(resp.get("error", "Failed")), raw=resp
            )
        except Exception as e:
            return OrderResult(success=False, message=str(e))

    def get_quote(self, symbol: str) -> Dict:
        try:
            delta_symbol = self._convert_symbol(symbol)
            logger.debug(f"Quote: {symbol} → {delta_symbol}")
            # ✅ FIX: tuple timeout
            r = requests.get(f"{self._base}/v2/tickers/{delta_symbol}", timeout=(3, 8))
            r.raise_for_status()
            data = r.json().get("result", {})
            quotes = data.get("quotes", {})
            return {
                "ltp": self._d(data.get("close") or data.get("last_price")),
                "bid": self._d(quotes.get("best_bid") or data.get("best_bid_price")),
                "ask": self._d(quotes.get("best_ask") or data.get("best_ask_price")),
                "volume": self._d(data.get("volume")),
                "change": self._d(data.get("change")),
                "open": self._d(data.get("open")),
                "high": self._d(data.get("high")),
                "low": self._d(data.get("low")),
                "raw": data,
            }
        except Exception as e:
            logger.error(f"Quote error: {symbol} | {e}")
            return {"ltp": 0, "error": str(e)}

    def get_candles(
        self,
        symbol: str,
        resolution: str,
        from_ts: int,
        to_ts: int,
    ) -> List[CandleBar]:
        """Fetch candles from Delta Exchange India server."""
        delta_symbol = self._convert_symbol(symbol)
        logger.info("Delta candles: %s → %s", symbol, delta_symbol)

        _map = {
            "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m",
            "30m": "30m", "1h": "1h", "4h": "4h", "1d": "1d",
            "1": "1m", "5": "5m", "15": "15m", "60": "1h",
            "240": "4h", "1440": "1d", "D": "1d",
        }
        delta_res = _map.get(str(resolution), "15m")

        try:
            url = f"{self._candle_url}/v2/history/candles"
            params = {
                "symbol": delta_symbol,
                "resolution": delta_res,
                "start": from_ts,
                "end": to_ts,
            }
            logger.info("Delta API: %s | %s", url, params)
            # ✅ FIX: (connect_timeout, read_timeout) tuple
            # timeout=30 (int) sirf READ timeout tha — connect infinite tha.
            r = requests.get(url, params=params, timeout=(3, 8))
            r.raise_for_status()
            data = r.json()

            if not data.get("success"):
                logger.error("Delta API error: %s", data)
                return []

            rows = data.get("result", [])
            candles = [
                CandleBar(
                    timestamp=int(row.get("time", 0)),
                    open=self._d(row.get("open")),
                    high=self._d(row.get("high")),
                    low=self._d(row.get("low")),
                    close=self._d(row.get("close")),
                    volume=self._d(row.get("volume")),
                )
                for row in rows
            ]
            logger.info("Delta candles fetched: %d bars", len(candles))
            return candles

        except Exception as e:
            logger.exception("Delta candles error: %s → %s", symbol, delta_symbol)
            return []