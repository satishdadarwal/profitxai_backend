# broker_adapters/dhan/adapter.py
#
# Dhan Broker Adapter — BaseBrokerAdapter implement karta hai
# Registry mein auto-register hota hai (@BrokerRegistry.register se)
#
# SEBI Compliance (April 2026 mandatory):
#   ✅ Static IP whitelisting — order APIs ke liye zaruri
#      web.dhan.co → My Profile → Static IP → VPS ka IP daalo
#   ✅ Access token 24hr validity (SEBI mandate)
#   ✅ Unique client ID per user — token sharing nahi
#   ✅ Full audit trail — har order ka orderId tracked
#
# ✅ FIX (2026-05-30):
#   _normalize_symbol() ab symbol_mapper.get_dhan_security_info() use karta hai
#   → INDEX: IDX_I segment + numeric securityId (13=NIFTY, 25=BANKNIFTY etc.)
#   → FnO: NSE_FNO/BSE_FNO + numeric securityId from Dhan CSV instrument list
#   → place_order() mein "securityId": trading_symbol → numeric ID se fix
#   → get_quote() mein correct segment + numeric ID
#
# Dhan API v2 docs: https://dhanhq.co/docs/v2/

import logging
from typing import Dict, List, Optional

import requests

from broker_adapters.base import (
    BaseBrokerAdapter,
    OrderResult,
    PositionInfo,
    FundsInfo,
    CandleBar,
)
from broker_adapters.registry import BrokerRegistry

logger = logging.getLogger(__name__)

DHAN_BASE_URL = "https://api.dhan.co/v2"

# ── Dhan status strings ───────────────────────────────────────
_FILLED_STATUSES    = {"TRADED", "PART_TRADED", "FILLED"}
_REJECTED_STATUSES  = {"REJECTED", "INVALID_REQUEST", "NOT_TRADED"}
_CANCELLED_STATUSES = {"CANCELLED", "EXPIRED"}

# ── Product / order type constants ───────────────────────────
PRODUCT_INTRADAY = "INTRADAY"
PRODUCT_CNC      = "CNC"
PRODUCT_MARGIN   = "MARGIN"

ORDER_MARKET = "MARKET"
ORDER_LIMIT  = "LIMIT"
ORDER_SL     = "STOP_LOSS"
ORDER_SL_M   = "STOP_LOSS_MARKET"


# ─────────────────────────────────────────────────────────────
# ✅ Symbol normalization: symbol_mapper use karo
# ─────────────────────────────────────────────────────────────

def _normalize_symbol(raw_symbol: str) -> tuple:
    """
    Internal/Fyers symbol → (security_id, exchange_segment, instrument_type)

    ✅ FIX: symbol_mapper.get_dhan_security_info() use karta hai jisse:
      - INDEX → IDX_I segment + numeric securityId (13, 25, 51 etc.)
      - FnO   → NSE_FNO/BSE_FNO + numeric securityId from Dhan CSV
      - Equity→ NSE_EQ + numeric securityId

    Returns: (security_id: str, exchange_segment: str, instrument_type: str)
    """
    try:
        from broker_adapters.dhan.symbol_mapper import get_dhan_security_info
        security_id, segment = get_dhan_security_info(raw_symbol)
    except ImportError:
        # Fallback agar symbol_mapper available nahi
        security_id, segment = _normalize_symbol_fallback(raw_symbol)

    # instrument_type derive karo segment se
    sym = raw_symbol.upper().strip()
    if ":" in sym:
        _, sym = sym.split(":", 1)

    if segment == "IDX_I" or sym.endswith("-INDEX"):
        instrument = "INDEX"
    elif sym.endswith("CE") or sym.endswith("PE"):
        instrument = "OPTIDX"
    elif "FUT" in sym:
        instrument = "FUTIDX"
    elif segment in ("NSE_EQ", "BSE_EQ"):
        instrument = "EQUITY"
    else:
        instrument = "EQUITY"

    return security_id, segment, instrument


