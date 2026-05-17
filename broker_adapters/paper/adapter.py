# broker_adapters/paper/adapter.py
#
# FakeFyersAdapter — Paper trading ke liye mock adapter.
#
# BLOCKER #3 FIX:
# ───────────────
# Problem:
#   fill_handler.py sirf Fyers OPEN orders check karta tha.
#   Agar Fyers API reject kare (wrong symbol format, expired token, lot size
#   issue) toh order silently FAILED mark hota tha — bina koi explanation ke.
#   Paper trading se live trading ka code path test hi nahi tha.
#
# Solution:
#   Yeh adapter real Fyers API call nahi karta — sab kuch simulate karta hai.
#   BrokerRegistry mein "paper" slug se register hota hai.
#   place_broker_order task mein: agar order.mode == "paper" toh yahi adapter use karo.
#   fill_handler mein: paper orders ka alag _simulate_paper_fill() path hai.
#
# Isse kya milta hai:
#   - Pura order lifecycle test kar sakte ho bina real money ke:
#       place → OPEN → FILLED → realized_pnl → wallet settlement
#   - Fyers symbol format bugs, lot size errors, token issues — sab paper mein
#     catch ho jaate hain pehle.
#   - Django shell ya management command se ek line mein test:
#       FakeFyersAdapter({}).place_order("NSE:NIFTY50-INDEX", "buy", 1) → OrderResult(success=True, ...)

