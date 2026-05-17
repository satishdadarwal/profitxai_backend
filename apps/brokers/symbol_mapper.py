# apps/brokers/symbol_mapper.py
"""
Fyers Symbol Normalization
Internal format → Fyers API format
"""

import logging

logger = logging.getLogger(__name__)

# ✅ CORRECT Fyers symbol mapping (verified via API test)
FYERS_SYMBOL_MAP = {
    # Index symbols - KEEP ORIGINAL, they are CORRECT!
    "NSE:NIFTY50-INDEX": "NSE:NIFTY50-INDEX",       # ✅ Already correct
    "NSE:NIFTYBANK-INDEX": "NSE:NIFTYBANK-INDEX",   # ✅ Already correct
    "NSE:FINNIFTY-INDEX": "NSE:FINNIFTY-INDEX",
    "NSE:MIDCPNIFTY-INDEX": "NSE:MIDCPNIFTY-INDEX",
    "BSE:SENSEX-INDEX": "BSE:SENSEX-INDEX",
    
    # Backward compatibility - short names
    "NIFTY": "NSE:NIFTY50-INDEX",
    "BANKNIFTY": "NSE:NIFTYBANK-INDEX",
    "FINNIFTY": "NSE:FINNIFTY-INDEX",
    "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX",
    "SENSEX": "BSE:SENSEX-INDEX",
}


def normalize_for_fyers(symbol: str) -> str:
    # Static map check pehle
    normalized = FYERS_SYMBOL_MAP.get(symbol.upper(), None)
    if normalized:
        return normalized

    # Already correct format hai
    if symbol.startswith("NSE:") or symbol.startswith("BSE:") or symbol.startswith("MCX:"):
        return symbol

    sym_upper = symbol.upper()

    # Options symbols - CE/PE se end hote hain
    if sym_upper.endswith("CE") or sym_upper.endswith("PE"):
        # BSE options
        if any(x in sym_upper for x in ["SENSEX", "BANKEX"]):
            return f"BSE:{symbol}"
        # NSE options
        return f"NSE:{symbol}"

    # Futures
    if "FUT" in sym_upper:
        return f"NSE:{symbol}"

    # Index
    if sym_upper.endswith("-INDEX"):
        if any(x in sym_upper for x in ["SENSEX", "BANKEX"]):
            return f"BSE:{symbol}"
        return f"NSE:{symbol}"

    # Equity default
    return f"NSE:{symbol}"