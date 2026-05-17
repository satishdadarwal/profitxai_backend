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
    from .models import OptionSymbol, OptionContract, OptionTrade
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

# Lot size mapping (NSE standard - verify before use, NSE changes periodically)
_LOT_SIZES = {
    "NIFTY":      75,
    "BANKNIFTY":  30,
    "FINNIFTY":   40,
    "MIDCPNIFTY": 75,
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

    # Weekly — next Thursday
    days_until_thu = (3 - today.weekday() + 7) % 7
    if days_until_thu == 0:
        days_until_thu = 7
    return today + timedelta(days=days_until_thu)


def format_fyers_symbol(name: str, expiry: date, strike: int, otype: str) -> str:
    """
    NSE Fyers option symbol format: NSE:NIFTY25MAY2221500CE
    Format: NSE:{NAME}{YY}{MON}{DD}{STRIKE}{TYPE}
    """
    months = ["", "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
              "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
    yy  = str(expiry.year)[2:]
    mon = months[expiry.month]
    dd  = str(expiry.day).zfill(2)
    return f"NSE:{name.upper()}{yy}{mon}{dd}{int(strike)}{otype}"


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

def check_sltp_for_trade(trade: OptionTrade, current_premium: float) -> Optional[Dict[str, Any]]:
    """Return {'action': 'close', 'reason': 'SL'/'TP', 'exit_price': X} or None."""
    if trade.action == "buy":
        if current_premium <= trade.stop_loss:
            return {"action": "close", "reason": "SL", "exit_price": trade.stop_loss}
        if current_premium >= trade.target_price:
            return {"action": "close", "reason": "TP", "exit_price": trade.target_price}
    else:  # sell
        if current_premium >= trade.stop_loss:
            return {"action": "close", "reason": "SL", "exit_price": trade.stop_loss}
        if current_premium <= trade.target_price:
            return {"action": "close", "reason": "TP", "exit_price": trade.target_price}
    return None


def update_trailing_sl(trade: OptionTrade, current_premium: float) -> float:
    """15% trailing SL — only moves in trade's favour."""
    trailing_pct = 0.15

    if trade.action == "buy":
        new_sl = current_premium * (1 - trailing_pct)
        if new_sl > trade.stop_loss:
            trade.stop_loss = new_sl
            trade.save(update_fields=["stop_loss"])
    else:
        new_sl = current_premium * (1 + trailing_pct)
        if new_sl < trade.stop_loss:
            trade.stop_loss = new_sl
            trade.save(update_fields=["stop_loss"])

    return trade.stop_loss


# ─────────────────────────────────────────────────────────────────────────────
#  Trade lifecycle - FIXED VERSION
# ─────────────────────────────────────────────────────────────────────────────

def close_trade(trade_id: int, close_price: float, reason: str = "manual") -> Dict[str, Any]:
    """
    Close an option trade and update account balance
    
    Args:
        trade_id: OptionTrade ID
        close_price: Exit price
        reason: Closure reason (manual/sl_hit/tp_hit/expiry)
    
    CRITICAL FIX: acc.save() now correctly indented inside paper mode block
    """
    try:
        from .models import OptionTrade
        
        trade = OptionTrade.objects.get(id=trade_id)
        
        # Validate trade state
        if trade.status != "open":
            raise ValueError(f"Trade {trade_id} is not open (status: {trade.status})")
        
        # Calculate P&L - using 'action' field (buy/sell) instead of 'trade_type'
        if trade.action == "buy":
            pnl = (close_price - trade.entry_price) * trade.quantity
        else:  # sell
            pnl = (trade.entry_price - close_price) * trade.quantity
        
        realized_pnl = float(pnl)
        
        # Update trade record - using 'pnl' and 'exit_reason' fields
        trade.exit_price = close_price
        trade.exit_time = timezone.now()
        trade.status = "closed"
        trade.exit_reason = reason  # Changed from close_reason
        trade.pnl = realized_pnl    # Changed from realized_pnl
        trade.save()
        
        # ✅ CRITICAL FIX: acc.save() moved INSIDE the if block
        if trade.mode == "paper":
            try:
                from apps.paper_trading.models import PaperAccount
                acc = PaperAccount.objects.get(user=trade.user)
                margin = trade.entry_price * trade.quantity
                acc.balance += Decimal(str(margin)) + Decimal(str(realized_pnl))
                acc.total_pnl += Decimal(str(realized_pnl))
                acc.save()  # ✅ FIXED: Now correctly indented inside if block
            except Exception as exc:
                logger.error("close_trade: PaperAccount update failed | trade=%s | %s", trade.id, exc)
        
        # For live trades, BrokerOrder will handle the actual closure
        elif trade.mode == "live":
            try:
                from apps.brokers.models import BrokerOrder
                
                # Place exit order via broker adapter (if factory exists)
                try:
                    from apps.broker_adapters.factory import BrokerAdapterFactory
                    adapter = BrokerAdapterFactory.get_adapter(trade.user)
                    
                    # Reverse the trade type for exit
                    exit_side = "sell" if trade.action == "buy" else "buy"
                    
                    adapter.place_order(
                        symbol=trade.contract.fyers_symbol,  # Changed from option_contract
                        quantity=trade.quantity,
                        side=exit_side,
                        order_type="market",
                        product_type="INTRADAY"
                    )
                except ImportError:
                    logger.warning("BrokerAdapterFactory not available, skipping broker order")
                
                # Update BrokerOrder metadata
                bo = BrokerOrder.objects.filter(
                    option_trade=trade,
                    order_type="ENTRY",  # Assuming OrderType.ENTRY exists
                ).first()
                if bo:
                    metadata = bo.metadata or {}
                    metadata.update({"realized_pnl": realized_pnl, "exit_reason": reason})
                    bo.metadata = metadata
                    bo.save(update_fields=["metadata"])
                    
            except Exception as exc:
                logger.warning("close_trade: Live mode processing failed | %s", exc)
        
        logger.info(
            "Trade closed | id=%s | mode=%s | reason=%s | pnl=%.2f",
            trade.id, trade.mode, reason, realized_pnl,
        )
        
        return {
            "success": True,
            "trade_id": trade.id,
            "realized_pnl": realized_pnl,
            "exit_price": close_price,
            "reason": reason
        }
        
    except Exception as e:
        logger.error(f"Error closing trade {trade_id}: {str(e)}")
        return {"success": False, "error": str(e)}


def monitor_open_trades() -> None:
    """
    Monitor all open trades for SL/TP hits
    Called by Celery beat every 10 seconds
    """
    from .models import OptionTrade
    
    open_trades = OptionTrade.objects.filter(status="open")
    
    for trade in open_trades:
        try:
            # Get current LTP from option contract
            current_price = trade.contract.ltp  # Changed from option_contract
            
            if current_price is None:
                continue
            
            # Check Stop Loss
            if trade.stop_loss and current_price <= trade.stop_loss:
                close_trade(
                    trade_id=trade.id,
                    close_price=trade.stop_loss,
                    reason="sl_hit"
                )
                continue
            
            # Check Take Profit (target_price field exists)
            if trade.target_price and current_price >= trade.target_price:
                close_trade(
                    trade_id=trade.id,
                    close_price=trade.target_price,
                    reason="tp_hit"
                )
                continue
            
            # Check Trailing Stop Loss (if fields exist in model)
            # Commented out since use_trailing_sl and trailing_sl_percent may not exist
            # if hasattr(trade, 'use_trailing_sl') and trade.use_trailing_sl:
            #     if hasattr(trade, 'trailing_sl_percent') and trade.trailing_sl_percent:
            #         trailing_sl = current_price * (1 - trade.trailing_sl_percent / 100)
            #         if trade.stop_loss is None or trailing_sl > trade.stop_loss:
            #             trade.stop_loss = trailing_sl
            #             trade.save()
        
        except Exception as e:
            logger.error(f"Error monitoring trade {trade.id}: {str(e)}")
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
    Live options trade place karo (OptionTrade + BrokerOrder + Celery task).

    Returns:
        {
            "success": True,
            "option_trade_id": str,
            "broker_order_id": str,
            "contract": "NSE:NIFTY25MAY2221500CE",
            "message": "..."
        }
    """
    from apps.brokers.models import BrokerAccount, BrokerOrder
    from apps.brokers.tasks import place_broker_order
    from .models import OptionTrade

    # ── 1. OptionSymbol + ATM Contract ───────────────────────────────────────
    sym      = get_or_create_option_symbol(symbol_name)
    contract = get_atm_contract(symbol_name, spot, option_type, expiry=expiry, monthly=monthly)
    qty      = lots * sym.lot_size

    # ── 2. Broker account dhundo ─────────────────────────────────────────────
    broker_qs = BrokerAccount.objects.filter(user=user, is_active=True, is_verified=True)
    if strategy and hasattr(strategy, "broker") and strategy.broker:
        broker_account = broker_qs.filter(id=strategy.broker_id).first() or broker_qs.first()
    else:
        broker_account = broker_qs.first()

    if not broker_account:
        return {"success": False, "error": "No active verified broker account found"}

    # ── 3. OptionTrade + BrokerOrder atomic create ───────────────────────────
    with transaction.atomic():
        trade = OptionTrade.objects.create(
            user         = user,
            mode         = "live",  # Changed from OptionTrade.LIVE
            symbol       = sym,
            contract     = contract,
            action       = action,
            lots         = lots,
            quantity     = qty,
            entry_price  = entry_price,
            target_price = target_price,
            stop_loss    = stop_loss,
            entry_spot   = spot,
            current_price= entry_price,
            setup_type   = setup_type,
            timeframe    = timeframe,
            strategy     = strategy,
            metadata     = metadata or {},
        )

        broker_order = BrokerOrder.objects.create(
            broker_account = broker_account,
            option_trade   = trade,              # legacy live flow FK
            symbol         = contract.fyers_symbol,
            direction      = action.upper(),
            order_type     = "MARKET",
            quantity       = float(qty),
            price          = float(entry_price),
            stop_loss      = float(stop_loss),
            take_profit    = float(target_price),
            status         = "PENDING",  # Changed from BrokerOrder.Status.PENDING
            metadata       = {
                "lots":         lots,
                "option_type":  option_type,
                "strike":       contract.strike,
                "expiry":       contract.expiry.isoformat(),
                "spot_at_entry": spot,
                "setup_type":   setup_type,
            },
        )

    # ── 4. Celery task — broker ko bhejo ─────────────────────────────────────
    place_broker_order.apply_async(
        args  = [str(broker_order.id)],
        queue = "orders",
    )

    logger.info(
        "Live OptionTrade queued | trade=%s | %s %s%s @ %.2f | lots=%d | broker_order=%s",
        trade.id, action.upper(), symbol_name, option_type,
        entry_price, lots, broker_order.id,
    )

    return {
        "success":         True,
        "option_trade_id": str(trade.id),
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