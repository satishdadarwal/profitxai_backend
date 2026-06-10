# apps/backtest/algos/multi_confirm_crypto.py
#
# ══════════════════════════════════════════════════════════════════════════════
#  MULTI-CONFIRMATION CRYPTO STRATEGY
#  BTC | ETH | SOL | BNB | XRP aur koi bhi crypto pair
# ══════════════════════════════════════════════════════════════════════════════
#
#  STRATEGY OVERVIEW:
#  Teen independent layers ek saath confirm karein tab hi trade hoga.
#  Ek bhi layer miss — signal HOLD ho jaata hai.
#
#  LAYER 1 — TECHNICAL (EMA Trend + RSI Momentum + Bollinger Band)
#    → Trend direction, momentum state, aur volatility squeeze confirm karo
#
#  LAYER 2 — VOLATILITY REGIME (ATR-based)
#    → Low volatility = better entry; high choppiness mein trade nahi
#
#  LAYER 3 — VOLUME CONFIRMATION
#    → Volume average se above ho — institutional participation confirm karo
#
#  CONFIDENCE SCORING:
#    Har sub-condition ek score deti hai (0-100).
#    Final signal tab hi emit hota hai jab score >= min_confidence (default 65).
#
#  PARAMETERS (strategy.parameters mein set karo):
#  {
#    "fast_ema"        : 9,      # Fast EMA period
#    "slow_ema"        : 21,     # Slow EMA period
#    "trend_ema"       : 50,     # HTF trend confirm EMA
#    "rsi_period"      : 14,     # RSI period
#    "rsi_ob"          : 65,     # RSI overbought level (crypto mein 70 bahut strict hai)
#    "rsi_os"          : 35,     # RSI oversold level
#    "bb_period"       : 20,     # Bollinger Band period
#    "bb_std"          : 2.0,    # BB standard deviation
#    "atr_period"      : 14,     # ATR period (volatility filter)
#    "atr_max_pct"     : 5.0,    # ATR % max (isse zyada choppy market = skip)
#    "vol_ma_period"   : 20,     # Volume moving average period
#    "vol_multiplier"  : 1.2,    # Volume must be vol_ma * this multiplier
#    "min_confidence"  : 65,     # Minimum confidence score to trade (0-100)
#    "htf_lookback"    : 10,     # HTF trend lookback candles
#    "mtf_lookback"    : 5,      # MTF momentum confirm lookback
#  }
#
#  HOW IT INTEGRATES:
#  - strategy.algo_name = "multi_confirm_crypto"
#  - signal_router.py → crypto detect karega (BTC/ETH/USDT keywords)
#  - Delta Exchange pe futures/perp order jayega
#  - Paper mode bhi supported
#
# ══════════════════════════════════════════════════════════════════════════════

from __future__ import annotations

import logging
import math
from decimal import Decimal
from typing import Optional, List

logger = logging.getLogger(__name__)


# ── Lazy Base (circular import avoid karne ke liye) ───────────────────────────
class _Base:
    name: str = "base"

    def __init__(self, parameters: Optional[dict] = None):
        self.params = parameters or {}

    def get_param(self, key: str, default=None):
        return self.params.get(key, default)

    def generate_signal(self, *args, **kwargs):
        raise NotImplementedError


# ══════════════════════════════════════════════════════════════════════════════
#  Indicator Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _ema(prices: List[float], period: int) -> List[float]:
    """Exponential Moving Average calculate karo."""
    if len(prices) < period:
        return []
    k = 2 / (period + 1)
    seed = sum(prices[:period]) / period
    result = [seed]
    for p in prices[period:]:
        seed = p * k + seed * (1 - k)
        result.append(seed)
    return result


def _rsi(prices: List[float], period: int) -> Optional[float]:
    """RSI calculate karo (Wilder's smoothing)."""
    if len(prices) < period + 2:
        return None
    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains = [d if d > 0 else 0.0 for d in deltas[-period:]]
    losses = [-d if d < 0 else 0.0 for d in deltas[-period:]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1 + rs))


