# apps/common/candle_service.py
#
# Unified candle fetcher — Yahoo, Fyers, Delta sab yahan se.
# fetch_candles(symbol, timeframe, from_ts, to_ts, source="auto")
#
# source="auto"  → symbol dekh ke broker decide karo
#                  BTC/ETH/SOL/crypto → delta
#                  NIFTY/BANKNIFTY/NSE stocks → fyers (fallback: yahoo)
# source="yahoo"  → force Yahoo Finance
# source="fyers"  → force Fyers adapter
# source="delta"  → force Delta adapter

import datetime
import logging
import time
from typing import List, Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
#  CandleBar import — broker_adapters.base se
# ─────────────────────────────────────────────────────────────
from broker_adapters.base import CandleBar

# ─────────────────────────────────────────────────────────────
#  Symbol classifier
# ─────────────────────────────────────────────────────────────

_CRYPTO_KEYWORDS = {
    "BTC",
    "ETH",
    "SOL",
    "BNB",
    "XRP",
    "ADA",
    "DOGE",
    "AVAX",
    "LTC",
    "MATIC",
    "DOT",
    "USDT",
    "USDC",
}

_FYERS_KEYWORDS = {
    "NIFTY",
    "BANKNIFTY",
    "FINNIFTY",
    "SENSEX",
    "NSE:",
    "BSE:",
    "-EQ",
    "-INDEX",
}


def _detect_source(symbol: str) -> str:
    """
    Symbol dekh ke best source decide karo.
    Returns: 'delta' | 'fyers' | 'yahoo'
    """
    upper = symbol.upper()

    # Crypto check
    for kw in _CRYPTO_KEYWORDS:
        if kw in upper:
            return "delta"

    # Indian market check
    for kw in _FYERS_KEYWORDS:
        if kw in upper:
            return "fyers"

    # Default: yahoo (NSE stocks etc.)
    return "yahoo"


# ─────────────────────────────────────────────────────────────
#  Timeframe normalizers
# ─────────────────────────────────────────────────────────────

# Yahoo Finance interval strings
_YAHOO_TF_MAP = {
    "1": "1m",
    "3": "5m",  # yahoo 3m nahi deta — 5m nearest
    "5": "5m",
    "15": "15m",
    "30": "30m",
    "60": "1h",
    "120": "1h",  # yahoo 2h nahi deta
    "240": "1h",  # yahoo 4h nahi deta
    "1440": "1d",
    "D": "1d",
    # string aliases
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "4h": "1h",
    "1d": "1d",
}

# Fyers resolution strings
_FYERS_TF_MAP = {
    "1": "1",
    "5": "5",
    "15": "15",
    "30": "30",
    "60": "60",
    "240": "240",
    "1440": "D",
    "D": "D",
    "1m": "1",
    "5m": "5",
    "15m": "15",
    "30m": "30",
    "1h": "60",
    "4h": "240",
    "1d": "D",
}

# Delta Exchange resolution strings
_DELTA_TF_MAP = {
    "1": "1m",
    "5": "5m",
    "15": "15m",
    "30": "30m",
    "60": "1h",
    "240": "4h",
    "1440": "1d",
    "D": "1d",
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "4h": "4h",
    "1d": "1d",
}


# ─────────────────────────────────────────────────────────────
#  Public API
# ─────────────────────────────────────────────────────────────


def fetch_candles(
    symbol: str,
    timeframe: str,
    from_ts: int,
    to_ts: int,
    source: str = "auto",
    strategy=None,  # optional — fyers credentials ke liye
) -> List[CandleBar]:
    """
    Unified candle fetcher.

    Args:
        symbol    — e.g. 'BTC-USDT', 'NIFTY', 'NSE:RELIANCE-EQ'
        timeframe — '1','5','15','60','240','D'  ya  '1m','15m','1h','1d'
        from_ts   — Unix timestamp (seconds)
        to_ts     — Unix timestamp (seconds)
        source    — 'auto' | 'yahoo' | 'fyers' | 'delta'
        strategy  — Strategy instance (fyers credentials ke liye, optional)

    Returns:
        List[CandleBar] — sorted oldest to newest
        Empty list []   — on error (never raises)
    """
    if source == "auto":
        source = _detect_source(symbol)

    logger.debug(
        "fetch_candles | symbol=%s | tf=%s | source=%s | from=%s | to=%s",
        symbol,
        timeframe,
        source,
        from_ts,
        to_ts,
    )

    try:
        if source == "delta":
            return _fetch_from_delta(symbol, timeframe, from_ts, to_ts)

        if source == "fyers":
            return _fetch_from_fyers(
                symbol, timeframe, from_ts, to_ts, strategy=strategy
            )

        if source == "yahoo":
            return _fetch_from_yahoo(symbol, timeframe, from_ts, to_ts)

        logger.warning("Unknown source '%s' — falling back to yahoo", source)
        return _fetch_from_yahoo(symbol, timeframe, from_ts, to_ts)

    except Exception as e:
        logger.error(
            "fetch_candles unexpected error | symbol=%s | source=%s | err=%s",
            symbol,
            source,
            e,
        )
        return []


