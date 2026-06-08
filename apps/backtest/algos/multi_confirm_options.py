# apps/backtest/algos/multi_confirm_options.py
#
# ══════════════════════════════════════════════════════════════════════════════
#  MULTI-CONFIRMATION NSE/BSE OPTIONS STRATEGY — BUYER
#  NIFTY | BANKNIFTY | FINNIFTY | MIDCPNIFTY | SENSEX | BANKEX
# ══════════════════════════════════════════════════════════════════════════════
#
#  STRATEGY OVERVIEW:
#  Option BUYER momentum aur breakout se profit karta hai.
#  Hum ITM/ATM options BUY karte hain jab:
#    - Strong directional trend confirm ho (EMA alignment)
#    - Momentum strong ho (RSI + MACD)
#    - Volatility breakout ho (BB squeeze ke baad expansion)
#    - Volume surge confirm kare institutional move
#    - Expiry pe enough time baki ho (theta decay se bachne ke liye)
#
#  LAYERS:
#  1. TREND LAYER    — EMA 9/21/50 alignment + HTF trend (1H candles)
#  2. MOMENTUM LAYER — RSI direction + MACD histogram
#  3. BREAKOUT LAYER — Bollinger Band squeeze → expansion
#  4. VOLUME LAYER   — Volume surge = institutional entry confirm
#  5. EXPIRY GUARD   — Expiry ke bahut kareeb nahi (theta kills buyers)
#  6. CONFIDENCE     — Weighted score >= min_confidence tab hi trade
#
#  SIGNAL ROUTER INTEGRATION:
#    signal_type = "buy"  → trader_type="buyer" → BUY CE (bullish)
#    signal_type = "sell" → trader_type="buyer" → BUY PE (bearish)
#    risk_config["trader_type"] = "buyer"  ← CRITICAL
#
#  RISK CONFIG (strategy.risk_config mein set karo):
#  {
#    "trader_type": "buyer",     # MUST be "buyer"
#    "sl_pct":      30,          # Option premium ka 30% SL
#    "target_pct":  60,          # 60% gain = profit book
#    "qty":         1,
#  }
#
#  PARAMETERS (strategy.parameters mein):
#  {
#    "fast_ema":       9,
#    "slow_ema":       21,
#    "trend_ema":      50,
#    "rsi_period":     14,
#    "rsi_ob":         70,     # RSI > 70 = bullish momentum confirm
#    "rsi_os":         30,     # RSI < 30 = bearish momentum confirm
#    "rsi_mid_bull":   55,     # RSI > 55 = mild bullish
#    "rsi_mid_bear":   45,     # RSI < 45 = mild bearish
#    "macd_fast":      12,
#    "macd_slow":      26,
#    "macd_signal":    9,
#    "bb_period":      20,
#    "bb_std":         2.0,
#    "bb_squeeze_pct": 2.5,    # BB width < 2.5% = squeeze (breakout imminent)
#    "vol_ma_period":  20,
#    "vol_multiplier": 1.5,    # Volume must be 1.5x above average
#    "min_dte":        2,      # Minimum days to expiry (buyer theta protection)
#    "max_dte":        15,     # Maximum DTE (too far = low premium movement)
#    "htf_lookback":   10,
#    "min_confidence": 62,     # Minimum score to trade (raise = fewer signals)
#    "otm_shift":      0,      # ATM buy (0) ya ITM (negative) ya OTM (positive)
#  }
#
# ══════════════════════════════════════════════════════════════════════════════

from __future__ import annotations

import logging
import math
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional, List, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from apps.backtest.engine import AlgoSignal

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  NSE/BSE Symbol Config
# ─────────────────────────────────────────────────────────────────────────────

NSE_BSE_SYMBOLS = {
    # NSE Indices
    "NIFTY":       {"lot_size": 65,  "strike_step": 50,  "exchange": "NSE", "expiry_day": 3},  # Thursday
    "BANKNIFTY":   {"lot_size": 30,  "strike_step": 100, "exchange": "NSE", "expiry_day": 3},
    "FINNIFTY":    {"lot_size": 60,  "strike_step": 50,  "exchange": "NSE", "expiry_day": 1},  # Tuesday
    "MIDCPNIFTY":  {"lot_size": 120, "strike_step": 25,  "exchange": "NSE", "expiry_day": 3},
    # BSE Indices
    "SENSEX":      {"lot_size": 10,  "strike_step": 100, "exchange": "BSE", "expiry_day": 4},  # Friday
    "BANKEX":      {"lot_size": 15,  "strike_step": 100, "exchange": "BSE", "expiry_day": 1},  # Monday
}

