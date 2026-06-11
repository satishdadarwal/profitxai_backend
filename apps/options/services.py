# apps/options/services.py
# ─────────────────────────────────────────────────────────────────────────────
#  Complete corrected version with proper type hints and imports
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Optional, Dict, Any, List

from django.db import transaction
from django.utils import timezone

if TYPE_CHECKING:
    from .models import OptionSymbol, OptionContract
    from django.contrib.auth.models import User

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  Option Symbol helpers
# ─────────────────────────────────────────────────────────────────────────────

# Strike step mapping (NSE standard)
_STRIKE_STEPS = {
    "NIFTY":      50,
    "BANKNIFTY":  100,
    "FINNIFTY":   50,
    "MIDCPNIFTY": 25,
    "SENSEX":     100,
    "BANKEX":     100,
}

# Lot size mapping (NSE April 2025 revision — fyers_utils.LOT_SIZES ke saath sync)
_LOT_SIZES = {
    "NIFTY":      65,   # April 2025 revised
    "BANKNIFTY":  30,
    "FINNIFTY":   60,   # April 2025 revised (was 40)
    "MIDCPNIFTY": 120,  # April 2025 revised (was 75)
    "SENSEX":     10,
    "BANKEX":     15,
}


def get_or_create_option_symbol(name: str) -> OptionSymbol:
    """
    Symbol name se OptionSymbol fetch karo ya create karo.
    e.g. "NIFTY" → OptionSymbol(name="NIFTY", fyers_symbol="NSE:NIFTY50-INDEX", ...)
    """
    from .models import OptionSymbol

    _FYERS_MAP = {
        "NIFTY":      "NSE:NIFTY50-INDEX",
        "BANKNIFTY":  "NSE:NIFTYBANK-INDEX",
        "FINNIFTY":   "NSE:FINNIFTY-INDEX",
        "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX",
        "SENSEX":     "BSE:SENSEX-INDEX",
        "BANKEX":     "BSE:BANKEX-INDEX",
    }

    upper = name.upper()
    obj, _ = OptionSymbol.objects.get_or_create(
        name=upper,
        defaults={
            "fyers_symbol": _FYERS_MAP.get(upper, f"NSE:{upper}-INDEX"),
            "lot_size":     _LOT_SIZES.get(upper, 75),
            "strike_step":  _STRIKE_STEPS.get(upper, 50),
            "is_active":    True,
        },
    )
    return obj


# ─────────────────────────────────────────────────────────────────────────────
#  Expiry helpers
# ─────────────────────────────────────────────────────────────────────────────

def nearest_expiry(monthly: bool = False) -> date:
    """
    nearest_expiry()         → next Thursday (weekly, NSE standard)
    nearest_expiry(monthly)  → last Thursday of current month
    SENSEX/BANKEX use Friday — but Thursday is used for NIFTY/BNK weekly.
    """
    today = date.today()

    if monthly:
        # Last Thursday of current month
        # Go to next month's 1st, then go back to last Thursday
        if today.month == 12:
            first_next = date(today.year + 1, 1, 1)
        else:
            first_next = date(today.year, today.month + 1, 1)
        last_day = first_next - timedelta(days=1)
        days_back = (last_day.weekday() - 3) % 7   # 3 = Thursday
        monthly_expiry = last_day - timedelta(days=days_back)
        if monthly_expiry < today:
            # Already past — go to next month
            if first_next.month == 12:
                first_next2 = date(first_next.year + 1, 1, 1)
            else:
                first_next2 = date(first_next.year, first_next.month + 1, 1)
            last_day2 = first_next2 - timedelta(days=1)
            days_back2 = (last_day2.weekday() - 3) % 7
            monthly_expiry = last_day2 - timedelta(days=days_back2)
        return monthly_expiry

    # Weekly — aaj ya agla Thursday
    # FIX: aaj Thursday ho toh aaj ka expiry return karo (pehle +7 ho jaata tha)
    days_until_thu = (3 - today.weekday()) % 7
    return today + timedelta(days=days_until_thu)


