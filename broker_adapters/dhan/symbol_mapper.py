# broker_adapters/dhan/symbol_mapper.py
#
# Dhan Security ID + Lot Size Mapper
#
# INDEX (IDX_I) — hardcoded, stable, confirmed from Dhan official docs & community:
#   https://dhanhq.co/docs/v2/  +  https://madefortrade.in/t/get-nifty-50-price-using-dhan-api/23462
#
# FnO (NSE_FNO / BSE_FNO) — runtime CSV lookup from Dhan instrument list:
#   https://api.dhan.co/v2/instrument/NSE_FNO   (daily updated)
#   https://api.dhan.co/v2/instrument/BSE_FNO
#
# ✅ Lot size bhi CSV se aata hai — hardcode nahi (SEBI changes hote rehte hain)
#   Jan 2026 current: NIFTY=65, BANKNIFTY=30, FINNIFTY=60, MIDCPNIFTY=120,
#                     NIFTYNXT50=25, SENSEX=20, BANKEX=20
#
# CSV columns (compact format):
#   SEM_SMST_SECURITY_ID, SEM_EXM_EXCH_ID, SEM_SEGMENT, SEM_INSTRUMENT_NAME,
#   SEM_TRADING_SYMBOL, SEM_LOT_UNITS, SEM_CUSTOM_SYMBOL, SEM_EXPIRY_DATE,
#   SEM_STRIKE_PRICE, SEM_OPTION_TYPE, SEM_EXPIRY_CODE, SEM_EXPIRY_FLAG
#
# Usage:
#   from broker_adapters.dhan.symbol_mapper import get_dhan_security_info, get_lot_size
#   sec_id, segment = get_dhan_security_info("NIFTY26JUN2524500CE")
#   lot = get_lot_size("NIFTY26JUN2524500CE")  # → 65 (from CSV)

import csv
import io
import logging
import threading
import time
from typing import Optional, Tuple

import requests

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# INDEX securityId map — IDX_I segment
# Source: Dhan official docs + community confirmed
# ─────────────────────────────────────────────────────────────
INDEX_SECURITY_MAP = {
    # symbol_name → (security_id, segment)
    "NIFTY":        ("13",  "IDX_I"),
    "NIFTY50":      ("13",  "IDX_I"),
    "BANKNIFTY":    ("25",  "IDX_I"),
    "NIFTYBANK":    ("25",  "IDX_I"),
    "FINNIFTY":     ("27",  "IDX_I"),
    "MIDCPNIFTY":   ("442", "IDX_I"),
    "NIFTYNXT50":   ("29",  "IDX_I"),
    "SENSEX":       ("51",  "IDX_I"),
    "BANKEX":       ("194", "IDX_I"),
    # With -INDEX suffix (internal format)
    "NIFTY50-INDEX":    ("13",  "IDX_I"),
    "NIFTYBANK-INDEX":  ("25",  "IDX_I"),
    "FINNIFTY-INDEX":   ("27",  "IDX_I"),
    "MIDCPNIFTY-INDEX": ("442", "IDX_I"),
    "NIFTYNXT50-INDEX": ("29",  "IDX_I"),
    "SENSEX-INDEX":     ("51",  "IDX_I"),
    "BANKEX-INDEX":     ("194", "IDX_I"),
}

# Underlying INDEX → securityId (FnO orders ke liye)
UNDERLYING_SECURITY_ID = {
    "NIFTY":      "13",
    "BANKNIFTY":  "25",
    "FINNIFTY":   "27",
    "MIDCPNIFTY": "442",
    "NIFTYNXT50": "29",
    "SENSEX":     "51",
    "BANKEX":     "194",
}

