# apps/strategies/fyers_utils.py
#
# Fyers ke liye helper functions:
# 1. ATM option symbol generate karo
# 2. Current month futures symbol generate karo

import datetime
import logging
import math

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
#  Lot sizes
# ─────────────────────────────────────────────────────────────────
LOT_SIZES = {
    "NIFTY": 65,
    "BANKNIFTY": 30,
    "FINNIFTY": 60,
    "MIDCPNIFTY": 120,
    "SENSEX": 10,
}

# Strike step (nearest ATM strike)
STRIKE_STEPS = {
    "NIFTY": 50,
    "BANKNIFTY": 100,
    "FINNIFTY": 50,
    "MIDCPNIFTY": 25,
    "SENSEX": 100,
}


def get_atm_option_symbol(
    symbol: str, current_price: float, option_type: str
) -> str | None:
    """
    ATM option ka Fyers symbol generate karo.

    Format: NSE:NIFTY25JAN24000CE
    symbol: 'NIFTY'
    current_price: 24350.5
    option_type: 'CE' ya 'PE'

    Returns: 'NSE:NIFTY25JAN24350CE' ya None
    """
    try:
        sym = symbol.upper()
        step = STRIKE_STEPS.get(sym, 50)

        # ATM strike = nearest round number
        atm_strike = int(round(current_price / step) * step)

        # Current week/month expiry
        expiry_str = _get_current_expiry(sym)
        if not expiry_str:
            logger.error("Expiry calculate nahi ho rahi | symbol=%s", sym)
            return None

        # Fyers format: NSE:NIFTY25JAN24350CE
        # BSE ke liye SENSEX
        exchange = "BSE" if sym == "SENSEX" else "NSE"
        fyers_symbol = f"{exchange}:{sym}{expiry_str}{atm_strike}{option_type}"

        logger.info(
            "ATM option symbol: %s | strike=%d | expiry=%s",
            fyers_symbol,
            atm_strike,
            expiry_str,
        )
        return fyers_symbol

    except Exception as exc:
        logger.exception("ATM option symbol error: %s", exc)
        return None


def get_current_futures_symbol(symbol: str) -> str | None:
    """
    Current month futures symbol generate karo.

    Format: NSE:NIFTY25JANFUT
    """
    try:
        sym = symbol.upper()
        exchange = "BSE" if sym == "SENSEX" else "NSE"
        month_str = _get_current_futures_expiry()
        fyers_symbol = f"{exchange}:{sym}{month_str}FUT"

        logger.info("Futures symbol: %s", fyers_symbol)
        return fyers_symbol

    except Exception as exc:
        logger.exception("Futures symbol error: %s", exc)
        return None


# ─────────────────────────────────────────────────────────────────
#  Private helpers
# ─────────────────────────────────────────────────────────────────


def _get_current_expiry(symbol: str) -> str | None:
    """
    Weekly expiry symbols ke liye: NIFTY, BANKNIFTY, FINNIFTY → weekly Thursday
    Monthly: MIDCPNIFTY, SENSEX → last Thursday of month

    Returns: '25JAN' format string
    """
    today = datetime.date.today()
    sym = symbol.upper()

    monthly_symbols = ["MIDCPNIFTY", "SENSEX"]

    if sym in monthly_symbols:
        # Last Thursday of current month
        expiry = _last_thursday_of_month(today.year, today.month)
        if expiry < today:
            # Next month
            if today.month == 12:
                expiry = _last_thursday_of_month(today.year + 1, 1)
            else:
                expiry = _last_thursday_of_month(today.year, today.month + 1)
    else:
        # Next (or current) Thursday
        expiry = _next_thursday(today)

    year_2d = str(expiry.year)[2:]
    month_3 = expiry.strftime("%b").upper()
    return f"{year_2d}{month_3}"


def _get_current_futures_expiry() -> str:
    """Current/next month futures expiry: '25JAN' format"""
    today = datetime.date.today()
    expiry = _last_thursday_of_month(today.year, today.month)

    if expiry < today:
        if today.month == 12:
            expiry = _last_thursday_of_month(today.year + 1, 1)
        else:
            expiry = _last_thursday_of_month(today.year, today.month + 1)

    year_2d = str(expiry.year)[2:]
    month_3 = expiry.strftime("%b").upper()
    return f"{year_2d}{month_3}"


def _next_thursday(from_date: datetime.date) -> datetime.date:
    """Agle Thursday ki date (ya aaj agar Thursday hai aur market nahi banda)"""
    days_ahead = 3 - from_date.weekday()  # Thursday = 3
    if days_ahead <= 0:
        days_ahead += 7
    return from_date + datetime.timedelta(days=days_ahead)


def _last_thursday_of_month(year: int, month: int) -> datetime.date:
    """Month ka last Thursday"""
    # Last day of month
    if month == 12:
        last_day = datetime.date(year + 1, 1, 1) - datetime.timedelta(days=1)
    else:
        last_day = datetime.date(year, month + 1, 1) - datetime.timedelta(days=1)

    # Go back to find Thursday (weekday 3)
    offset = (last_day.weekday() - 3) % 7
    return last_day - datetime.timedelta(days=offset)
