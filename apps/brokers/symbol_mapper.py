# apps/brokers/symbol_mapper.py
#
# CHANGES:
#   ✅ BANKEX add kiya (BSE index — Fyers pe tradeable hai)
#   ✅ NIFTYNXT50 add kiya (NSE index — seed_symbols.py mein tha)
#   ✅ Equity stocks ke liye dynamic mapping support add kiya
"""
Fyers Symbol Normalization
Internal format → Fyers API format
"""

import logging
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────
#  Index Symbol Map (strict — yeh hamesha same rahenge)
# ─────────────────────────────────────────────────────────────────
FYERS_SYMBOL_MAP = {
    # Index full-form
    "NSE:NIFTY50-INDEX":     "NSE:NIFTY50-INDEX",
    "NSE:NIFTYBANK-INDEX":   "NSE:NIFTYBANK-INDEX",
    "NSE:FINNIFTY-INDEX":    "NSE:FINNIFTY-INDEX",
    "NSE:MIDCPNIFTY-INDEX":  "NSE:MIDCPNIFTY-INDEX",
    "NSE:NIFTYNXT50-INDEX":  "NSE:NIFTYNXT50-INDEX",   # ✅ ADD
    "BSE:SENSEX-INDEX":      "BSE:SENSEX-INDEX",
    "BSE:BANKEX-INDEX":      "BSE:BANKEX-INDEX",        # ✅ ADD

    # Short names → full Fyers format
    "NIFTY":      "NSE:NIFTY50-INDEX",
    "BANKNIFTY":  "NSE:NIFTYBANK-INDEX",
    "FINNIFTY":   "NSE:FINNIFTY-INDEX",
    "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX",
    "NIFTYNXT50": "NSE:NIFTYNXT50-INDEX",   # ✅ ADD
    "SENSEX":     "BSE:SENSEX-INDEX",
    "BANKEX":     "BSE:BANKEX-INDEX",       # ✅ ADD
}

# BSE symbols set (exchange prefix ke liye)
_BSE_SYMBOLS = {"SENSEX", "BANKEX", "SENSEX-INDEX", "BANKEX-INDEX"}


def normalize_for_fyers(symbol: str) -> str:
    """
    Kisi bhi symbol format ko Fyers API ke liye normalize karo.

    Supports:
    - Index symbols: NIFTY → NSE:NIFTY50-INDEX
    - Option symbols: NIFTY26MAY2224500CE → NSE:NIFTY26MAY2224500CE
    - Futures symbols: NIFTY26MAYFUT → NSE:NIFTY26MAYFUT
    - Equity stocks: RELIANCE → NSE:RELIANCE-EQ
    - Already normalized: NSE:... → as-is
    """
    # Static map check pehle (indices)
    normalized = FYERS_SYMBOL_MAP.get(symbol.upper())
    if normalized:
        return normalized

    # Already correct format
    if symbol.startswith(("NSE:", "BSE:", "MCX:")):
        return symbol

    sym_upper = symbol.upper()

    # Options (CE/PE se end hote hain)
    if sym_upper.endswith("CE") or sym_upper.endswith("PE"):
        if any(x in sym_upper for x in ("SENSEX", "BANKEX")):
            return f"BSE:{symbol}"
        return f"NSE:{symbol}"

    # Futures
    if "FUT" in sym_upper:
        if any(x in sym_upper for x in ("SENSEX", "BANKEX")):
            return f"BSE:{symbol}"
        return f"NSE:{symbol}"

    # Index (INDEX se end)
    if sym_upper.endswith("-INDEX"):
        if any(x in sym_upper for x in ("SENSEX", "BANKEX")):
            return f"BSE:{symbol}"
        return f"NSE:{symbol}"

    # ✅ Equity stock default (NSE:SYMBOL-EQ format)
    # Crypto reject karo
    if sym_upper.endswith("USDT") or sym_upper.endswith("USD") or "-USDT" in sym_upper:
        logger.warning("Crypto symbol Fyers ke liye normalize nahi hoga: %s", symbol)
        return symbol  # as-is return karo — calling code handle karega

    return f"NSE:{symbol}-EQ"