# ─────────────────────────────────────────────────────────────
# ✅ Fallback lot sizes — sirf CSV fail hone pe use karo
# Current as of Jan 2026 (NSE circular Oct 2025 + Jan 2026)
# ─────────────────────────────────────────────────────────────
_FALLBACK_LOT_SIZES = {
    "NIFTY":      65,
    "BANKNIFTY":  30,
    "FINNIFTY":   60,
    "MIDCPNIFTY": 120,
    "NIFTYNXT50": 25,
    "SENSEX":     20,
    "BANKEX":     20,
}

# ─────────────────────────────────────────────────────────────
# FnO CSV Cache — har 6 ghante ek baar fetch
# _fno_cache: trading_symbol → (security_id, segment, lot_size)
# ─────────────────────────────────────────────────────────────
_fno_cache: dict = {}
_fno_cache_ts: float = 0
_fno_lock = threading.Lock()
_CACHE_TTL = 6 * 3600  # 6 hours

DHAN_FNO_URLS = {
    "NSE_FNO": "https://api.dhan.co/v2/instrument/NSE_FNO",
    "BSE_FNO": "https://api.dhan.co/v2/instrument/BSE_FNO",
}


def _load_fno_csv(segment: str, url: str) -> dict:
    """
    Dhan instrument CSV download karo.
    ✅ SEM_LOT_UNITS bhi store karo — lot size CSV se aata hai, hardcode nahi.

    Returns: {trading_symbol: (security_id, segment, lot_size)}
    """
    result = {}
    try:
        resp = requests.get(url, timeout=(5, 30))
        resp.raise_for_status()
        content = resp.content.decode("utf-8", errors="replace")

        reader = csv.DictReader(io.StringIO(content))
        for row in reader:
            trading_sym = (
                row.get("SEM_TRADING_SYMBOL") or
                row.get("SEM_CUSTOM_SYMBOL") or ""
            ).strip().upper()

            sec_id = (row.get("SEM_SMST_SECURITY_ID") or "").strip()

            # ✅ Lot size CSV se
            try:
                lot_size = int(float(row.get("SEM_LOT_UNITS") or 0))
            except (ValueError, TypeError):
                lot_size = 1

            if trading_sym and sec_id:
                result[trading_sym] = (sec_id, segment, lot_size)

        logger.info(
            "DhanSymbolMapper: loaded %d symbols from %s", len(result), segment
        )
    except Exception as e:
        logger.error("DhanSymbolMapper: CSV load failed [%s]: %s", segment, e)
    return result


def _refresh_fno_cache(force: bool = False):
    """FnO CSV refresh — TTL expired ho ya force=True pe."""
    global _fno_cache, _fno_cache_ts

    now = time.time()
    if not force and _fno_cache and (now - _fno_cache_ts) < _CACHE_TTL:
        return

    with _fno_lock:
        if not force and _fno_cache and (time.time() - _fno_cache_ts) < _CACHE_TTL:
            return

        new_cache = {}
        for segment, url in DHAN_FNO_URLS.items():
            new_cache.update(_load_fno_csv(segment, url))

        if new_cache:
            _fno_cache = new_cache
            _fno_cache_ts = time.time()
            logger.info(
                "DhanSymbolMapper: FnO cache refreshed | total=%d symbols",
                len(_fno_cache),
            )
        else:
            logger.warning(
                "DhanSymbolMapper: FnO cache refresh returned empty — keeping old"
            )


# ─────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────