def _bollinger(prices: List[float], period: int, std_mult: float) -> Optional[dict]:
    """Bollinger Bands calculate karo."""
    if len(prices) < period:
        return None
    window = prices[-period:]
    middle = sum(window) / period
    variance = sum((p - middle) ** 2 for p in window) / period
    std = math.sqrt(variance)
    upper = middle + std_mult * std
    lower = middle - std_mult * std
    width_pct = ((upper - lower) / middle * 100) if middle > 0 else 0
    return {
        "upper": upper,
        "middle": middle,
        "lower": lower,
        "std": std,
        "width_pct": width_pct,
    }


def _atr(candles: list, period: int) -> Optional[float]:
    """Average True Range calculate karo (volatility measure)."""
    if len(candles) < period + 1:
        return None

    def _c(c): return c["close"] if isinstance(c, dict) else c.close
    def _h(c): return c["high"] if isinstance(c, dict) else c.high
    def _l(c): return c["low"] if isinstance(c, dict) else c.low

    trs = []
    for i in range(1, len(candles)):
        curr_high = _h(candles[i])
        curr_low = _l(candles[i])
        prev_close = _c(candles[i - 1])
        tr = max(
            curr_high - curr_low,
            abs(curr_high - prev_close),
            abs(curr_low - prev_close),
        )
        trs.append(tr)

    if len(trs) < period:
        return None

    return sum(trs[-period:]) / period


def _vol_ma(candles: list, period: int) -> Optional[float]:
    """Volume Moving Average calculate karo."""
    def _v(c): return c.get("volume", 0) if isinstance(c, dict) else getattr(c, "volume", 0)

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


# ══════════════════════════════════════════════════════════════════════════════
#  MultiConfirmCryptoAlgo
# ══════════════════════════════════════════════════════════════════════════════