# ─────────────────────────────────────────────────────────────
#  Source: Yahoo Finance
# ─────────────────────────────────────────────────────────────

# Yahoo 7-day limit for intraday (minutes)
_YAHOO_INTRADAY_LIMIT_DAYS = 7
_YAHOO_60D_TFS = {"30m"}  # 30m: max 60 days

_YAHOO_SYMBOL_MAP = {
    "NIFTY": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "FINNIFTY": "NIFTY_FIN_SERVICE.NS",
    "SENSEX": "^BSESN",
}


def _yahoo_symbol(symbol: str) -> str:
    clean = symbol.upper()
    for prefix in ("NSE:", "BSE:"):
        clean = clean.replace(prefix, "")
    clean = clean.replace("-INDEX", "").replace("-EQ", "").strip()

    if clean in _YAHOO_SYMBOL_MAP:
        return _YAHOO_SYMBOL_MAP[clean]

    # Crypto — yahoo format
    if any(kw in clean for kw in ("BTC", "ETH", "SOL", "BNB")):
        clean = clean.replace("-USDT", "-USD").replace("-", "")
        return f"{clean}-USD"

    return f"{clean}.NS"


def _fetch_from_yahoo(
    symbol: str,
    timeframe: str,
    from_ts: int,
    to_ts: int,
) -> List[CandleBar]:
    try:
        import yfinance as yf

        yahoo_sym = _yahoo_symbol(symbol)
        interval = _YAHOO_TF_MAP.get(str(timeframe), "15m")

        # Yahoo intraday data limit: 7 days for 1m/5m/15m
        now = int(time.time())
        if interval in ("1m", "5m", "15m"):
            limit_from = now - (_YAHOO_INTRADAY_LIMIT_DAYS * 86400)
            if from_ts < limit_from:
                logger.debug("Yahoo 7-day limit applied for %s interval", interval)
                from_ts = limit_from

        start = datetime.datetime.utcfromtimestamp(from_ts).strftime("%Y-%m-%d")
        end = datetime.datetime.utcfromtimestamp(to_ts).strftime("%Y-%m-%d")

        # end == start hone pe yfinance empty deta hai — +1 day karo
        if start == end:
            end_dt = datetime.datetime.utcfromtimestamp(to_ts) + datetime.timedelta(
                days=1
            )
            end = end_dt.strftime("%Y-%m-%d")

        hist = yf.Ticker(yahoo_sym).history(
            start=start,
            end=end,
            interval=interval,
            auto_adjust=True,
        )

        if hist.empty:
            logger.warning(
                "Yahoo returned empty data | symbol=%s | interval=%s",
                yahoo_sym,
                interval,
            )
            return []

        result = []
        for ts_idx, row in hist.iterrows():
            # pandas Timestamp → unix int
            try:
                ts_unix = int(ts_idx.timestamp())
            except Exception:
                ts_unix = int(ts_idx.value // 1_000_000_000)

            result.append(
                CandleBar(
                    timestamp=ts_unix,
                    open=float(row["Open"]),
                    high=float(row["High"]),
                    low=float(row["Low"]),
                    close=float(row["Close"]),
                    volume=float(row.get("Volume", 0)),
                )
            )

        logger.info("Yahoo candles | symbol=%s | bars=%d", yahoo_sym, len(result))
        return result

    except Exception as e:
        logger.error("Yahoo fetch error | symbol=%s | err=%s", symbol, e)
        return []


# ─────────────────────────────────────────────────────────────
#  Source: Fyers
# ─────────────────────────────────────────────────────────────


def _fyers_symbol(symbol: str) -> str:
    """
    Convert generic symbol to Fyers format using centralized mapper.
    
    'NSE:NIFTY50-INDEX' → 'NSE:NIFTYINDEX-INDEX' ✅ (FIX)
    'NIFTY' → 'NSE:NIFTYINDEX-INDEX'
    'RELIANCE' → 'NSE:RELIANCE-EQ'
    """
    from apps.brokers.symbol_mapper import normalize_for_fyers
    
    upper = symbol.upper()
    
    # ✅ First check: exact match in mapping (handles NSE:NIFTY50-INDEX)
    normalized = normalize_for_fyers(upper)
    if normalized != upper:
        return normalized
    
    # If already formatted with exchange prefix, return as-is
    if ":" in upper:
        return upper
    
    # Legacy fallback for plain symbols like "RELIANCE"
    return f"NSE:{upper}-EQ"


def _fetch_from_fyers(
    symbol: str,
    timeframe: str,
    from_ts: int,
    to_ts: int,
    strategy=None,
) -> List[CandleBar]:
    """
    Fyers adapter se candles fetch karo.
    Strategy ke broker account se credentials lo.
    """
    try:
        from apps.brokers.models import BrokerAccount
        from broker_adapters.registry import BrokerRegistry

        # Credentials — strategy user ka Fyers account
        user = getattr(strategy, "user", None) if strategy else None
        account = None

        if user:
            account = BrokerAccount.objects.filter(
                user=user,
                broker="fyers",
                is_active=True,
                is_verified=True,
            ).first()

        if not account:
            # fallback — koi bhi active fyers account
            account = BrokerAccount.objects.filter(
                broker="fyers",
                is_active=True,
                is_verified=True,
            ).first()

        if not account:
            logger.warning(
                "No active Fyers account — falling back to Yahoo | symbol=%s", symbol
            )
            return _fetch_from_yahoo(symbol, timeframe, from_ts, to_ts)

        adapter = BrokerRegistry.make(
            "fyers",
            {
                "app_id": account.app_id,
                "access_token": account.access_token,
            },
        )

        fyers_sym = _fyers_symbol(symbol)
        resolution = _FYERS_TF_MAP.get(str(timeframe), "15")

        candles = adapter.get_candles(
            symbol=fyers_sym,
            resolution=resolution,
            from_ts=from_ts,
            to_ts=to_ts,
        )

        logger.info("Fyers candles | symbol=%s | bars=%d", fyers_sym, len(candles))
        return candles or []

    except Exception as e:
        logger.error(
            "Fyers fetch error | symbol=%s | err=%s — falling back to Yahoo", symbol, e
        )
        return _fetch_from_yahoo(symbol, timeframe, from_ts, to_ts)


# ─────────────────────────────────────────────────────────────
#  Source: Delta Exchange
# ─────────────────────────────────────────────────────────────


def _fetch_from_delta(symbol, timeframe, from_ts, to_ts):
    """Delta Exchange se candles fetch karo. Public endpoint — no auth needed."""
    try:
        import requests as _requests

        # ✅ DELTA: prefix strip karo pehle
        clean = symbol.upper()
        if clean.startswith("DELTA:"):
            clean = clean.replace("DELTA:", "")  # DELTA:ETH-USDT → ETH-USDT

        _delta_sym_map = {
            "BTC-USDT": "BTCUSD",
            "ETH-USDT": "ETHUSD",
            "SOL-USDT": "SOLUSD",
            "BNB-USDT": "BNBUSD",
            "XRP-USDT": "XRPUSD",
            "ADA-USDT": "ADAUSD",
            "DOGE-USDT": "DOGEUSD",
            "AVAX-USDT": "AVAXUSD",
            "BTCUSDT": "BTCUSD",
            "ETHUSDT": "ETHUSD",
            "SOLUSDT": "SOLUSD",
            "BNBUSDT": "BNBUSD",
            "XRPUSDT": "XRPUSD",
            "ADAUSDT": "ADAUSD",
            "DOGEUSDT": "DOGEUSD",
            "AVAXUSDT": "AVAXUSD",
            "BTCUSD": "BTCUSD",
            "ETHUSD": "ETHUSD",
        }
        delta_sym = _delta_sym_map.get(clean)
        if not delta_sym:
            delta_sym = clean.replace("-USDT", "USD").replace("-", "")

        resolution = _DELTA_TF_MAP.get(str(timeframe), "15m")

        url = "https://api.india.delta.exchange/v2/history/candles"
        params = {
            "symbol": delta_sym,
            "resolution": resolution,
            "start": from_ts,
            "end": to_ts,
        }

        logger.debug("Delta candles request | symbol=%s | params=%s", delta_sym, params)

        # ✅ FIX: (connect_timeout, read_timeout) — timeout=30 infinite connect tha
        r = _requests.get(url, params=params, timeout=(3, 8))
        r.raise_for_status()
        data = r.json()

        if not data.get("success"):
            logger.error("Delta API error | symbol=%s | response=%s", delta_sym, data)
            return []

        rows = data.get("result", [])
        candles = [
            CandleBar(
                timestamp=int(row.get("time", 0)),
                open=float(row.get("open", 0) or 0),
                high=float(row.get("high", 0) or 0),
                low=float(row.get("low", 0) or 0),
                close=float(row.get("close", 0) or 0),
                volume=float(row.get("volume", 0) or 0),
            )
            for row in rows
            if row.get("time")
        ]

        candles.sort(key=lambda c: c.timestamp)
        logger.info("Delta candles | symbol=%s | bars=%d", delta_sym, len(candles))
        return candles

    except Exception as e:
        logger.error("Delta fetch error | symbol=%s | err=%s", symbol, e)
        return []


# ─────────────────────────────────────────────────────────────
#  Convenience helper — execute_cycle ke liye
# ─────────────────────────────────────────────────────────────


def fetch_candles_for_strategy(
    strategy,
    symbol: str,
    timeframe: str,
    bars: int = 200,
) -> List[CandleBar]:
    import time as _time
    import hashlib, pickle
    try:
        from django.core.cache import cache as _djcache
        _ck = f'candles_{hashlib.md5((symbol+timeframe+str(bars)).encode()).hexdigest()}'
        _hit = _djcache.get(_ck)
        if _hit is not None:
            return pickle.loads(_hit)
    except Exception:
        _djcache = None
        _ck = None

    now_ts = int(_time.time())

    # ✅ timeframe string → int safely convert karo
    try:
        tf_minutes = int(timeframe)
    except (ValueError, TypeError):
        tf_minutes = 15  # fallback

    # ✅ FIX: After-hours / weekend mein Fyers recent candles return nahi karta
    # Isliye bars * 3 ka buffer rakho taaki last trading session ka data mile
    # Example: 1m * 500 bars = 500 min ≈ 8h — raat ko empty.
    # Buffer: 500 * 3 = 1500 min ≈ 25h — kal ka session bhi cover ho jata hai
    buffer_multiplier = 3 if tf_minutes <= 5 else 2
    from_ts = now_ts - (tf_minutes * 60 * bars * buffer_multiplier)

    # Broker se source decide karo
    broker_slug = ""
    if strategy and hasattr(strategy, "broker") and strategy.broker:
        broker_slug = getattr(strategy.broker, "broker_slug", "")

    # ✅ Symbol se bhi detect karo — broker_slug na mile toh
    upper = symbol.upper()
    if broker_slug == "delta" or "-USDT" in upper or upper.startswith("DELTA:"):
        source = "delta"
    elif broker_slug == "fyers" or "NSE:" in upper or "BSE:" in upper:
        source = "fyers"
    else:
        source = "auto"

    logger.debug(
        "fetch_candles_for_strategy | symbol=%s | tf=%s | source=%s | from=%s | to=%s",
        symbol,
        timeframe,
        source,
        from_ts,
        now_ts,
    )

    candles = fetch_candles(
        symbol=symbol,
        timeframe=timeframe,
        from_ts=from_ts,
        to_ts=now_ts,
        source=source,
        strategy=strategy,
    )

    # ✅ FIX: Fyers 0 candles return kare (after-hours) → Yahoo fallback try karo
    if not candles and source == "fyers":
        logger.warning(
            "fetch_candles_for_strategy: Fyers returned 0 candles for %s tf=%s "
            "— trying Yahoo fallback",
            symbol,
            timeframe,
        )
        candles = _fetch_from_yahoo(symbol, timeframe, from_ts, now_ts)

    # Last `bars` candles rakho (buffer se zyada aa sakti hain)
    if candles and len(candles) > bars:
        candles = candles[-bars:]

    try:
        if _djcache and _ck and candles:
            import pickle
            _djcache.set(_ck, pickle.dumps(candles), timeout=300)
    except Exception:
        pass
    return candles