def _normalize_symbol_fallback(raw_symbol: str) -> tuple:
    """
    Fallback — symbol_mapper import fail ho toh yeh use karo.
    String symbol as-is return karta hai (old behavior).
    """
    sym = raw_symbol.upper().strip()
    exchange = "NSE"
    if ":" in sym:
        exchange, sym = sym.split(":", 1)

    if sym.endswith("-INDEX"):
        base = sym.replace("-INDEX", "")
        seg  = "NSE_FNO" if exchange == "NSE" else "BSE_FNO"
        return base, seg

    if sym.endswith("CE") or sym.endswith("PE"):
        seg = "NSE_FNO" if exchange == "NSE" else "BSE_FNO"
        return sym, seg

    if sym.endswith("FUT"):
        seg = "NSE_FNO" if exchange == "NSE" else "BSE_FNO"
        return sym, seg

    eq_sym = sym.replace("-EQ", "")
    seg    = "NSE_EQ" if exchange == "NSE" else "BSE_EQ"
    return eq_sym, seg


# ─────────────────────────────────────────────────────────────
# DhanAdapter
# ─────────────────────────────────────────────────────────────

@BrokerRegistry.register
class DhanAdapter(BaseBrokerAdapter):
    """
    Dhan broker — BaseBrokerAdapter implementation.

    Credentials dict (BrokerAccount se):
        dhan_client_id    — Dhan client ID (e.g. "1000000001")
        dhan_access_token — Bearer token (24hr validity)

    SEBI note:
        Order placement ke liye VPS ka static IP
        web.dhan.co pe whitelist hona chahiye.
        403 error = IP whitelist nahi hua.
    """

    BROKER_SLUG = "dhan"
    BROKER_NAME = "Dhan"

    SUPPORTS_OPTIONS = True
    SUPPORTS_FUTURES = True
    SUPPORTS_EQUITY  = True
    SUPPORTS_CRYPTO  = False

    REQUIRED_CREDENTIAL_FIELDS = ["dhan_client_id", "dhan_access_token"]

    # ── HTTP helpers ──────────────────────────────────────────

    @property
    def _headers(self) -> Dict:
        return {
            "access-token": self.credentials["dhan_access_token"],
            "client-id":    self.credentials["dhan_client_id"],
            "Content-Type": "application/json",
            "Accept":       "application/json",
        }

    def _get(self, path: str, params: dict = None) -> dict:
        url  = f"{DHAN_BASE_URL}{path}"
        resp = requests.get(url, headers=self._headers, params=params, timeout=(3, 10))
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, payload: dict) -> dict:
        url  = f"{DHAN_BASE_URL}{path}"
        resp = requests.post(url, headers=self._headers, json=payload, timeout=(3, 10))
        resp.raise_for_status()
        return resp.json()

    def _delete(self, path: str) -> dict:
        url  = f"{DHAN_BASE_URL}{path}"
        resp = requests.delete(url, headers=self._headers, timeout=(3, 10))
        resp.raise_for_status()
        return resp.json()

    def _handle_http_error(self, e: requests.HTTPError, context: str) -> str:
        """HTTP error ka clean message extract karo."""
        msg = str(e)
        if e.response is not None:
            try:
                body = e.response.json()
                msg  = body.get("remarks", body.get("errorMessage", body.get("message", msg)))
                if e.response.status_code == 403:
                    msg = (
                        f"Access denied (403): {msg}. "
                        "VPS ka static IP web.dhan.co → My Profile → Static IP "
                        "mein whitelist karo."
                    )
                elif e.response.status_code == 401:
                    msg = (
                        f"Unauthorized (401): {msg}. "
                        "Dhan se naya access token generate karo (24hr validity)."
                    )
            except Exception:
                pass
        logger.error("%s HTTP error | %s", context, msg)
        return msg

    # ── BaseBrokerAdapter: required methods ───────────────────

    def verify_connection(self) -> Dict:
        """Token valid hai ya nahi — fundlimit se check karo."""
        try:
            resp = self._get("/fundlimit")
            return {
                "success": True,
                "message": "Dhan connected ✅",
                "profile": {
                    "client_id":         self.credentials["dhan_client_id"],
                    "available_balance": float(resp.get("availabelBalance", 0) or 0),
                },
            }
        except requests.HTTPError as e:
            msg = self._handle_http_error(e, "verify_connection")
            return {"success": False, "message": msg}
        except Exception as exc:
            return {"success": False, "message": str(exc)}

    def get_funds(self) -> FundsInfo:
        """Available margin / funds."""
        try:
            resp = self._get("/fundlimit")
            available = float(resp.get("availabelBalance", 0) or 0)   # Dhan typo
            used      = float(resp.get("utilizedAmount",  0) or 0)
            total     = float(resp.get("sodLimit",        0) or 0)
            if total == 0:
                total = available + used
            return FundsInfo(
                available=available,
                used=used,
                total=total,
                currency="INR",
                raw=resp,
            )
        except Exception as exc:
            logger.error("DhanAdapter.get_funds | %s", exc)
            return FundsInfo(available=0, used=0, total=0, raw={"error": str(exc)})

    def get_positions(self) -> List[PositionInfo]:
        """Open intraday positions."""
        try:
            resp = self._get("/positions")
            rows = resp if isinstance(resp, list) else resp.get("data", [])
            result = []
            for p in rows:
                qty = float(p.get("netQty", 0) or 0)
                if qty == 0:
                    continue
                result.append(PositionInfo(
                    symbol        = p.get("tradingSymbol", ""),
                    side          = "long" if qty > 0 else "short",
                    qty           = abs(qty),
                    entry_price   = float(p.get("costPrice", 0) or 0),
                    current_price = float(p.get("lastTradedPrice", 0) or 0),
                    pnl           = float(p.get("unrealizedProfit", 0) or 0),
                    raw           = p,
                ))
            return result
        except Exception as exc:
            logger.error("DhanAdapter.get_positions | %s", exc)
            return []

    def get_orders(self, status: str = "all") -> List[Dict]:
        """Aaj ke orders. status: 'open'|'closed'|'all'"""
        try:
            resp = self._get("/orders")
            orders = resp if isinstance(resp, list) else resp.get("data", [])
            if status == "open":
                orders = [o for o in orders
                          if o.get("orderStatus", "").upper()
                          not in _FILLED_STATUSES | _REJECTED_STATUSES | _CANCELLED_STATUSES]
            elif status == "closed":
                orders = [o for o in orders
                          if o.get("orderStatus", "").upper()
                          in _FILLED_STATUSES | _REJECTED_STATUSES | _CANCELLED_STATUSES]
            return orders
        except Exception as exc:
            logger.error("DhanAdapter.get_orders | %s", exc)
            return []

    def get_order_status(self, order_id: str) -> Dict:
        """
        Single order ka status — fill_handler.py isko call karta hai.
        """
        try:
            resp = self._get(f"/orders/{order_id}")

            order = resp
            if isinstance(resp, list):
                order = resp[0] if resp else {}

            status_raw = order.get("orderStatus", "").upper()

            if status_raw in _FILLED_STATUSES:
                normalized = "complete"
            elif status_raw in _REJECTED_STATUSES:
                normalized = "rejected"
            elif status_raw in _CANCELLED_STATUSES:
                normalized = "cancelled"
            elif status_raw in ("PENDING", "TRANSIT", "PARTIALLY_PROCESSED"):
                normalized = "open"
            else:
                normalized = "open"

            filled_qty  = float(order.get("filledQty", 0) or 0)
            total_qty   = float(order.get("quantity", 0) or 0)
            pending_qty = max(0, total_qty - filled_qty)

            avg_price   = float(order.get("tradedPrice", 0) or 0)
            if avg_price == 0:
                avg_price = float(order.get("price", 0) or 0)

            return {
                "success":     True,
                "order_id":    order_id,
                "status":      normalized,
                "filled_qty":  filled_qty,
                "pending_qty": pending_qty,
                "avg_price":   avg_price,
                "symbol":      order.get("tradingSymbol", ""),
                "status_code": status_raw,
                "raw":         order,
            }

        except requests.HTTPError as e:
            msg = self._handle_http_error(e, f"get_order_status({order_id})")
            return {"success": False, "order_id": order_id, "status": "unknown",
                    "message": msg, "raw": {}}
        except Exception as exc:
            logger.error("DhanAdapter.get_order_status | order_id=%s | %s", order_id, exc)
            return {"success": False, "order_id": order_id, "status": "unknown",
                    "message": str(exc), "raw": {}}

    def place_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        order_type: str = "market",
        price: float = 0,
        **kwargs,
    ) -> OrderResult:
        """
        Dhan pe order place karo.

        ✅ FIX: securityId ab numeric ID hai (symbol_mapper se)
               INDEX: IDX_I + "13" (NIFTY), FnO: NSE_FNO + CSV se numeric ID

        kwargs (optional):
            stop_price   — SL trigger price
            product_type — "INTRADAY" (default) / "CNC" / "MARGIN"
        """
        try:
            security_id, exchange_seg, instrument = _normalize_symbol(symbol)

            # trading_symbol — display ke liye original symbol
            sym = symbol.upper().strip()
            if ":" in sym:
                _, sym = sym.split(":", 1)
            trading_symbol = sym.replace("-INDEX", "").replace("-EQ", "")

            # Order type mapping
            dhan_order_type = ORDER_MARKET
            if order_type == "limit":
                dhan_order_type = ORDER_LIMIT
            elif order_type in ("sl", "stop_loss"):
                dhan_order_type = ORDER_SL if price > 0 else ORDER_SL_M

            # Product type
            product_type = kwargs.get("product_type", PRODUCT_INTRADAY)
            if product_type == PRODUCT_INTRADAY and instrument in ("OPTIDX", "FUTIDX"):
                product_type = PRODUCT_MARGIN

            client_id = self.credentials["dhan_client_id"]

            payload = {
                "dhanClientId":      client_id,
                "transactionType":   "BUY" if side.lower() == "buy" else "SELL",
                "exchangeSegment":   exchange_seg,
                "productType":       product_type,
                "orderType":         dhan_order_type,
                "validity":          "DAY",
                "tradingSymbol":     trading_symbol,
                "securityId":        security_id,   # ✅ FIX: numeric ID from mapper
                "quantity":          int(qty),
                "disclosedQuantity": 0,
                "price":             float(price) if price else 0.0,
                "triggerPrice":      float(kwargs.get("stop_price", 0) or 0),
                "afterMarketOrder":  False,
            }

            logger.info(
                "DhanAdapter.place_order | client=%s | %s %s [secId=%s seg=%s] %s qty=%s",
                client_id, side.upper(), symbol, security_id, exchange_seg,
                dhan_order_type, qty,
            )

            resp     = self._post("/orders", payload)
            order_id = resp.get("orderId", "")
            status   = resp.get("orderStatus", "")

            if order_id:
                logger.info("✅ Dhan order | id=%s | status=%s", order_id, status)
                return OrderResult(
                    success=True,
                    order_id=str(order_id),
                    message=status,
                    raw=resp,
                )
            else:
                err = resp.get("remarks", resp.get("errorMessage", str(resp)))
                logger.error("❌ Dhan order failed | %s", err)
                return OrderResult(success=False, message=err, raw=resp)

        except requests.HTTPError as e:
            msg = self._handle_http_error(e, "place_order")
            return OrderResult(success=False, message=msg, raw={})
        except Exception as exc:
            logger.exception("DhanAdapter.place_order | %s", exc)
            return OrderResult(success=False, message=str(exc))

    def cancel_order(self, order_id: str) -> OrderResult:
        """Order cancel karo."""
        try:
            self._delete(f"/orders/{order_id}")
            logger.info("✅ Dhan order cancelled | order_id=%s", order_id)
            return OrderResult(success=True, order_id=order_id, message="Cancelled")
        except requests.HTTPError as e:
            msg = self._handle_http_error(e, f"cancel_order({order_id})")
            return OrderResult(success=False, message=msg)
        except Exception as exc:
            logger.error("DhanAdapter.cancel_order | %s", exc)
            return OrderResult(success=False, message=str(exc))

    def get_quote(self, symbol: str) -> Dict:
        """
        Live quote fetch karo.
        ✅ FIX: symbol_mapper se correct securityId + segment use karo.
        Dhan v2 /marketfeed/ohlc → LTP + OHLC dono milte hain.
        Response: {"data": {"IDX_I": {"13": {"last_price": 22500, "ohlc": {...}}}}}
        """
        try:
            security_id, exchange_seg, _ = _normalize_symbol(symbol)

            # ✅ numeric int ID array bhejo
            try:
                sec_id_int = int(security_id)
            except (ValueError, TypeError):
                sec_id_int = security_id  # string fallback

            payload = {exchange_seg: [sec_id_int]}
            resp    = self._post("/marketfeed/ohlc", payload)
            data    = resp.get("data", {})

            # ✅ Response: {segment: {securityId_str: {last_price, ohlc}}}
            seg_data = data.get(exchange_seg, {})
            item = seg_data.get(str(security_id), {})

            if item:
                ltp   = float(item.get("last_price", 0) or 0)
                ohlc  = item.get("ohlc", {}) or {}
                return {
                    "ltp":    ltp,
                    "open":   float(ohlc.get("open",  0) or ltp),
                    "high":   float(ohlc.get("high",  0) or ltp),
                    "low":    float(ohlc.get("low",   0) or ltp),
                    "close":  float(ohlc.get("close", 0) or ltp),
                    "bid":    0.0,
                    "ask":    0.0,
                    "volume": 0,
                    "change": 0.0,
                    "raw":    item,
                }
            return {}
        except Exception as exc:
            logger.error("DhanAdapter.get_quote | %s", exc)
            return {}

    def get_candles(
        self,
        symbol: str,
        resolution: str,
        from_ts: int,
        to_ts: int,
    ) -> List[CandleBar]:
        """
        Historical OHLCV candles.
        ✅ FIX: securityId ab numeric mapper se aata hai.
        Dhan v2: /charts/intraday (intraday) or /charts/historical (daily+)
        """
        try:
            security_id, exchange_seg, instrument = _normalize_symbol(symbol)

            from datetime import datetime
            from_date = datetime.fromtimestamp(from_ts).strftime("%Y-%m-%d")
            to_date   = datetime.fromtimestamp(to_ts).strftime("%Y-%m-%d")

            _res_map = {
                "1":  1,  "1m":  1,
                "5":  5,  "5m":  5,
                "15": 15, "15m": 15,
                "25": 25, "25m": 25,
                "60": 60, "1h":  60,
            }
            is_intraday = resolution.lower() not in ("1d", "day", "1w", "week")

            if is_intraday:
                interval = _res_map.get(str(resolution).lower(), 1)
                endpoint = "/charts/intraday"
                payload  = {
                    "securityId":      security_id,
                    "exchangeSegment": exchange_seg,
                    "instrument":      instrument,
                    "interval":        interval,
                    "fromDate":        from_date,
                    "toDate":          to_date,
                }
            else:
                endpoint = "/charts/historical"
                payload  = {
                    "securityId":      security_id,
                    "exchangeSegment": exchange_seg,
                    "instrument":      instrument,
                    "expiryCode":      0,
                    "fromDate":        from_date,
                    "toDate":          to_date,
                }

            resp = self._post(endpoint, payload)

            opens      = resp.get("open",      [])
            highs      = resp.get("high",      [])
            lows       = resp.get("low",       [])
            closes     = resp.get("close",     [])
            volumes    = resp.get("volume",    [])
            timestamps = resp.get("timestamp", [])

            candles = []
            for i in range(len(closes)):
                candles.append(CandleBar(
                    timestamp = int(timestamps[i]) if i < len(timestamps) else 0,
                    open      = float(opens[i])    if i < len(opens)      else 0,
                    high      = float(highs[i])    if i < len(highs)      else 0,
                    low       = float(lows[i])     if i < len(lows)       else 0,
                    close     = float(closes[i]),
                    volume    = float(volumes[i])  if i < len(volumes)    else 0,
                ))
            return candles

        except Exception as exc:
            logger.error("DhanAdapter.get_candles | %s", exc)
            return []