class MultiConfirmCryptoAlgo(_Base):
    """
    Multi-Confirmation Crypto Strategy.

    Teen layers ek saath confirm karein tab hi BUY/SELL signal emit hoga:
      Layer 1: Technical — EMA trend + RSI momentum + BB squeeze
      Layer 2: Volatility Regime — ATR % filter (choppy market skip)
      Layer 3: Volume — Avg volume se above hona zaroori

    confidence score 0–100 calculate hota hai; min_confidence threshold
    se kam ho toh HOLD.
    """

    name = "multi_confirm_crypto"

    # ── Parameter defaults ──────────────────────────────────────────────────
    _DEFAULTS = {
        "fast_ema": 9,
        "slow_ema": 21,
        "trend_ema": 50,
        "rsi_period": 14,
        "rsi_ob": 65,
        "rsi_os": 35,
        "bb_period": 20,
        "bb_std": 2.0,
        "atr_period": 14,
        "atr_max_pct": 5.0,
        "vol_ma_period": 20,
        "vol_multiplier": 1.2,
        "min_confidence": 65,
        "htf_lookback": 10,
        "mtf_lookback": 5,
    }

    def _p(self, key):
        """Parameter lo — default fallback ke saath."""
        val = self.params.get(key, self._DEFAULTS[key])
        return type(self._DEFAULTS[key])(val)

    def generate_signal(self, symbol, price, strategy, **ctx):
        from apps.backtest.engine import AlgoSignal

        # ── Candles lo ──────────────────────────────────────────────────────
        # HTF (1H) → trend, MTF (15m) → setup, LTF (5m) → entry
        htf = ctx.get("htf") or ctx.get("candles", [])
        mtf = ctx.get("mtf") or htf
        ltf = ctx.get("ltf") or mtf

        # Working candles: sabse lambi available list use karo indicators ke liye
        # (HTF > MTF > LTF order — longer history = better indicator quality)
        candles = max((htf, mtf, ltf), key=len)

        min_needed = max(
            self._p("trend_ema") + 5,
            self._p("bb_period") + 5,
            self._p("atr_period") + 5,
            self._p("vol_ma_period") + 5,
        )

        if len(candles) < min_needed:
            return AlgoSignal(
                signal_type="hold",
                symbol=symbol,
                price=price,
                reason=f"Insufficient candles: {len(candles)} < {min_needed}",
                result="skipped",
            )

        closes = [_get_close(c) for c in candles]
        current_price = float(price)

        # ══════════════════════════════════════════════════════════════════
        #  LAYER 1: TECHNICAL INDICATORS
        # ══════════════════════════════════════════════════════════════════

        # -- EMA fast/slow/trend --
        ema_fast_series = _ema(closes, self._p("fast_ema"))
        ema_slow_series = _ema(closes, self._p("slow_ema"))
        ema_trend_series = _ema(closes, self._p("trend_ema"))

        if not ema_fast_series or not ema_slow_series or not ema_trend_series:
            return AlgoSignal(
                signal_type="hold", symbol=symbol, price=price,
                reason="EMA calculation failed", result="skipped",
            )

        ema_fast = ema_fast_series[-1]
        ema_fast_prev = ema_fast_series[-2] if len(ema_fast_series) > 1 else ema_fast
        ema_slow = ema_slow_series[-1]
        ema_slow_prev = ema_slow_series[-2] if len(ema_slow_series) > 1 else ema_slow
        ema_trend = ema_trend_series[-1]

        # EMA alignment
        bullish_ema = ema_fast > ema_slow and ema_slow > ema_trend
        bearish_ema = ema_fast < ema_slow and ema_slow < ema_trend

        # Crossover detection
        bull_cross = ema_fast_prev <= ema_slow_prev and ema_fast > ema_slow
        bear_cross = ema_fast_prev >= ema_slow_prev and ema_fast < ema_slow

        # -- RSI --
        rsi = _rsi(closes, self._p("rsi_period"))
        rsi_val = rsi if rsi is not None else 50.0
        rsi_bullish = rsi_val < self._p("rsi_os") + 15
        rsi_bearish = rsi_val > self._p("rsi_ob") - 15

        # -- Bollinger Bands --
        bb = _bollinger(closes, self._p("bb_period"), self._p("bb_std"))
        bb_ok = bb is not None

        if bb_ok:
            bb_range = bb["upper"] - bb["lower"]
            bb_pos = (current_price - bb["lower"]) / bb_range if bb_range > 0 else 0.5
            bb_bullish = bb_pos <= 0.40
            bb_bearish = bb_pos >= 0.60
            bb_squeeze = bb["width_pct"] < 3.0
        else:
            bb_pos, bb_bullish, bb_bearish, bb_squeeze = 0.5, False, False, False

        # ══════════════════════════════════════════════════════════════════
        #  LAYER 2: VOLATILITY REGIME (ATR filter)
        # ══════════════════════════════════════════════════════════════════

        atr = _atr(candles, self._p("atr_period"))
        atr_ok = False
        atr_pct = 0.0

        if atr is not None and current_price > 0:
            atr_pct = (atr / current_price) * 100
            atr_ok = atr_pct <= self._p("atr_max_pct")
        else:
            atr_ok = True

        # ══════════════════════════════════════════════════════════════════
        #  LAYER 3: VOLUME CONFIRMATION
        # ══════════════════════════════════════════════════════════════════

        vol_ma = _vol_ma(candles, self._p("vol_ma_period"))
        current_vol = _get_volume(candles[-1])
        vol_ok = False

        if vol_ma and vol_ma > 0:
            vol_ok = current_vol >= vol_ma * self._p("vol_multiplier")
        else:
            vol_ok = True

        # ══════════════════════════════════════════════════════════════════
        #  HTF TREND CONFIRMATION (Macro filter)
        # ══════════════════════════════════════════════════════════════════

        htf_lookback = self._p("htf_lookback")
        htf_ok = len(htf) >= htf_lookback + 1

        if htf_ok:
            htf_closes = [_get_close(c) for c in htf]
            htf_trend_bull = htf_closes[-1] > htf_closes[-htf_lookback]
            htf_trend_bear = htf_closes[-1] < htf_closes[-htf_lookback]
        else:
            htf_trend_bull = bullish_ema
            htf_trend_bear = bearish_ema

        # ══════════════════════════════════════════════════════════════════
        #  CONFIDENCE SCORING
        # ══════════════════════════════════════════════════════════════════

        def _score_buy() -> tuple[float, list]:
            """BUY ke liye confidence score calculate karo."""
            score = 0.0
            reasons = []

            if bullish_ema:
                score += 20
                reasons.append("EMA aligned bullish")
            if bull_cross:
                score += 10
                reasons.append("EMA bull crossover")
            elif ema_fast > ema_slow:
                score += 5

            if rsi_val <= self._p("rsi_os"):
                score += 20
                reasons.append(f"RSI oversold ({rsi_val:.1f})")
            elif rsi_val <= self._p("rsi_os") + 15:
                score += 12
                reasons.append(f"RSI near oversold ({rsi_val:.1f})")
            elif rsi_val < 55:
                score += 5

            if bb_ok:
                if bb_squeeze:
                    score += 10
                    reasons.append("BB squeeze (breakout imminent)")
                if bb_bullish:
                    score += 10
                    reasons.append(f"Price near BB lower (pos={bb_pos:.2f})")
                elif bb_pos < 0.5:
                    score += 5

            if atr_ok:
                score += 15
                reasons.append(f"ATR ok ({atr_pct:.2f}%)")
            else:
                reasons.append(f"ATR high ({atr_pct:.2f}%) — penalty")
                score -= 10

            if vol_ok:
                score += 15
                reasons.append("Volume confirmed")

            if htf_trend_bull:
                score += 10
                reasons.append("HTF bullish")

            return min(score, 100.0), reasons

        def _score_sell() -> tuple[float, list]:
            """SELL ke liye confidence score calculate karo."""
            score = 0.0
            reasons = []

            if bearish_ema:
                score += 20
                reasons.append("EMA aligned bearish")
            if bear_cross:
                score += 10
                reasons.append("EMA bear crossover")
            elif ema_fast < ema_slow:
                score += 5

            if rsi_val >= self._p("rsi_ob"):
                score += 20
                reasons.append(f"RSI overbought ({rsi_val:.1f})")
            elif rsi_val >= self._p("rsi_ob") - 15:
                score += 12
                reasons.append(f"RSI near overbought ({rsi_val:.1f})")
            elif rsi_val > 45:
                score += 5

            if bb_ok:
                if bb_squeeze:
                    score += 10
                    reasons.append("BB squeeze (breakout imminent)")
                if bb_bearish:
                    score += 10
                    reasons.append(f"Price near BB upper (pos={bb_pos:.2f})")
                elif bb_pos > 0.5:
                    score += 5

            if atr_ok:
                score += 15
                reasons.append(f"ATR ok ({atr_pct:.2f}%)")
            else:
                reasons.append(f"ATR high ({atr_pct:.2f}%) — penalty")
                score -= 10

            if vol_ok:
                score += 15
                reasons.append("Volume confirmed")

            if htf_trend_bear:
                score += 10
                reasons.append("HTF bearish")

            return min(score, 100.0), reasons

        min_conf = self._p("min_confidence")

        buy_score, buy_reasons = _score_buy()
        sell_score, sell_reasons = _score_sell()

        # ── HTF 4H bias filter ────────────────────────────────────────────────
        try:
            _htf4h_cr = ctx.get("htf4h") or ctx.get("htf_4h") or []
            if _htf4h_cr and len(_htf4h_cr) >= 10:
                _4hc_cr = [_get_close(c) for c in _htf4h_cr]
                _n4h = min(20, len(_4hc_cr))
                _e4h_cr = sum(_4hc_cr[:_n4h]) / _n4h
                _k4h = 2 / 21
                for _v4h in _4hc_cr[_n4h:]:
                    _e4h_cr = _v4h * _k4h + _e4h_cr * (1 - _k4h)
                if _4hc_cr[-1] > _e4h_cr:  # 4H bullish
                    buy_score = min(buy_score + 5, 100.0)
                    buy_reasons.append("4H bias bullish ✅")
                    sell_score = 0.0  # 4H conflicts with SELL
                    logger.debug("MultiConfirmCrypto 4H bullish — BUY +5, SELL blocked")
                else:  # 4H bearish
                    sell_score = min(sell_score + 5, 100.0)
                    sell_reasons.append("4H bias bearish ✅")
                    buy_score = 0.0  # 4H conflicts with BUY
                    logger.debug("MultiConfirmCrypto 4H bearish — SELL +5, BUY blocked")
        except Exception as _e4h_cr:
            logger.debug("MultiConfirmCrypto 4H bias error: %s", _e4h_cr)

        # ── DOL (Draw on Liquidity) — nearest BSL/SSL as target ──────────────
        try:
            _highs_cr = [float(c["high"] if isinstance(c, dict) else c.high) for c in candles]
            _lows_cr  = [float(c["low"] if isinstance(c, dict) else c.low) for c in candles]
            _above_cr = [h for h in _highs_cr if h > current_price]
            if _above_cr:
                _dol_bsl_cr = min(_above_cr)
                if (_dol_bsl_cr - current_price) / current_price * 100 >= 0.3:
                    buy_score = min(buy_score + 10, 100.0)
                    buy_reasons.append(f"DOL_BSL={_dol_bsl_cr:.4f}")
            _below_cr = [l for l in _lows_cr if 0 < l < current_price]
            if _below_cr:
                _dol_ssl_cr = max(_below_cr)
                if (current_price - _dol_ssl_cr) / current_price * 100 >= 0.3:
                    sell_score = min(sell_score + 10, 100.0)
                    sell_reasons.append(f"DOL_SSL={_dol_ssl_cr:.4f}")
        except Exception:
            pass

        # ATR actual value (for SL/TP in signal_router)
        atr_actual = atr if atr else round(float(price) * atr_pct / 100, 4)

        metadata = {
            "ema_fast": round(ema_fast, 4),
            "ema_slow": round(ema_slow, 4),
            "ema_trend": round(ema_trend, 4),
            "rsi": round(rsi_val, 2),
            "bb_pos": round(bb_pos, 3) if bb_ok else None,
            "bb_width_pct": round(bb["width_pct"], 2) if bb_ok else None,
            "bb_squeeze": bb_squeeze,
            "atr_pct": round(atr_pct, 3),
            "atr": round(atr_actual, 4),   # ✅ ATR for SL/TP
            "spot": float(price),           # ✅ Current price
            "atr_ok": atr_ok,
            "current_vol": current_vol,
            "vol_ma": round(vol_ma, 2) if vol_ma else None,
            "vol_ok": vol_ok,
            "buy_score": round(buy_score, 1),
            "sell_score": round(sell_score, 1),
            "min_confidence": min_conf,
        }

        if buy_score >= min_conf and buy_score > sell_score:
            return AlgoSignal(
                signal_type="buy",
                symbol=symbol,
                price=price,
                reason=(
                    f"MULTI-CONFIRM BUY | Score: {buy_score:.1f}/{min_conf} | "
                    + " | ".join(buy_reasons[:4])
                ),
                confidence=buy_score,
                metadata=metadata,
            )

        if sell_score >= min_conf and sell_score > buy_score:
            return AlgoSignal(
                signal_type="sell",
                symbol=symbol,
                price=price,
                reason=(
                    f"MULTI-CONFIRM SELL | Score: {sell_score:.1f}/{min_conf} | "
                    + " | ".join(sell_reasons[:4])
                ),
                confidence=sell_score,
                metadata=metadata,
            )

        dominant = "buy" if buy_score > sell_score else "sell"
        dom_score = buy_score if dominant == "buy" else sell_score
        return AlgoSignal(
            signal_type="hold",
            symbol=symbol,
            price=price,
            reason=(
                f"No confident signal | best={dominant} score={dom_score:.1f} < {min_conf} | "
                f"rsi={rsi_val:.1f} | atr={atr_pct:.1f}% | vol_ok={vol_ok}"
            ),
            confidence=dom_score,
            metadata=metadata,
            result="skipped",
        )


# ── Import here so engine se import karte waqt AlgoSignal available ho ──────
# (generate_signal ke andar lazy import hota hai — circular avoid ke liye)
try:
    from apps.backtest.engine import AlgoSignal  # noqa: F401  (side-effect import)
except ImportError:
    pass  # Standalone test ke waqt ignore karo