# Symbol aliases — Fyers format se clean naam
_SYMBOL_MAP = {
    "NIFTY50-INDEX":    "NIFTY",
    "NIFTY50":          "NIFTY",
    "NIFTYBANK-INDEX":  "BANKNIFTY",
    "NIFTYBANK":        "BANKNIFTY",
    "FINNIFTY-INDEX":   "FINNIFTY",
    "MIDCPNIFTY-INDEX": "MIDCPNIFTY",
    "SENSEX-INDEX":     "SENSEX",
    "BANKEX-INDEX":     "BANKEX",
}


# ─────────────────────────────────────────────────────────────────────────────
#  Lazy Base (circular import avoid)
# ─────────────────────────────────────────────────────────────────────────────

class _Base:
    name: str = "base"

    def __init__(self, parameters: Optional[dict] = None):
        self.params = parameters or {}

    def get_param(self, key: str, default=None):
        return self.params.get(key, default)

    def generate_signal(self, *args, **kwargs):
        raise NotImplementedError


# ─────────────────────────────────────────────────────────────────────────────
#  Pure Python Indicators (no pandas / numpy dependency)
# ─────────────────────────────────────────────────────────────────────────────


def _ema_series(values: List[float], period: int) -> List[float]:
    if len(values) < period:
        return []
    k = 2 / (period + 1)
    seed = sum(values[:period]) / period
    result = [seed]
    for v in values[period:]:
        seed = v * k + seed * (1 - k)
        result.append(seed)
    return result


def _rsi(closes: List[float], period: int = 14) -> float:
    """RSI — Wilder's smoothing method."""
    if len(closes) < period + 2:
        return 50.0
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [max(d, 0.0) for d in deltas]
    losses = [abs(min(d, 0.0)) for d in deltas]
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    return round(100 - 100 / (1 + avg_g / avg_l), 2)


def _macd(closes: List[float], fast: int = 12, slow: int = 26, signal: int = 9) -> Optional[dict]:
    """MACD — returns macd_line, signal_line, histogram (last values)."""
    if len(closes) < slow + signal:
        return None
    ema_fast = _ema_series(closes, fast)
    ema_slow = _ema_series(closes, slow)
    diff = len(ema_fast) - len(ema_slow)
    if diff > 0:
        ema_fast = ema_fast[diff:]
    elif diff < 0:
        ema_slow = ema_slow[-diff:]
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    if len(macd_line) < signal:
        return None
    sig_line = _ema_series(macd_line, signal)
    if not sig_line:
        return None
    hist = macd_line[-len(sig_line):]
    histogram = [m - s for m, s in zip(hist, sig_line)]
    return {
        "macd":      macd_line[-1],
        "signal":    sig_line[-1],
        "histogram": histogram[-1],
        "hist_prev": histogram[-2] if len(histogram) > 1 else histogram[-1],
    }


def _bollinger(closes: List[float], period: int = 20, std_mult: float = 2.0) -> Optional[dict]:
    if len(closes) < period:
        return None
    window = closes[-period:]
    middle = sum(window) / period
    variance = sum((p - middle) ** 2 for p in window) / period
    std = math.sqrt(variance)
    upper = middle + std_mult * std
    lower = middle - std_mult * std
    width_pct = (upper - lower) / middle * 100 if middle > 0 else 0
    return {
        "upper":     round(upper, 2),
        "middle":    round(middle, 2),
        "lower":     round(lower, 2),
        "std":       round(std, 4),
        "width_pct": round(width_pct, 3),
    }


def _atr(candles: list, period: int = 14) -> float:
    """Average True Range — market volatility measure."""
    if len(candles) < period + 1:
        return 0.0

    def _h(c): return float(c.get("high", 0)) if isinstance(c, dict) else float(getattr(c, "high", 0))
    def _l(c): return float(c.get("low", 0))  if isinstance(c, dict) else float(getattr(c, "low", 0))
    def _c(c): return float(c.get("close", 0)) if isinstance(c, dict) else float(getattr(c, "close", 0))

    trs = []
    for i in range(1, len(candles)):
        h = _h(candles[i])
        l = _l(candles[i])
        pc = _c(candles[i-1])
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)

    if not trs:
        return 0.0

    # Wilder smoothing
    atr_val = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr_val = (atr_val * (period - 1) + tr) / period

    return round(atr_val, 2)