def format_fyers_symbol(name: str, expiry: date, strike: int, otype: str) -> str:
    """
    NSE Fyers weekly option symbol format: NSE:NIFTY2652123650PE
    Format: NSE:{NAME}{YY}{M}{DD}{STRIKE}{TYPE}

    ✅ FIX: Weekly options use single-char month code (1-9, O, N, D)
    Monthly futures use 3-char (JAN, MAY etc.) — but that is NOT used here.

    Examples:
        NIFTY, 2026-05-21, 23650, PE → NSE:NIFTY2652123650PE
        BANKNIFTY, 2026-05-21, 53600, CE → NSE:BANKNIFTY2652153600CE
    """
    # Single-char month codes: Jan=1 ... Sep=9, Oct=O, Nov=N, Dec=D
    _WEEKLY_MONTH = {
        1: "1", 2: "2", 3: "3", 4: "4", 5: "5",
        6: "6", 7: "7", 8: "8", 9: "9",
        10: "O", 11: "N", 12: "D",
    }
    yy  = str(expiry.year)[2:]
    mon = _WEEKLY_MONTH[expiry.month]
    dd  = str(expiry.day).zfill(2)
    exchange = "BSE" if name.upper() in ("SENSEX", "BANKEX") else "NSE"
    return f"{exchange}:{name.upper()}{yy}{mon}{dd}{int(strike)}{otype}"


# ─────────────────────────────────────────────────────────────────────────────
#  Option Chain & ATM helpers
# ─────────────────────────────────────────────────────────────────────────────

def estimate_chain_premium(spot: float, strike: float, otype: str) -> float:
    """Quick BS-lite premium estimate for chain display."""
    intrinsic = max(0.0, spot - strike) if otype == "CE" else max(0.0, strike - spot)
    tv = max(20.0, min(spot * 0.005, 200.0))
    return round(intrinsic + tv, 2)


def estimate_premium(
    spot: float,
    strike: float,
    option_type: str,
    entry_spot: float,
    entry_premium: float,
) -> float:
    """
    Simplified delta-based premium estimate.
    Good for paper trade monitoring. For live trades, prefer fetching real LTP.
    """
    spot_move   = spot - entry_spot
    moneyness   = (spot - strike) if option_type == "CE" else (strike - spot)

    if moneyness > 200:
        delta = 0.80
    elif moneyness > 100:
        delta = 0.70
    elif moneyness > 0:
        delta = 0.55
    elif moneyness > -100:
        delta = 0.35
    elif moneyness > -200:
        delta = 0.20
    else:
        delta = 0.10

    premium_change = spot_move * delta if option_type == "CE" else -spot_move * delta
    estimated = entry_premium + premium_change
    # Clamp: min ₹1, max 3× entry (reasonable intraday bound)
    return max(1.0, min(estimated, entry_premium * 3.0))


def get_atm_contract(
    symbol_name: str,
    spot: float,
    option_type: str,      # "CE" or "PE"
    expiry: Optional[date] = None,
    monthly: bool = False,
) -> OptionContract:
    """
    Spot price se ATM OptionContract fetch karo ya create karo.
    Algo signal handler aur LiveOptionTradeView dono yahi use karein.

    Args:
        symbol_name:  "NIFTY", "BANKNIFTY" etc.
        spot:         Current index price
        option_type:  "CE" or "PE"
        expiry:       Override expiry date (None = nearest weekly)
        monthly:      If True, use monthly expiry

    Returns:
        OptionContract instance (already saved in DB)
    """
    from .models import OptionContract

    sym    = get_or_create_option_symbol(symbol_name)
    step   = sym.strike_step
    atm    = round(spot / step) * step
    exp    = expiry or nearest_expiry(monthly=monthly)
    fyers  = format_fyers_symbol(sym.name, exp, int(atm), option_type)

    contract, created = OptionContract.objects.get_or_create(
        symbol      = sym,
        strike      = float(atm),
        option_type = option_type,
        expiry      = exp,
        defaults    = {"fyers_symbol": fyers},
    )
    if created:
        logger.info(
            "OptionContract created | %s %s%s %s | fyers=%s",
            sym.name, atm, option_type, exp, fyers,
        )
    return contract


