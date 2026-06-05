# apps/strategies/fyers_utils.py
#
# FIX 1: _next_thursday() — aaj Thursday ho toh aaj ka expiry use karo (na next week)
#         Pehle: days_ahead <= 0 → +7 karo (Thursday ko bhi next week jaata tha)
#         Ab:    days_ahead < 0  → +7 karo (aaj Thursday = days_ahead=0 = aaj ka expiry)
#
# FIX 2: get_atm_option_symbol() — format options/services.py ke format_fyers_symbol se match kiya
#         Fyers weekly option format: NSE:NIFTY26MAY2224500CE  (YY + MON + DD + STRIKE + TYPE)
#         Pehle: NSE:NIFTY26MAY24500CE (DD nahi tha — invalid symbol!)
#         Ab:    NSE:NIFTY26MAY2224500CE (DD add kiya)

import datetime
import logging

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
#  Lot sizes (NSE April 2025 revision)
# ─────────────────────────────────────────────────────────────────
LOT_SIZES = {
    "NIFTY":      65,
    "BANKNIFTY":  30,
    "FINNIFTY":   60,
    "MIDCPNIFTY": 120,
    "SENSEX":     10,
    "BANKEX":     15,
}

# Strike step (nearest ATM strike ke liye)
STRIKE_STEPS = {
    "NIFTY":      50,
    "BANKNIFTY":  100,
    "FINNIFTY":   50,
    "MIDCPNIFTY": 25,
    "SENSEX":     100,
    "BANKEX":     100,
}

