import logging
import time
from typing import Dict, List

import requests

logger = logging.getLogger(__name__)

DELTA_BASE = "https://api.india.delta.exchange/v2"

# ✅ SYMBOL MAP
SYMBOL_MAP = {
    "BTC-USDT": "BTCUSD",
    "ETH-USDT": "ETHUSD",
    "SOL-USDT": "SOLUSD",
    "BNB-USDT": "BNBUSD",
    "XRP-USDT": "XRPUSD",
    "ADA-USDT": "ADAUSD",
    "DOGE-USDT": "DOGEUSD",
    "DELTA:BTC-USDT": "BTCUSD",
    "DELTA:ETH-USDT": "ETHUSD",
    "DELTA:SOL-USDT": "SOLUSD",
    "DELTA:BNB-USDT": "BNBUSD",
    "DELTA:XRP-USDT": "XRPUSD",
    "DELTA:ADA-USDT": "ADAUSD",
    "DELTA:DOGE-USDT": "DOGEUSD",
}

TIMEFRAME_MAP = {
    "1": "1m",
    "3": "3m",
    "5": "5m",
    "15": "15m",
    "30": "30m",
    "60": "1h",
    "1H": "1h",
    "4H": "4h",
    "D": "1d",   # ✅ FIXED: Daily timeframe
    "1440": "1d", # ✅ FIXED: Fallback if minutes value sent
}

_ticker_cache: Dict = {}
_ticker_cache_ts: float = 0
_CACHE_TTL = 2


# ✅ FIXED SYMBOL NORMALIZER
def to_delta_symbol(symbol: str) -> str:
    sym = symbol.upper().replace("DELTA:", "")
    return SYMBOL_MAP.get(sym) or sym.replace("-", "")


def _fetch_all_tickers() -> Dict[str, dict]:
    global _ticker_cache, _ticker_cache_ts

    now = time.time()
    if _ticker_cache and (now - _ticker_cache_ts) < _CACHE_TTL:
        return _ticker_cache

    try:
        resp = requests.get(
            f"{DELTA_BASE}/tickers",
            timeout=8,
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()

        if not data.get("success"):
            logger.warning("Delta /tickers failed: %s", data)
            return _ticker_cache

        by_sym = {}
        for item in data.get("result", []):
            sym = item.get("symbol", "")
            if sym:
                by_sym[sym] = item

        _ticker_cache = by_sym
        _ticker_cache_ts = now

        logger.info("Delta all tickers fetched: %d symbols", len(by_sym))
        return by_sym

    except Exception as exc:
        logger.exception("Delta fetch error: %s", exc)
        return _ticker_cache


def _parse_ticker(app_symbol: str, t: dict) -> dict:
    close = float(t.get("close") or 0)
    open_ = float(t.get("open") or close)

    change = round(close - open_, 2)
    chg_pct = round((change / open_ * 100) if open_ else 0, 2)

    quotes = t.get("quotes") or {}

    return {
        "symbol": app_symbol.upper(),
        "delta_sym": t.get("symbol", ""),
        "ltp": close,
        "bid": float(quotes.get("best_bid") or 0),
        "ask": float(quotes.get("best_ask") or 0),
        "volume": float(t.get("volume") or 0),
        "open": open_,
        "high": float(t.get("high") or 0),
        "low": float(t.get("low") or 0),
        "prev_close": open_,
        "change": change,
        "change_pct": chg_pct,
        "source": "delta",
    }


# 🔥 FINAL FIXED FUNCTION (NO BLOCKING CALL)
def fetch_delta_ticker(symbol: str) -> dict:
    delta_sym = to_delta_symbol(symbol)

    all_tickers = _fetch_all_tickers()
    t = all_tickers.get(delta_sym)

    if t:
        return _parse_ticker(symbol, t)

    # ❌ NO single API call (removed)
    logger.warning("Delta no data | %s", delta_sym)

    return {
        "symbol": symbol.upper(),
        "ltp": 0,
        "change": 0,
        "change_pct": 0,
        "error": f"No ticker data for {delta_sym}",
    }


def fetch_delta_tickers_bulk(symbols: List[str]) -> Dict[str, dict]:
    if not symbols:
        return {}

    all_tickers = _fetch_all_tickers()
    result = {}

    for sym in symbols:
        delta_sym = to_delta_symbol(sym)
        t = all_tickers.get(delta_sym)

        if t:
            result[sym.upper()] = _parse_ticker(sym, t)
        else:
            logger.warning("Delta skip | %s", sym)

    logger.info("Delta bulk: requested=%d found=%d", len(symbols), len(result))
    return result


def fetch_delta_candles(symbol: str, timeframe: str = "15", limit: int = 200) -> dict:
    delta_sym = to_delta_symbol(symbol)
    resolution = TIMEFRAME_MAP.get(str(timeframe), "15m")

    # ✅ FIX: Calculate start time based on actual timeframe
    now = int(time.time())
    
    # Convert timeframe to seconds
    tf_seconds_map = {
        "1": 60,
        "3": 180,
        "5": 300,
        "15": 900,
        "30": 1800,
        "60": 3600,
        "1H": 3600,
        "4H": 14400,
        "D": 86400,    # ✅ Daily
        "1440": 86400, # ✅ Fallback if minutes value ever sent
    }
    
    tf_seconds = tf_seconds_map.get(str(timeframe), 900)
    start = now - (limit * tf_seconds)

    try:
        logger.info(
            "🔍 Delta candles: symbol=%s, delta_sym=%s, tf=%s, resolution=%s, limit=%d",
            symbol, delta_sym, timeframe, resolution, limit
        )
        
        resp = requests.get(
            f"{DELTA_BASE}/history/candles",
            params={
                "symbol": delta_sym,
                "resolution": resolution,
                "start": start,
                "end": now,
            },
            timeout=10,
        )

        resp.raise_for_status()
        data = resp.json()
        
        logger.info("✅ Delta API response: success=%s, candles=%d", 
                   data.get("success"), len(data.get("result", [])))

        if not data.get("success"):
            return {"error": "Delta API error"}

        rows = data.get("result", [])
        if not rows:
            return {"error": "No candles"}

        candles = []
        for row in rows:
            candles.append(
                {
                    "ts": int(row.get("time", 0)),
                    "open": float(row.get("open", 0)),
                    "high": float(row.get("high", 0)),
                    "low": float(row.get("low", 0)),
                    "close": float(row.get("close", 0)),
                    "volume": float(row.get("volume", 0)),
                }
            )

        return {"candles": candles[-limit:], "source": "delta"}

    except Exception as e:
        logger.error("Delta candles error: %s", e)
        return {"error": str(e)}


def is_crypto_symbol(symbol: str) -> bool:
    sym = symbol.upper()

    # NSE / BSE exclude
    if sym.startswith("NSE:") or sym.startswith("BSE:"):
        return False

    # Crypto पहचान
    return any(x in sym for x in ["BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE"])