def build_option_chain(spot: float, symbol: OptionSymbol) -> List[Dict[str, Any]]:
    """
    Spot se ±5 strikes ka chain build karo.
    OptionSnapshot bhi save karta hai har contract ka.
    """
    from .models import OptionContract, OptionSnapshot

    step   = symbol.strike_step
    atm    = round(spot / step) * step
    strikes = [atm + (i - 5) * step for i in range(11)]
    expiry  = nearest_expiry()
    chain: List[Dict[str, Any]] = []

    for strike in strikes:
        for otype in ("CE", "PE"):
            fyers_sym = format_fyers_symbol(symbol.name, expiry, int(strike), otype)

            contract, _ = OptionContract.objects.get_or_create(
                symbol=symbol,
                strike=float(strike),
                option_type=otype,
                expiry=expiry,
                defaults={"fyers_symbol": fyers_sym},
            )

            ltp = estimate_chain_premium(spot, strike, otype)

            # Save snapshot for historical analysis
            OptionSnapshot.objects.create(
                contract    = contract,
                ltp         = ltp,
                spot_price  = spot,
            )

            moneyness = (
                "ATM" if strike == atm
                else ("ITM" if (otype == "CE" and strike < atm) or (otype == "PE" and strike > atm) else "OTM")
            )

            chain.append({
                "strike":      strike,
                "type":        otype,
                "ltp":         ltp,
                "symbol":      fyers_sym,
                "expiry":      expiry.isoformat(),
                "moneyness":   moneyness,
                "contract_id": str(contract.id),
            })

    return chain


# ─────────────────────────────────────────────────────────────────────────────
#  SL / TP check
# ─────────────────────────────────────────────────────────────────────────────

def check_sltp_for_trade(order, current_premium: float) -> Optional[Dict[str, Any]]:
    """
    Check SL/TP for an Order (options).
    Return {'action': 'close', 'reason': 'SL'/'TP', 'exit_price': X} or None.
    """
    sl = float(order.sl_price) if order.sl_price else None
    tp = float(order.target_price) if order.target_price else None
    side = order.side

    if side == "buy":
        if sl and current_premium <= sl:
            return {"action": "close", "reason": "SL", "exit_price": sl}
        if tp and current_premium >= tp:
            return {"action": "close", "reason": "TP", "exit_price": tp}
    else:  # sell
        if sl and current_premium >= sl:
            return {"action": "close", "reason": "SL", "exit_price": sl}
        if tp and current_premium <= tp:
            return {"action": "close", "reason": "TP", "exit_price": tp}
    return None


def update_trailing_sl(order, current_premium: float) -> float:
    """15% trailing SL — only moves in order's favour."""
    trailing_pct = 0.15
    sl = float(order.sl_price) if order.sl_price else None

    if sl is None:
        return current_premium

    if order.side == "buy":
        new_sl = current_premium * (1 - trailing_pct)
        if new_sl > sl:
            order.sl_price = Decimal(str(new_sl))
            order.save(update_fields=["sl_price", "updated_at"])
    else:
        new_sl = current_premium * (1 + trailing_pct)
        if new_sl < sl:
            order.sl_price = Decimal(str(new_sl))
            order.save(update_fields=["sl_price", "updated_at"])

    return float(order.sl_price)


# ─────────────────────────────────────────────────────────────────────────────
#  Trade lifecycle - FIXED VERSION
# ─────────────────────────────────────────────────────────────────────────────