def _vol_ma(candles: list, period: int) -> Optional[float]:
    def _v(c): return float(c.get("volume", 0)) if isinstance(c, dict) else float(getattr(c, "volume", 0))
    vols = [_v(c) for c in candles[-period:]]
    if len(vols) < period or all(v == 0 for v in vols):
        return None
    return sum(vols) / period


def _get_close(c) -> float:
    return float(c["close"] if isinstance(c, dict) else c.close)


def _get_volume(c) -> float:
    if isinstance(c, dict):
        return float(c.get("volume", 0))
    return float(getattr(c, "volume", 0))


# ─────────────────────────────────────────────────────────────────────────────
#  Expiry Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _days_to_expiry(sym: str) -> int:
    """Nearest expiry ke days calculate karo."""
    cfg = NSE_BSE_SYMBOLS.get(sym, {})
    expiry_weekday = cfg.get("expiry_day", 3)
    today = date.today()
    days_ahead = (expiry_weekday - today.weekday()) % 7
    return days_ahead if days_ahead > 0 else 7


def _is_buyer_friendly_dte(dte: int, min_dte: int, max_dte: int) -> Tuple[bool, str]:
    if dte < min_dte:
        return False, f"DTE {dte} too low (min={min_dte}) — theta risk"
    if dte > max_dte:
        return False, f"DTE {dte} too high (max={max_dte}) — low delta impact"
    return True, f"DTE {dte} ok"


# ─────────────────────────────────────────────────────────────────────────────
#  OTM Strike Calculator
# ─────────────────────────────────────────────────────────────────────────────

def _strike_price(spot: float, sym: str, option_type: str, otm_shift: int = 0) -> int:
    cfg = NSE_BSE_SYMBOLS.get(sym, {"strike_step": 50})
    step = cfg["strike_step"]
    atm = round(spot / step) * step
    if option_type == "CE":
        return int(atm + otm_shift * step)
    else:
        return int(atm - otm_shift * step)


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN STRATEGY CLASS
# ─────────────────────────────────────────────────────────────────────────────