import uuid
import logging
import time
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
class FakeFyersAdapter(BaseBrokerAdapter):
    """
    Mock adapter for paper trading.

    Real Fyers API se identically behave karta hai, lekin:
    - Koi HTTP call nahi karta
    - Har place_order() immediately success return karta hai
    - Order IDs fake hote hain (UUID-based)
    - get_order_status() always "complete" return karta hai (auto-fill)

    Agar tum chahte ho ki rejection simulate karo (e.g. wrong symbol format
    test karna hai) toh _simulate_failure = True set karo — sab orders reject
    ho jaayenge. Useful for testing rejection handling code path.
    """

    BROKER_SLUG = "paper"
    BROKER_NAME = "Paper Trading (Mock)"

    SUPPORTS_OPTIONS = True
    SUPPORTS_FUTURES = True
    SUPPORTS_EQUITY = True
    SUPPORTS_CRYPTO = True

    REQUIRED_CREDENTIAL_FIELDS = []   # Paper mode mein credentials zaruri nahi

    # ── Failure simulation (testing ke liye) ────────────────────────────────
    # Agar True karo toh saare orders reject honge — rejection path test karne ke liye.
    # Normal use mein False rakho.
    _simulate_failure: bool = False
    _failure_reason: str = "Simulated rejection for testing"

    def __init__(self, credentials: dict):
        # Paper mode mein empty credentials bhi chalenge
        self.credentials = credentials or {}
        logger.debug("FakeFyersAdapter initialized (paper mode)")

    def _validate_credentials(self):
        # Paper mode — no validation needed
        pass

    # ── Minimal symbol validation ────────────────────────────────────────────
    # Yeh real Fyers jitna strict nahi hai, lekin common format errors pakad leta hai.
    # Real Fyers format: "NSE:NIFTY50-INDEX", "NSE:RELIANCE-EQ", "MCX:CRUDEOIL25JANFUT"

    def _warn_symbol_format(self, symbol: str) -> None:
        """Symbol format issues log karo — real Fyers reject kar deta hai inhe."""
        if not symbol:
            logger.warning("FakeFyersAdapter: empty symbol — real Fyers would reject this")
            return
        if ":" not in symbol:
            logger.warning(
                "FakeFyersAdapter: symbol '%s' has no exchange prefix (e.g. 'NSE:') — "
                "real Fyers would reject this with 'Invalid symbol'",
                symbol,
            )
        known_exchanges = {"NSE", "BSE", "MCX", "BFO", "NFO", "NSE_CM"}
        exchange = symbol.split(":")[0] if ":" in symbol else ""
        if exchange and exchange not in known_exchanges:
            logger.warning(
                "FakeFyersAdapter: unknown exchange '%s' in symbol '%s' — "
                "real Fyers may reject this",
                exchange, symbol,
            )

    # ── BaseBrokerAdapter implementation ────────────────────────────────────

    def verify_connection(self) -> Dict:
        return {
            "success": True,
            "message": "Paper mode — no real connection needed",
            "profile": {"name": "Paper Trader", "broker": "paper"},
        }

    def get_funds(self) -> FundsInfo:
        """Paper wallet mein hamesha funds available hain (from INR Wallet model)."""
        return FundsInfo(
            available=100_000.0,
            used=0.0,
            total=100_000.0,
            currency="INR",
            raw={"source": "paper_mode"},
        )

    def get_positions(self) -> List[PositionInfo]:
        """Paper positions DB se aani chahiye — yahan empty list return karo."""
        return []

    def get_orders(self, status: str = "all") -> List[Dict]:
        return []

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
        Paper order place karo.

        ✅ Symbol format warnings log karta hai — inhe fix karo BEFORE live trading.
        ✅ Lot size check: Fyers mein qty integer hona chahiye, 0 nahi.
        ✅ Failure simulation support for testing rejection code path.
        """
        self._warn_symbol_format(symbol)

        # Failure simulation (testing rejection path ke liye)
        if self._simulate_failure:
            logger.info(
                "FakeFyersAdapter: SIMULATED REJECTION | symbol=%s | reason=%s",
                symbol, self._failure_reason,
            )
            return OrderResult(
                success=False,
                message=self._failure_reason,
                raw={"simulated": True, "reason": self._failure_reason},
            )

        # Lot size validation (Fyers requirements mirror karo)
        qty_int = int(qty)
        if qty_int <= 0:
            logger.warning(
                "FakeFyersAdapter: qty=%s rounds to 0 or negative — "
                "real Fyers would reject with 'Invalid quantity'",
                qty,
            )
            return OrderResult(
                success=False,
                message=f"Invalid quantity: {qty} → rounds to {qty_int}. "
                        "Real Fyers requires integer qty > 0.",
                raw={"qty_sent": qty, "qty_int": qty_int},
            )

        # Generate fake order ID (same format as Fyers: numeric string)
        fake_order_id = str(int(time.time() * 1000))[-12:]

        logger.info(
            "📝 Paper order placed | symbol=%s | side=%s | qty=%d | order_type=%s | "
            "fake_order_id=%s",
            symbol, side, qty_int, order_type, fake_order_id,
        )

        return OrderResult(
            success=True,
            order_id=fake_order_id,
            message="Paper order accepted",
            raw={
                "source":        "paper_mode",
                "fake_order_id": fake_order_id,
                "symbol":        symbol,
                "side":          side,
                "qty":           qty_int,
                "order_type":    order_type,
                "price":         price,
                "s":             "ok",
                "code":          200,
            },
        )

    def get_order_status(self, order_id: str) -> Dict:
        """
        Paper orders hamesha auto-filled maano.

        fill_handler.py ka _check_and_process_single_order() yeh call karta hai.
        Paper mode mein hum immediately "complete" return karte hain —
        real time mein market order milliseconds mein fill hota hai.

        avg_price: last known LTP se fill karo. Agar nahi mila toh ek
        reasonable default use karo — paper mode mein exact price matter nahi karta.
        """
        from apps.brokers.fill_handler import _get_paper_fill_price

        fill_price = _get_paper_fill_price(order_id)

        logger.info(
            "📝 Paper order status: COMPLETE (auto-fill) | order_id=%s | fill_price=%s",
            order_id, fill_price,
        )

        return {
            "success":     True,
            "order_id":    order_id,
            "status":      "complete",   # fill_handler.py ko FILLED samajhna chahiye
            "status_code": 2,            # Fyers status code 2 = Traded
            "filled_qty":  1,            # fill_handler _simulate_paper_fill() actual qty use karega
            "pending_qty": 0,
            "avg_price":   fill_price,
            "price":       fill_price,
            "symbol":      "",
            "raw":         {"source": "paper_mode", "order_id": order_id},
        }

    def cancel_order(self, order_id: str) -> OrderResult:
        logger.info("📝 Paper order cancelled | order_id=%s", order_id)
        return OrderResult(
            success=True,
            order_id=order_id,
            message="Paper order cancelled",
            raw={"source": "paper_mode"},
        )

    def get_quote(self, symbol: str) -> Dict:
        """
        Paper mode mein LTP Redis cache se lo.
        Feed running nahi hai toh reasonable fallback return karo.
        """
        try:
            from django.core.cache import cache
            ltp = cache.get(f"ltp:{symbol}")
            if ltp:
                return {"ltp": float(ltp), "bid": 0, "ask": 0, "volume": 0, "change": 0, "raw": {}}
        except Exception:
            pass
        # Fallback: 0 return karo — paper mode mein fill price last_price se aata hai
        return {"ltp": 0.0, "bid": 0, "ask": 0, "volume": 0, "change": 0, "raw": {}}

    def get_candles(
        self, symbol: str, resolution: str, from_ts: int, to_ts: int
    ) -> List[CandleBar]:
        return []

    def refresh_token(self) -> Dict:
        return {"success": True, "message": "Paper mode — no token needed"}

    def get_option_chain(self, symbol: str, expiry: str) -> List:
        return []