def close_trade(order_id, close_price: float, reason: str = "manual") -> Dict[str, Any]:
    """
    Close an Order (options trade) by order_id or Order instance.

    Args:
        order_id: Order UUID or Order instance
        close_price: Exit price
        reason: Closure reason (manual/sl_hit/tp_hit/expiry)
    """
    try:
        from apps.orders.models import Order

        if isinstance(order_id, Order):
            order = order_id
        else:
            order = Order.objects.get(id=order_id)

        if order.status != "open":
            raise ValueError(f"Order {order_id} is not open (status: {order.status})")

        entry = float(order.entry_price or 0)
        qty = float(order.quantity or 0)

        if order.side == "buy":
            pnl = (close_price - entry) * qty
        else:
            pnl = (entry - close_price) * qty

        realized_pnl = round(pnl, 2)

        order.exit_price = Decimal(str(close_price))
        order.exit_time = timezone.now()
        order.status = Order.Status.FILLED
        order.exit_reason = reason
        order.realized_pnl = Decimal(str(realized_pnl))
        order.save(update_fields=[
            "exit_price", "exit_time", "status", "exit_reason",
            "realized_pnl", "updated_at",
        ])

        logger.info(
            "Order closed | id=%s | mode=%s | reason=%s | pnl=%.2f",
            order.id, order.mode, reason, realized_pnl,
        )

        return {
            "success": True,
            "trade_id": order.id,
            "realized_pnl": realized_pnl,
            "exit_price": close_price,
            "reason": reason,
        }

    except Exception as e:
        logger.error(f"Error closing order {order_id}: {str(e)}")
        return {"success": False, "error": str(e)}


def monitor_open_trades() -> None:
    """
    Monitor all open option orders for SL/TP hits.
    Called by Celery beat every 10 seconds.
    """
    from apps.orders.models import Order

    open_orders = Order.objects.filter(status="open", instrument_type="options")

    for order in open_orders:
        try:
            current_price = float(order.current_price) if order.current_price else None

            if current_price is None:
                continue

            sl = float(order.sl_price) if order.sl_price else None
            tp = float(order.target_price) if order.target_price else None

            if order.side == "buy":
                if sl and current_price <= sl:
                    close_trade(order, sl, "sl_hit")
                    continue
                if tp and current_price >= tp:
                    close_trade(order, tp, "tp_hit")
                    continue
            else:
                if sl and current_price >= sl:
                    close_trade(order, sl, "sl_hit")
                    continue
                if tp and current_price <= tp:
                    close_trade(order, tp, "tp_hit")
                    continue

        except Exception as e:
            logger.error(f"Error monitoring order {order.id}: {str(e)}")
            continue


# ─────────────────────────────────────────────────────────────────────────────
#  Live option trade placement (Algo + Manual dono ke liye)
# ─────────────────────────────────────────────────────────────────────────────