def get_dhan_security_info(raw_symbol: str) -> Tuple[str, str]:
    """
    Kisi bhi symbol ke liye (security_id, exchange_segment) return karo.

    Priority:
      1. Index map — IDX_I (hardcoded, always fast)
      2. FnO CSV cache — NSE_FNO / BSE_FNO (lazy loaded)
      3. Fallback — symbol string as-is + guessed segment

    Returns: (security_id: str, exchange_segment: str)
    """
    sym = raw_symbol.upper().strip()
    if ":" in sym:
        _, sym = sym.split(":", 1)

    # ── 1. Index lookup ──────────────────────────────────────
    if sym in INDEX_SECURITY_MAP:
        return INDEX_SECURITY_MAP[sym]

    # ── 2. FnO CSV lookup ────────────────────────────────────
    if not _fno_cache or (time.time() - _fno_cache_ts) > _CACHE_TTL:
        _refresh_fno_cache()

    if sym in _fno_cache:
        sec_id, segment, _ = _fno_cache[sym]
        return sec_id, segment

    # ── 3. Fallback ──────────────────────────────────────────
    logger.warning("DhanSymbolMapper: '%s' not in cache — fallback", sym)

    if sym.endswith("CE") or sym.endswith("PE"):
        seg = "BSE_FNO" if any(x in sym for x in ("SENSEX", "BANKEX")) else "NSE_FNO"
        return sym, seg

    if "FUT" in sym:
        seg = "BSE_FNO" if any(x in sym for x in ("SENSEX", "BANKEX")) else "NSE_FNO"
        return sym, seg

    return sym.replace("-EQ", ""), "NSE_EQ"


def get_lot_size(raw_symbol: str) -> int:
    """
    ✅ Lot size CSV se fetch karo — hardcode nahi.
    SEBI periodic revisions ke saath automatically correct rahega.

    Priority:
      1. FnO CSV cache → SEM_LOT_UNITS column
      2. Underlying se match (NIFTY* → NIFTY lot size)
      3. Fallback hardcoded (Jan 2026 values) — sirf CSV fail hone pe

    Args:
        raw_symbol: e.g. "NIFTY26JUN2524500CE", "BANKNIFTY26JUN25FUT", "NIFTY"

    Returns:
        int lot size, e.g. 65 for NIFTY, 30 for BANKNIFTY
    """
    sym = raw_symbol.upper().strip()
    if ":" in sym:
        _, sym = sym.split(":", 1)

    # ── 1. Direct CSV cache lookup ────────────────────────────
    if not _fno_cache or (time.time() - _fno_cache_ts) > _CACHE_TTL:
        _refresh_fno_cache()

    if sym in _fno_cache:
        _, _, lot_size = _fno_cache[sym]
        if lot_size > 0:
            return lot_size

    # ── 2. Underlying match — same underlying ke kisi bhi
    #       contract se lot size nikalo (e.g. NIFTY26JUN25FUT → NIFTY)
    UNDERLYINGS = [
        "MIDCPNIFTY", "NIFTYNXT50", "BANKNIFTY", "FINNIFTY",
        "BANKEX", "SENSEX", "NIFTY",
    ]
    matched_underlying = None
    for u in UNDERLYINGS:
        if sym.startswith(u) or sym == u:
            matched_underlying = u
            break

    if matched_underlying and _fno_cache:
        # Cache mein is underlying ka koi bhi contract dhundo
        for cached_sym, (_, _, lot) in _fno_cache.items():
            if cached_sym.startswith(matched_underlying) and lot > 0:
                logger.debug(
                    "DhanSymbolMapper: lot_size for %s → %d (via %s)",
                    sym, lot, cached_sym,
                )
                return lot

    # ── 3. Hardcoded fallback (Jan 2026 values) ───────────────
    if matched_underlying:
        fallback = _FALLBACK_LOT_SIZES.get(matched_underlying, 1)
        logger.warning(
            "DhanSymbolMapper: lot_size fallback for %s → %d "
            "(CSV unavailable — update if SEBI changes lot size)",
            sym, fallback,
        )
        return fallback

    logger.warning("DhanSymbolMapper: lot_size unknown for %s → returning 1", sym)
    return 1


def preload_cache():
    """
    Server startup pe FnO cache preload karo (background thread mein).
    apps.py ke ready() mein call kar sakte ho.
    """
    thread = threading.Thread(
        target=_refresh_fno_cache, kwargs={"force": True}, daemon=True
    )
    thread.start()
    logger.info("DhanSymbolMapper: background preload started")