class MultiConfirmOptionsAlgo(_Base):
    """
    Multi-Confirmation NSE/BSE Options Buyer Strategy.

    6 layers ek saath confirm karein:
      1. EMA Trend (fast/slow/trend alignment)
      2. RSI Momentum (directional strength)
      3. MACD Histogram (momentum acceleration)
      4. Bollinger Band Breakout (volatility expansion)
      5. Volume Surge (institutional confirm)
      6. DTE Guard (theta protection for buyer)

    signal_type="buy"  → router BUY CE karta hai (bullish)
    signal_type="sell" → router BUY PE karta hai (bearish)
    risk_config["trader_type"] = "buyer" hona ZAROORI hai
    """

    name = "multi_confirm_options"

    _DEFAULTS = {
        "fast_ema":       9,
        "slow_ema":       21,
        "trend_ema":      50,
        "rsi_period":     14,
        "rsi_ob":         65,
        "rsi_os":         35,
        "rsi_mid_bull":   55,
        "rsi_mid_bear":   45,
        "macd_fast":      12,
        "macd_slow":      26,
        "macd_signal":    9,
        "bb_period":      20,
        "bb_std":         2.0,
        "bb_squeeze_pct": 2.5,
        "vol_ma_period":  20,
        "vol_multiplier": 1.5,
        "min_dte":        2,
        "max_dte":        15,
        "htf_lookback":   10,
        "min_confidence": 62,
        "otm_shift":      0,
    }

    def _p(self, key):
        val = self.params.get(key, self._DEFAULTS[key])
        return type(self._DEFAULTS[key])(val)

    def _clean_symbol(self, symbol: str) -> str:
        raw = symbol.upper().strip()
        for prefix in ("NSE:", "BSE:", "NFO:", "BFO:"):
            if raw.startswith(prefix):
                raw = raw[len(prefix):]
        return _SYMBOL_MAP.get(raw, raw)

    def _extract_closes(self, candles: list) -> List[float]:
        return [_get_close(c) for c in candles]

    def generate_signal(
        self,
        symbol: str,
        price: Decimal,
        strategy: object,
        candles=None,
        htf=None,
        mtf=None,
        ltf=None,
        **ctx,
    ) -> "AlgoSignal":

        from apps.backtest.engine import AlgoSignal

        sym   = self._clean_symbol(symbol)
        spot  = float(price)

        if sym not in NSE_BSE_SYMBOLS:
            return AlgoSignal(
                signal_type="hold", symbol=symbol, price=price,
                reason=f"Symbol '{sym}' not in NSE/BSE list. Supported: {list(NSE_BSE_SYMBOLS.keys())}",
                result="skipped",
            )

        all_candles = [c for c in (htf, mtf, ltf, candles) if c]
        if not all_candles:
            return AlgoSignal(
                signal_type="hold", symbol=symbol, price=price,
                reason="No candles available", result="skipped",
            )
        main_candles = max(all_candles, key=len)
        trend_candles = htf if htf and len(htf) >= self._p("htf_lookback") + 1 else main_candles

        min_needed = max(
            self._p("trend_ema") + 5,
            self._p("macd_slow") + self._p("macd_signal") + 2,
            self._p("bb_period") + 5,
            self._p("vol_ma_period") + 2,
        )

        if len(main_candles) < min_needed:
            return AlgoSignal(
                signal_type="hold", symbol=symbol, price=price,
                reason=f"Insufficient candles: {len(main_candles)} < {min_needed}",
                result="skipped",
            )

        closes = self._extract_closes(main_candles)

        ema_f_series = _ema_series(closes, self._p("fast_ema"))
        ema_s_series = _ema_series(closes, self._p("slow_ema"))
        ema_t_series = _ema_series(closes, self._p("trend_ema"))

        if not all([ema_f_series, ema_s_series, ema_t_series]):
            return AlgoSignal(
                signal_type="hold", symbol=symbol, price=price,
                reason="EMA calculation failed", result="skipped",
            )

        ef  = ema_f_series[-1]
        ef2 = ema_f_series[-2] if len(ema_f_series) > 1 else ef
        es  = ema_s_series[-1]
        es2 = ema_s_series[-2] if len(ema_s_series) > 1 else es
        et  = ema_t_series[-1]

        bull_align  = ef > es > et
        bear_align  = ef < es < et
        bull_cross  = ef2 <= es2 and ef > es
        bear_cross  = ef2 >= es2 and ef < es
        price_bull  = spot > et
        price_bear  = spot < et

        htf_closes = self._extract_closes(trend_candles)
        lb = self._p("htf_lookback")
        htf_bull = htf_closes[-1] > htf_closes[-lb] if len(htf_closes) >= lb + 1 else bull_align
        htf_bear = htf_closes[-1] < htf_closes[-lb] if len(htf_closes) >= lb + 1 else bear_align

        rsi_val = _rsi(closes, self._p("rsi_period"))
        rsi_bull_strong = rsi_val >= self._p("rsi_ob")
        rsi_bull_mid    = rsi_val >= self._p("rsi_mid_bull")
        rsi_bear_strong = rsi_val <= self._p("rsi_os")
        rsi_bear_mid    = rsi_val <= self._p("rsi_mid_bear")

        macd = _macd(closes, self._p("macd_fast"), self._p("macd_slow"), self._p("macd_signal"))
        macd_ok = macd is not None

        if macd_ok:
            macd_bull = macd["histogram"] > 0 and macd["histogram"] > macd["hist_prev"]
            macd_bear = macd["histogram"] < 0 and macd["histogram"] < macd["hist_prev"]
            macd_bull_cross = macd["hist_prev"] < 0 and macd["histogram"] >= 0
            macd_bear_cross = macd["hist_prev"] > 0 and macd["histogram"] <= 0
        else:
            macd_bull = macd_bear = macd_bull_cross = macd_bear_cross = False

        bb = _bollinger(closes, self._p("bb_period"), self._p("bb_std"))
        bb_ok = bb is not None

        if bb_ok:
            bb_range    = bb["upper"] - bb["lower"]
            bb_pos      = (spot - bb["lower"]) / bb_range if bb_range > 0 else 0.5
            bb_squeeze  = bb["width_pct"] < self._p("bb_squeeze_pct")
            bb_bull_break = bb_pos > 0.75 and bb_squeeze
            bb_bear_break = bb_pos < 0.25 and bb_squeeze
            bb_bull_strong = spot > bb["upper"]
            bb_bear_strong = spot < bb["lower"]
            bb_bull = bb_pos > 0.60
            bb_bear = bb_pos < 0.40
        else:
            bb_pos = bb_squeeze = bb_bull = bb_bear = False
            bb_bull_break = bb_bear_break = bb_bull_strong = bb_bear_strong = False

        vol_ma_val  = _vol_ma(main_candles, self._p("vol_ma_period"))
        curr_vol    = _get_volume(main_candles[-1])
        vol_surge   = False
        vol_ratio   = 0.0

        if vol_ma_val and vol_ma_val > 0:
            vol_ratio = curr_vol / vol_ma_val
            vol_surge = vol_ratio >= self._p("vol_multiplier")
        else:
            vol_surge = True

        dte         = _days_to_expiry(sym)
        dte_ok, dte_note = _is_buyer_friendly_dte(dte, self._p("min_dte"), self._p("max_dte"))

        def _score_bull() -> Tuple[float, List[str]]:
            s = 0.0
            r = []
            if bull_align:
                s += 20; r.append("EMA aligned bullish")
            elif ef > es:
                s += 10; r.append("EMA fast > slow")
            if bull_cross:
                s += 5;  r.append("EMA bull crossover")
            if price_bull:
                s += 5;  r.append(f"Price > EMA{self._p('trend_ema')}")
            if htf_bull:
                s += 5;  r.append("HTF bullish")
            if rsi_bull_strong:
                s += 20; r.append(f"RSI strong ({rsi_val:.1f})")
            elif rsi_bull_mid:
                s += 12; r.append(f"RSI bullish ({rsi_val:.1f})")
            elif rsi_val > 50:
                s += 5
            if macd_ok:
                if macd_bull_cross:
                    s += 20; r.append("MACD hist zero-cross up")
                elif macd_bull:
                    s += 12; r.append(f"MACD hist rising ({macd['histogram']:.2f})")
                elif macd["histogram"] > 0:
                    s += 5
            if bb_ok:
                if bb_bull_strong:
                    s += 15; r.append("Price above BB upper")
                elif bb_bull_break:
                    s += 12; r.append("BB squeeze breakout UP")
                elif bb_bull:
                    s += 7;  r.append(f"BB bullish zone ({bb_pos:.2f})")
            if vol_surge:
                s += 15; r.append(f"Volume surge ({vol_ratio:.1f}x)")
            elif vol_ratio >= 1.0:
                s += 5
            if not dte_ok:
                s -= 15; r.append(f"DTE penalty: {dte_note}")
            elif dte <= 5:
                s += 5;  r.append(f"DTE sweet spot ({dte})")
            return min(s, 100.0), r

        def _score_bear() -> Tuple[float, List[str]]:
            s = 0.0
            r = []
            if bear_align:
                s += 20; r.append("EMA aligned bearish")
            elif ef < es:
                s += 10; r.append("EMA fast < slow")
            if bear_cross:
                s += 5;  r.append("EMA bear crossover")
            if price_bear:
                s += 5;  r.append(f"Price < EMA{self._p('trend_ema')}")
            if htf_bear:
                s += 5;  r.append("HTF bearish")
            if rsi_bear_strong:
                s += 20; r.append(f"RSI strong bearish ({rsi_val:.1f})")
            elif rsi_bear_mid:
                s += 12; r.append(f"RSI bearish ({rsi_val:.1f})")
            elif rsi_val < 50:
                s += 5
            if macd_ok:
                if macd_bear_cross:
                    s += 20; r.append("MACD hist zero-cross down")
                elif macd_bear:
                    s += 12; r.append(f"MACD hist falling ({macd['histogram']:.2f})")
                elif macd["histogram"] < 0:
                    s += 5
            if bb_ok:
                if bb_bear_strong:
                    s += 15; r.append("Price below BB lower")
                elif bb_bear_break:
                    s += 12; r.append("BB squeeze breakout DOWN")
                elif bb_bear:
                    s += 7;  r.append(f"BB bearish zone ({bb_pos:.2f})")
            if vol_surge:
                s += 15; r.append(f"Volume surge ({vol_ratio:.1f}x)")
            elif vol_ratio >= 1.0:
                s += 5
            if not dte_ok:
                s -= 15; r.append(f"DTE penalty: {dte_note}")
            elif dte <= 5:
                s += 5;  r.append(f"DTE sweet spot ({dte})")
            return min(s, 100.0), r

        bull_score, bull_reasons = _score_bull()
        bear_score, bear_reasons = _score_bear()
        min_conf = self._p("min_confidence")

        # ✅ ATR calculate karo — SL/TP ke liye
        atr_val = _atr(main_candles, period=14)

        cfg = NSE_BSE_SYMBOLS[sym]
        metadata = {
            "nse_symbol":     sym,
            "lot_size":       cfg["lot_size"],
            "strike_step":    cfg["strike_step"],
            "ema_fast":       round(ef, 2),
            "ema_slow":       round(es, 2),
            "ema_trend":      round(et, 2),
            "rsi":            rsi_val,
            "macd":           round(macd["macd"], 4) if macd_ok else None,
            "macd_hist":      round(macd["histogram"], 4) if macd_ok else None,
            "bb_pos":         round(bb_pos, 3) if bb_ok else None,
            "bb_width_pct":   bb["width_pct"] if bb_ok else None,
            "bb_squeeze":     bb_squeeze if bb_ok else False,
            "vol_ratio":      round(vol_ratio, 2),
            "vol_surge":      vol_surge,
            "dte":            dte,
            "dte_ok":         dte_ok,
            "bull_score":     round(bull_score, 1),
            "bear_score":     round(bear_score, 1),
            "min_confidence": min_conf,
            "trader_type":    "buyer",
            "atr":            atr_val,
            "spot":           spot,
        }

        # ✅ MOMENTUM HARD GATE — buyer ke liye strong momentum zaroori
        bull_momentum_ok = (
            rsi_val >= 55 and                          # RSI bullish zone
            (not macd_ok or macd_bull or macd_bull_cross) and  # MACD bullish
            (not bb_ok or bb_bull)                     # BB bullish zone
        )
        bear_momentum_ok = (
            rsi_val <= 45 and                          # RSI bearish zone
            (not macd_ok or macd_bear or macd_bear_cross) and  # MACD bearish
            (not bb_ok or bb_bear)                     # BB bearish zone
        )

        if bull_score >= min_conf and bull_score > bear_score and bull_momentum_ok:
            strike = _strike_price(spot, sym, "CE", self._p("otm_shift"))
            metadata["option_type"] = "CE"
            metadata["suggested_strike"] = strike
            metadata["setup_type"] = f"MultiConfirm_CE_{sym}"

            logger.info(
                "✅ MultiConfirmOptions BUY CE | %s | spot=%.2f | strike=%d | "
                "conf=%.1f | RSI=%.1f | DTE=%d",
                sym, spot, strike, bull_score, rsi_val, dte,
            )

            return AlgoSignal(
                signal_type="buy",
                symbol=symbol,
                price=price,
                confidence=bull_score,
                reason=(
                    f"BUY CE | Score: {bull_score:.1f}/{min_conf} | Strike: {strike} | "
                    + " | ".join(bull_reasons[:5])
                ),
                metadata=metadata,
            )

        if bear_score >= min_conf and bear_score > bull_score and bear_momentum_ok:
            strike = _strike_price(spot, sym, "PE", self._p("otm_shift"))
            metadata["option_type"] = "PE"
            metadata["suggested_strike"] = strike
            metadata["setup_type"] = f"MultiConfirm_PE_{sym}"

            logger.info(
                "✅ MultiConfirmOptions BUY PE | %s | spot=%.2f | strike=%d | "
                "conf=%.1f | RSI=%.1f | DTE=%d",
                sym, spot, strike, bear_score, rsi_val, dte,
            )

            return AlgoSignal(
                signal_type="sell",
                symbol=symbol,
                price=price,
                confidence=bear_score,
                reason=(
                    f"BUY PE | Score: {bear_score:.1f}/{min_conf} | Strike: {strike} | "
                    + " | ".join(bear_reasons[:5])
                ),
                metadata=metadata,
            )

        dominant = "BULL" if bull_score > bear_score else "BEAR"
        dom_score = bull_score if dominant == "BULL" else bear_score
        return AlgoSignal(
            signal_type="hold",
            symbol=symbol,
            price=price,
            confidence=dom_score,
            reason=(
                f"No signal | best={dominant} {dom_score:.1f} < {min_conf} | "
                f"RSI={rsi_val:.1f} | BB_squeeze={bb_squeeze if bb_ok else 'N/A'} | "
                f"MACD_hist={macd['histogram']:.3f if macd_ok else 0} | DTE={dte}"
            ),
            metadata=metadata,
            result="skipped",
        )