def place_live_option_trade(
    *,
    user: User,
    symbol_name: str,        # "NIFTY", "BANKNIFTY"
    option_type: str,        # "CE" or "PE"
    action: str,             # "buy" or "sell"
    lots: int,
    spot: float,             # Current index spot price
    entry_price: float,      # Estimated premium at entry
    stop_loss: float,
    target_price: float,
    setup_type: str = "Algo",
    timeframe: str = "15",
    expiry: Optional[date] = None,
    monthly: bool = False,
    strategy: Any = None,           # Strategy instance (optional)
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Live options trade place karo (Order + BrokerOrder + Celery task).

    Returns:
        {
            "success": True,
            "trade_id": str,
            "broker_order_id": str,
            "contract": "NSE:NIFTY25MAY2221500CE",
            "message": "..."
        }
    """
    from apps.brokers.models import BrokerAccount, BrokerOrder
    from apps.brokers.tasks import place_broker_order
    from apps.orders.models import Order
    from apps.market.models import Asset

    # ── 1. OptionSymbol + ATM Contract ───────────────────────────────────────
    sym      = get_or_create_option_symbol(symbol_name)
    contract = get_atm_contract(symbol_name, spot, option_type, expiry=expiry, monthly=monthly)
    qty      = lots * sym.lot_size

    # Symbol display: e.g. NIFTY2450023500CE
    symbol_str = f"{symbol_name}{int(contract.strike)}{option_type}"

    # ── 1b. Market hours guard — live orders sirf NSE hours mein ────────────
    from django.utils import timezone as tz
    import datetime
    now_ist = tz.localtime(tz.now(), datetime.timezone(datetime.timedelta(hours=5, minutes=30)))
    market_open  = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
    market_close = now_ist.replace(hour=15, minute=30, second=0, microsecond=0)
    is_weekday   = now_ist.weekday() < 5  # Mon-Fri

    if not (is_weekday and market_open <= now_ist <= market_close):
        logger.warning(
            "place_live_option_trade: Market closed | user=%s | time=%s",
            user.pk, now_ist.strftime("%H:%M %Z"),
        )
        return {"success": False, "error": "Market closed (NSE: 9:15–15:30 IST, Mon–Fri)"}

    # ── 2. Broker account dhundo ─────────────────────────────────────────────
    broker_qs = BrokerAccount.objects.filter(user=user, is_active=True, is_verified=True)
    if strategy and hasattr(strategy, "broker") and strategy.broker:
        broker_account = broker_qs.filter(id=strategy.broker_id).first() or broker_qs.first()
    else:
        broker_account = broker_qs.first()

    if not broker_account:
        return {"success": False, "error": "No active verified broker account found"}

    # ── 2b. Risk check — order place karne se pehle ──────────────────────────
    try:
        from apps.risk.manager import RiskManager
        rm = RiskManager(user)
        allowed, reason = rm.can_place_order(
            symbol=contract.fyers_symbol if hasattr(contract, "fyers_symbol") else symbol_name,
            qty=lots,
            price=entry_price,
            stop_loss=Decimal(str(stop_loss)),
        )
        if not allowed:
            logger.warning(
                "place_live_option_trade: Risk check blocked | user=%s | reason=%s",
                user.pk, reason,
            )
            return {"success": False, "error": f"Risk check failed: {reason}"}
    except Exception as risk_err:
        logger.error(
            "place_live_option_trade: RiskManager error | user=%s | err=%s — BLOCKED",
            user.pk, risk_err,
        )
        return {"success": False, "error": "Risk check system error — order blocked"}

    # ── 3. Get or create Asset ────────────────────────────────────────────────
    asset, _ = Asset.objects.get_or_create(
        symbol=symbol_str,
        defaults={
            "name": symbol_str,
            "asset_type": "options",
            "exchange": "NSE",
            "currency": "INR",
            "is_active": True,
        },
    )

    # ── 4. Order + BrokerOrder atomic create ──────────────────────────────────
    with transaction.atomic():
        order = Order.objects.create(
            user            = user,
            asset           = asset,
            mode            = Order.Mode.LIVE,
            side            = action,
            order_type      = Order.OrderType.MARKET,
            status          = Order.Status.OPEN,
            quantity        = Decimal(str(qty)),
            filled_qty      = Decimal(str(qty)),
            entry_price     = Decimal(str(entry_price)),
            current_price   = Decimal(str(entry_price)),
            sl_price        = Decimal(str(stop_loss)),
            target_price    = Decimal(str(target_price)),
            entry_time      = timezone.now(),
            option_type     = option_type,
            lots            = lots,
            position_size   = qty,
            symbol_display  = symbol_str,
            instrument_type = "options",
            strategy        = strategy,
            notes           = f"setup={setup_type} | timeframe={timeframe}",
        )

        broker_order = BrokerOrder.objects.create(
            broker_account = broker_account,
            order          = order,
            symbol         = contract.fyers_symbol,
            side           = action.lower(),
            order_type     = BrokerOrder.OrderType.ENTRY,
            quantity       = int(qty),
            price          = float(entry_price),
            status         = BrokerOrder.Status.PENDING,
            notes          = (
                f"lots={lots} | type={option_type} | "
                f"strike={contract.strike} | expiry={contract.expiry.isoformat()} | "
                f"spot={spot} | setup={setup_type}"
            ),
        )

    # ── 5. Celery task — broker ko bhejo ──────────────────────────────────────
    place_broker_order.apply_async(
        args  = [str(broker_order.id)],
        queue = "orders",
    )

    logger.info(
        "Live option Order queued | order=%s | %s %s%s @ %.2f | lots=%d | broker_order=%s",
        order.id, action.upper(), symbol_name, option_type,
        entry_price, lots, broker_order.id,
    )

    return {
        "success":         True,
        "trade_id":        str(order.id),
        "broker_order_id": str(broker_order.id),
        "contract":        contract.fyers_symbol,
        "strike":          contract.strike,
        "expiry":          contract.expiry.isoformat(),
        "lots":            lots,
        "quantity":        qty,
        "estimated_entry": entry_price,
        "stop_loss":       stop_loss,
        "target_price":    target_price,
        "message":         f"Live {action.upper()} {lots}L {symbol_name}{option_type} queued",
    }