# Month codes for Fyers symbol format
# Weekly options:  single char (1-9, O=Oct, N=Nov, D=Dec)
# Monthly futures: 3-char (JAN, FEB ... DEC)
_MONTHS = ["", "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
           "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
def calculate_lots(symbol: str, premium: float, capital: float, risk_pct: float = 0.10) -> int:
    """
    Universal lot calculator — NIFTY/BANKNIFTY/SENSEX/FINNIFTY sab ke liye.
    
    Args:
        symbol:   Base symbol e.g. 'NIFTY', 'BANKNIFTY', 'SENSEX'
        premium:  Current option premium (LTP) per share
        capital:  User wallet balance (INR)
        risk_pct: Max % of capital to risk per trade (default 10%)
    
    Returns:
        lots (int) — minimum 1
    
    Logic:
        risk_amount = capital * risk_pct
        lot_size    = LOT_SIZES[symbol]
        cost_per_lot = premium * lot_size
        lots = max(1, floor(risk_amount / cost_per_lot))
    """
    import math
    base = _clean_symbol(symbol)
    lot_size = LOT_SIZES.get(base, 1)
    if premium <= 0 or lot_size <= 0:
        return 1
    risk_amount = capital * risk_pct
    cost_per_lot = premium * lot_size
    lots = max(1, math.floor(risk_amount / cost_per_lot))
    return lots



# ✅ FIX: Fyers weekly option format uses single-char month code
# Jan=1 ... Sep=9, Oct=O, Nov=N, Dec=D
_WEEKLY_MONTH_CODES = {
    1: "1", 2: "2", 3: "3", 4: "4", 5: "5",
    6: "6", 7: "7", 8: "8", 9: "9",
    10: "O", 11: "N", 12: "D",
}


def _clean_symbol(symbol: str) -> str:
    """
    Raw symbol ko base name mein convert karo — LOT_SIZES lookup ke liye.

    Examples:
        'NSE:NIFTY50-INDEX' → 'NIFTY'
        'NSE:NIFTYBANK-INDEX' → 'BANKNIFTY'
        'NIFTY50-INDEX'     → 'NIFTY'
        'NIFTY'             → 'NIFTY'
        'BTC-USDT'          → 'BTC-USDT'
    """
    raw = symbol.upper().strip()

    for prefix in ("NSE:", "BSE:", "DELTA:"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            break

    SYMBOL_MAP = {
        "NIFTY50-INDEX":    "NIFTY",
        "NIFTY50":          "NIFTY",
        "NIFTYBANK-INDEX":  "BANKNIFTY",
        "NIFTYBANK":        "BANKNIFTY",
        "FINNIFTY-INDEX":   "FINNIFTY",
        "FINNIFTY":         "FINNIFTY",
        "MIDCPNIFTY-INDEX": "MIDCPNIFTY",
        "SENSEX-INDEX":     "SENSEX",
        "BANKEX-INDEX":     "BANKEX",
    }
    return SYMBOL_MAP.get(raw, raw)


def get_atm_option_symbol(
    symbol: str, current_price: float, option_type: str,
    user=None,
) -> str | None:
    """
    ATM option ka Fyers symbol generate karo.

    Strategy:
    1. Pehle Fyers option chain API se real symbol fetch karo (most accurate)
    2. Fallback: constructed symbol use karo

    symbol:        'NIFTY' ya 'NSE:NIFTYBANK-INDEX'
    current_price: underlying spot price
    option_type:   'CE' ya 'PE'
    user:          Django user (option chain fetch ke liye, optional)
    """
    try:
        sym  = _clean_symbol(symbol)

        # ✅ Crypto symbols ke liye Fyers options exist nahi karte
        CRYPTO_SYMBOLS = {
            "BTC-USDT", "ETH-USDT", "BNB-USDT", "XRP-USDT",
            "SOL-USDT", "ADA-USDT", "DOGE-USDT", "BTCUSD", "ETHUSD",
        }
        if sym in CRYPTO_SYMBOLS or "-USDT" in sym or "-USD" in sym:
            logger.warning(
                "Crypto symbol ke liye Fyers options nahi hote — skipping | symbol=%s", symbol
            )
            return None

        step = STRIKE_STEPS.get(sym, 50)
        atm_strike = int(round(current_price / step) * step)

        # ── Strategy 1: Option chain se real Fyers symbol fetch karo ─────────
        try:
            from apps.options.nse_fetcher import fetch_nse_option_chain
            chain_data = fetch_nse_option_chain(symbol=sym, expiry_ts="", user=user)
            chain = chain_data.get("chain", [])

            # ATM strike row dhundo
            atm_row = None
            min_diff = float("inf")
            for row in chain:
                diff = abs(row["strike"] - atm_strike)
                if diff < min_diff:
                    min_diff = diff
                    atm_row = row

            if atm_row:
                opt_data = atm_row.get(option_type, {})
                real_symbol = opt_data.get("symbol", "")
                if real_symbol:
                    logger.info(
                        "ATM option symbol (from chain): %s | strike=%d | expiry=%s",
                        real_symbol, atm_row["strike"], atm_row.get("expiry", ""),
                    )
                    return real_symbol
                else:
                    logger.warning(
                        "Option chain mein symbol field nahi mila | strike=%d | type=%s — constructed symbol fallback",
                        atm_strike, option_type,
                    )
        except Exception as chain_err:
            logger.warning("Option chain fetch failed: %s — constructed symbol fallback", chain_err)

        # ── Strategy 2: Construct symbol (fallback) ───────────────────────────
        expiry_date = _get_current_expiry_date(sym)
        if not expiry_date:
            logger.error("Expiry date nahi mili | symbol=%s", sym)
            return None

        yy  = str(expiry_date.year)[2:]
        mon = _WEEKLY_MONTH_CODES[expiry_date.month]   # single char: 1-9, O, N, D
        dd  = str(expiry_date.day).zfill(2)
        exchange = "BSE" if sym in ("SENSEX", "BANKEX") else "NSE"
        fyers_symbol = f"{exchange}:{sym}{yy}{mon}{dd}{atm_strike}{option_type}"

        logger.info(
            "ATM option symbol (constructed): %s | strike=%d | expiry=%s",
            fyers_symbol, atm_strike, expiry_date,
        )
        return fyers_symbol

    except Exception as exc:
        logger.exception("ATM option symbol error: %s", exc)
        return None


def get_current_futures_symbol(symbol: str) -> str | None:
    """
    Current month futures symbol generate karo.
    Format: NSE:NIFTY26MAYFUT
    """
    try:
        raw = symbol.upper()
        for prefix in ("NSE:", "BSE:", "DELTA:"):
            if raw.startswith(prefix):
                raw = raw[len(prefix):]
                break

        SYMBOL_MAP = {
            "NIFTY50-INDEX":    "NIFTY",
            "NIFTY50":          "NIFTY",
            "NIFTYBANK-INDEX":  "BANKNIFTY",
            "NIFTYBANK":        "BANKNIFTY",
            "FINNIFTY-INDEX":   "FINNIFTY",
            "MIDCPNIFTY-INDEX": "MIDCPNIFTY",
            "SENSEX-INDEX":     "SENSEX",
        }
        sym = SYMBOL_MAP.get(raw, raw)

        CRYPTO_SYMBOLS = {
            "BTC-USDT", "ETH-USDT", "BNB-USDT", "XRP-USDT",
            "SOL-USDT", "ADA-USDT", "DOGE-USDT", "BTCUSD", "ETHUSD",
        }
        if sym in CRYPTO_SYMBOLS or "-USDT" in sym or "-USD" in sym:
            logger.warning("Crypto symbol Fyers futures ke liye valid nahi | %s", symbol)
            return None

        expiry_date = _get_current_futures_expiry_date()
        yy  = str(expiry_date.year)[2:]
        mon = _MONTHS[expiry_date.month]
        exchange = "BSE" if sym in ("SENSEX", "BANKEX") else "NSE"
        fyers_symbol = f"{exchange}:{sym}{yy}{mon}FUT"

        logger.info("Futures symbol: %s", fyers_symbol)
        return fyers_symbol

    except Exception as exc:
        logger.exception("Futures symbol error: %s", exc)
        return None


# ─────────────────────────────────────────────────────────────────
#  Private helpers
# ─────────────────────────────────────────────────────────────────

def _get_current_expiry_date(symbol: str) -> datetime.date | None:
    """
    symbol ka next valid expiry date return karo.

    Weekly symbols (NIFTY, BANKNIFTY, FINNIFTY): next Thursday
    Monthly symbols (MIDCPNIFTY, SENSEX, BANKEX): last Thursday of month

    FIX: aaj Thursday ho toh aaj ka expiry use karo (pehle +7 ho jaata tha)
    """
    today  = datetime.date.today()
    sym    = symbol.upper()

    monthly_symbols = {"MIDCPNIFTY", "SENSEX", "BANKEX"}

    if sym in monthly_symbols:
        expiry = _last_thursday_of_month(today.year, today.month)
        if expiry < today:
            if today.month == 12:
                expiry = _last_thursday_of_month(today.year + 1, 1)
            else:
                expiry = _last_thursday_of_month(today.year, today.month + 1)
    else:
        expiry = _next_thursday_or_today(today)

    return expiry


def _get_current_futures_expiry_date() -> datetime.date:
    """Current/next month futures expiry date."""
    today  = datetime.date.today()
    expiry = _last_thursday_of_month(today.year, today.month)

    if expiry < today:
        if today.month == 12:
            expiry = _last_thursday_of_month(today.year + 1, 1)
        else:
            expiry = _last_thursday_of_month(today.year, today.month + 1)

    return expiry


def _next_thursday_or_today(from_date: datetime.date) -> datetime.date:
    """
    Aaj ya agle Thursday ki date return karo.

    FIX: Pehle `days_ahead <= 0` tha — Thursday (weekday=3) ko
         days_ahead = 3 - 3 = 0, condition True, +7 ho jaata tha.
         Ab `days_ahead < 0` hai — Thursday ko days_ahead=0 → aaj ka expiry.
    """
    days_ahead = 3 - from_date.weekday()  # Thursday = weekday 3
    if days_ahead < 0:                    # FIX: < 0 (not <= 0)
        days_ahead += 7
    return from_date + datetime.timedelta(days=days_ahead)


def _last_thursday_of_month(year: int, month: int) -> datetime.date:
    """Month ka last Thursday."""
    if month == 12:
        last_day = datetime.date(year + 1, 1, 1) - datetime.timedelta(days=1)
    else:
        last_day = datetime.date(year, month + 1, 1) - datetime.timedelta(days=1)

    offset = (last_day.weekday() - 3) % 7
    return last_day - datetime.timedelta(days=offset)


# ─────────────────────────────────────────────────────────────────
#  Legacy string-format helpers (signal_router compatibility)
# ─────────────────────────────────────────────────────────────────

def _get_current_expiry(symbol: str) -> str | None:
    """Legacy: '26MAY' format string return karo (without DD — NOT used for order symbols)."""
    expiry = _get_current_expiry_date(symbol)
    if not expiry:
        return None
    yy  = str(expiry.year)[2:]
    mon = _MONTHS[expiry.month]
    return f"{yy}{mon}"


def _get_current_futures_expiry() -> str:
    """Legacy: '26MAY' format."""
    expiry = _get_current_futures_expiry_date()
    yy  = str(expiry.year)[2:]
    mon = _MONTHS[expiry.month]
    return f"{yy}{mon}"

def get_best_premium_option(symbol: str, current_price: float, option_type: str, user=None,
                             min_premium: float = 80.0, max_premium: float = 400.0) -> dict | None:
    """
    Best premium option select karo — ATM ke aaspaas, premium range mein,
    highest OI + volume wala strike prefer karo.
    Returns: {'symbol': ..., 'strike': ..., 'ltp': ..., 'oi': ..., 'option_type': ...}
    """
    try:
        from apps.options.nse_fetcher import fetch_nse_option_chain
        chain_data = fetch_nse_option_chain(symbol=symbol, expiry_ts="", user=user)
        chain = chain_data.get('chain', [])
        step = STRIKE_STEPS.get(_clean_symbol(symbol), 50)
        atm_strike = int(round(current_price / step) * step)

        candidates = []
        for row in chain:
            opt = row.get(option_type, {})
            ltp = float(opt.get('ltp', 0))
            oi  = float(opt.get('oi', 0))
            vol = float(opt.get('volume', 0))
            sym = opt.get('symbol', '')
            strike = row['strike']
            if not sym or ltp < min_premium or ltp > max_premium:
                continue
            # ATM se kitna door hai (prefer closer)
            distance = abs(strike - atm_strike)
            candidates.append({
                'symbol': sym,
                'strike': strike,
                'ltp': ltp,
                'oi': oi,
                'volume': vol,
                'distance': distance,
                'option_type': option_type,
            })

        if not candidates:
            logger.warning("No candidate found in premium range ₹%.0f-₹%.0f | %s %s",
                           min_premium, max_premium, symbol, option_type)
            return None

        # Score: OI + volume high, distance ATM se kam
        def score(c):
            return (c['oi'] + c['volume']) / (c['distance'] + 1)

        best = max(candidates, key=score)
        logger.info("Best premium option | %s | strike=%d | ltp=₹%.1f | oi=%d | vol=%d",
                    best['symbol'], best['strike'], best['ltp'], best['oi'], best['volume'])
        return best
    except Exception as e:
        logger.exception("get_best_premium_option error: %s", e)
        return None


def get_straddle_options(symbol: str, current_price: float, user=None,
                          min_premium: float = 80.0, max_premium: float = 400.0) -> dict | None:
    """
    Market neutral straddle — ATM CE + ATM PE dono.
    Returns: {'CE': {...}, 'PE': {...}}
    """
    ce = get_best_premium_option(symbol, current_price, 'CE', user, min_premium, max_premium)
    pe = get_best_premium_option(symbol, current_price, 'PE', user, min_premium, max_premium)
    if not ce or not pe:
        logger.warning("Straddle nahi ban saka | %s | CE=%s | PE=%s", symbol, ce, pe)
        return None
    logger.info("Straddle ready | %s | CE=%s ₹%.1f | PE=%s ₹%.1f",
                symbol, ce['symbol'], ce['ltp'], pe['symbol'], pe['ltp'])
    return {'CE': ce, 'PE': pe}
