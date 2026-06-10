# apps/backtest/algos/nse_option_seller.py
#
# ══════════════════════════════════════════════════════════════════════════════
#  NSE OPTION SELLER STRATEGY — All NSE Indices
#  NIFTY | BANKNIFTY | FINNIFTY | MIDCPNIFTY | SENSEX | BANKEX
# ══════════════════════════════════════════════════════════════════════════════
#
#  STRATEGY OVERVIEW:
#  Option Seller hamesha time decay (theta) se profit karta hai.
#  Hum OTM options SELL karte hain jab:
#    - Market range-bound ho (low VIX)
#    - Strong trend ke against sell karo (mean reversion)
#    - Expiry ke kareeb ho (theta decay fastest hota hai)
#
#  ALGO LOGIC (Multi-filter approach):
#  1. TREND FILTER    — HTF (1H) trend identify karo
#  2. MOMENTUM FILTER — RSI overbought/oversold check
#  3. BB FILTER       — Bollinger Bands ke edge pe sell karo
#  4. EXPIRY FILTER   — Thursday/Monday ke kareeb zyada agressive
#  5. SIGNAL EMIT     — Trend direction ke against OTM sell signal
#
#  RISK CONFIG (strategy.risk_config mein set karo):
#  {
#    "trader_type": "seller",          # MUST be "seller"
#    "sl_pct": 50,                      # Option premium ka 50% SL (seller ke liye zyada)
#    "target_pct": 30,                  # 30% premium decay = profit book
#    "qty": 1,                          # Default lots
#    "otm_shift": 1,                    # ATM se kitne strikes OTM (0=ATM, 1=1 step OTM)
#    "rsi_period": 14,
#    "rsi_ob": 70,                      # Overbought — sell CE (bearish fade)
#    "rsi_os": 30,                      # Oversold   — sell PE (bullish fade)
#    "bb_period": 20,
#    "bb_std": 2.0,
#    "min_confidence": 55,              # Minimum confidence score
#    "expiry_boost": True,              # Expiry week mein zyada signals
#  }
#
#  HOW IT INTEGRATES:
#  - signal_router.py ka _paper_option_trade() → action="sell" route karta hai
#    jab strategy.risk_config["trader_type"] = "seller"
#  - Fyers live trading: _fyers_options_order() — side=1 (buy) as short hedge NOT used
#    For actual shorting, broker_adapter se SELL order jayega
#  - SL/TP automatically inverted for sellers in signal_router.py
#
# ══════════════════════════════════════════════════════════════════════════════

from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from apps.backtest.engine import AlgoSignal

logger = logging.getLogger(__name__)


# ── Lazy Base ─────────────────────────────────────────────────────
# Circular import fix: BaseAlgo ko directly inherit nahi karte
# ABC ki jagah simple base use karo — engine se independent
class _Base:
    name: str = "base"

    def __init__(self, parameters: Optional[dict] = None):
        self.params = parameters or {}

    def get_param(self, key: str, default=None):
        return self.params.get(key, default)

    def generate_signal(self, *args, **kwargs):
        raise NotImplementedError


# ─────────────────────────────────────────────────────────────────
#  NSE Symbol Config (April 2025 revised lot sizes)
# ─────────────────────────────────────────────────────────────────
NSE_SYMBOLS = {
    "NIFTY":      {"lot_size": 65,  "strike_step": 50,  "exchange": "NSE"},
    "BANKNIFTY":  {"lot_size": 30,  "strike_step": 100, "exchange": "NSE"},
    "FINNIFTY":   {"lot_size": 60,  "strike_step": 50,  "exchange": "NSE"},
    "MIDCPNIFTY": {"lot_size": 120, "strike_step": 25,  "exchange": "NSE"},
    "SENSEX":     {"lot_size": 10,  "strike_step": 100, "exchange": "BSE"},
    "BANKEX":     {"lot_size": 15,  "strike_step": 100, "exchange": "BSE"},
}


# ─────────────────────────────────────────────────────────────────
#  Technical Indicators (pure Python — no pandas dependency)
# ─────────────────────────────────────────────────────────────────

def _ema(values: list[float], period: int) -> list[float]:
    """Exponential Moving Average"""
    if len(values) < period:
        return []
    k = 2 / (period + 1)
    result = [sum(values[:period]) / period]  # SMA as seed
    for v in values[period:]:
        result.append(v * k + result[-1] * (1 - k))
    return result


def _rsi(closes: list[float], period: int = 14) -> float:
    """RSI — Relative Strength Index (last value)"""
    if len(closes) < period + 1:
        return 50.0  # Neutral fallback

    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains  = [max(d, 0) for d in deltas]
    losses = [abs(min(d, 0)) for d in deltas]

    # Initial average
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    # Wilder's smoothing
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def _bollinger(closes: list[float], period: int = 20, std_dev: float = 2.0) -> dict:
    """Bollinger Bands — returns upper, middle, lower"""
    if len(closes) < period:
        last = closes[-1] if closes else 0
        return {"upper": last * 1.02, "middle": last, "lower": last * 0.98, "width_pct": 2.0}

    window = closes[-period:]
    middle = sum(window) / period
    variance = sum((x - middle) ** 2 for x in window) / period
    std = variance ** 0.5

    upper = middle + std_dev * std
    lower = middle - std_dev * std
    width_pct = ((upper - lower) / middle) * 100 if middle > 0 else 2.0

    return {
        "upper": round(upper, 2),
        "middle": round(middle, 2),
        "lower": round(lower, 2),
        "width_pct": round(width_pct, 2),
    }


def _atr(candles: list, period: int = 14) -> float:
    """Average True Range — volatility measure"""
    if len(candles) < period + 1:
        return 0.0

    def _get(c, key):
        return c[key] if isinstance(c, dict) else getattr(c, key)

    trs = []
    for i in range(1, len(candles)):
        h = _get(candles[i], "high")
        l = _get(candles[i], "low")
        pc = _get(candles[i - 1], "close")
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)

    return round(sum(trs[-period:]) / period, 2)


# ─────────────────────────────────────────────────────────────────
#  Expiry Helper
# ─────────────────────────────────────────────────────────────────

def _days_to_expiry(symbol: str) -> int:
    """
    Current day se nearest expiry kitna dur hai.
    NIFTY/BANKNIFTY/FINNIFTY — weekly Thursday expiry
    SENSEX/BANKEX             — weekly Friday expiry
    MIDCPNIFTY                — monthly last Thursday
    """
    today = date.today()
    weekday = today.weekday()  # 0=Mon ... 6=Sun

    if symbol in ("SENSEX", "BANKEX"):
        # Friday expiry
        days_ahead = (4 - weekday) % 7
    elif symbol == "MIDCPNIFTY":
        # Monthly last Thursday — approximate: next Thursday + check if last of month
        days_ahead = (3 - weekday) % 7
        # If today IS Thursday, check if it's last Thursday
        if days_ahead == 0:
            next_thu = today + timedelta(days=7)
            if next_thu.month != today.month:
                days_ahead = 0  # Today is last Thursday
            else:
                days_ahead = 7  # Not last Thursday, but still use weekly
    else:
        # Weekly Thursday expiry (NIFTY, BANKNIFTY, FINNIFTY)
        days_ahead = (3 - weekday) % 7

    return days_ahead if days_ahead > 0 else 7  # At least 1 week


def _expiry_boost_factor(days_to_exp: int) -> float:
    """
    Expiry ke kareeb theta decay tezi se hota hai.
    Signal confidence boost:
      0-1 days: 1.3x  (expiry day / day before)
      2-3 days: 1.15x
      4+ days:  1.0x  (normal)
    """
    if days_to_exp <= 1:
        return 1.3
    elif days_to_exp <= 3:
        return 1.15
    return 1.0


# ─────────────────────────────────────────────────────────────────
#  OTM Strike Calculator
# ─────────────────────────────────────────────────────────────────

def _otm_strike(spot: float, symbol: str, option_type: str, otm_shift: int = 1) -> int:
    """
    ATM se otm_shift steps OTM strike calculate karo.

    CE sell karte hain (bearish setup) → strike ABOVE spot
    PE sell karte hain (bullish setup) → strike BELOW spot

    otm_shift=0 → ATM sell (aggressive)
    otm_shift=1 → 1 strike OTM (standard)
    otm_shift=2 → 2 strikes OTM (conservative)
    """
    cfg = NSE_SYMBOLS.get(symbol.upper(), {"strike_step": 50})
    step = cfg["strike_step"]
    atm = round(spot / step) * step

    if option_type == "CE":
        return int(atm + otm_shift * step)  # Above spot for CE sell
    else:
        return int(atm - otm_shift * step)  # Below spot for PE sell


# ─────────────────────────────────────────────────────────────────
#  MAIN STRATEGY CLASS
# ─────────────────────────────────────────────────────────────────

class NseOptionSellerAlgo(_Base):
    """
    NSE Option Seller — All Index Strategy

    Supported: NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, SENSEX, BANKEX

    Entry Logic:
      SELL CE when:
        - HTF trend is BEARISH (price below EMA 20 on 1H)
        - RSI > rsi_ob (overbought — likely to fall)
        - Price near / above BB upper band
        - Days to expiry <= 4 (preferred, boost only)

      SELL PE when:
        - HTF trend is BULLISH (price above EMA 20 on 1H)
        - RSI < rsi_os (oversold — likely to bounce)
        - Price near / below BB lower band
        - Days to expiry <= 4 (preferred, boost only)

    The signal_router.py already handles seller SL/TP inversion
    (seller mein SL is HIGHER, TP is LOWER — already coded in
    _paper_option_trade() and _calculate_sl/_calculate_tp).

    parameters (strategy.parameters JSON mein set karo):
      rsi_period  : 14   — RSI calculation period
      rsi_ob      : 70   — Overbought threshold (sell CE)
      rsi_os      : 30   — Oversold threshold (sell PE)
      bb_period   : 20   — Bollinger Band period
      bb_std      : 2.0  — BB standard deviation
      ema_period  : 20   — EMA for HTF trend
      otm_shift   : 1    — OTM strikes from ATM
      min_confidence : 55 — Min score to emit signal
      expiry_boost : true — Thursday me zyada aggressive

    risk_config (strategy.risk_config mein):
      trader_type : "seller"  ← CRITICAL
      sl_pct      : 50        ← Seller SL 50% above entry
      target_pct  : 30        ← Seller TP 30% below entry
    """

    name = "nse_option_seller"

    def __init__(self, parameters: Optional[dict] = None):
        super().__init__(parameters or {})
        self.rsi_period    = int(self.params.get("rsi_period", 14))
        self.rsi_ob        = float(self.params.get("rsi_ob", 70))
        self.rsi_os        = float(self.params.get("rsi_os", 30))
        self.bb_period     = int(self.params.get("bb_period", 20))
        self.bb_std        = float(self.params.get("bb_std", 2.0))
        self.ema_period    = int(self.params.get("ema_period", 20))
        self.otm_shift     = int(self.params.get("otm_shift", 1))
        self.min_conf      = float(self.params.get("min_confidence", 55))
        self.expiry_boost  = bool(self.params.get("expiry_boost", True))

    # ── Core Signal Generator ─────────────────────────────────────

    def generate_signal(
        self,
        symbol: str,
        price: Decimal,
        strategy: object,
        candles=None,   # LTF candles (default timeframe)
        htf=None,       # 1H candles (for trend)
        mtf=None,       # 15min (optional)
        ltf=None,       # 5min (optional, alias for candles)
        quote=None,
        **ctx,
    ) -> "AlgoSignal":
        """
        Main signal generation — multi-filter approach.
        """
        from apps.backtest.engine import AlgoSignal  # lazy import — circular avoid

        spot = float(price)
        sym  = self._clean_symbol(symbol)

        # ── Guard: Symbol valid hai? ──────────────────────────────
        if sym not in NSE_SYMBOLS:
            logger.warning(
                "NseOptionSeller: Unknown symbol '%s' — skipping", symbol
            )
            return AlgoSignal(
                signal_type="hold", symbol=symbol, price=price,
                reason=f"Symbol '{sym}' not in NSE list", result="skipped",
            )

        # ── Guard: Enough candles? ────────────────────────────────
        # Prefer htf for trend, fallback to candles
        trend_candles = htf or candles or ltf or []
        analysis_candles = candles or ltf or htf or []

        min_required = max(self.bb_period, self.ema_period, self.rsi_period + 2)
        if len(analysis_candles) < min_required:
            return AlgoSignal(
                signal_type="hold", symbol=symbol, price=price,
                reason=f"Insufficient candles: {len(analysis_candles)} < {min_required}",
                result="skipped",
            )

        # ── 1. TREND FILTER (HTF) ─────────────────────────────────
        trend, trend_score = self._trend_filter(trend_candles, spot)

        # ── 2. RSI FILTER ─────────────────────────────────────────
        closes = self._extract_closes(analysis_candles)
        rsi = _rsi(closes, self.rsi_period)
        rsi_signal, rsi_score = self._rsi_filter(rsi)

        # ── 3. BOLLINGER BAND FILTER ──────────────────────────────
        bb = _bollinger(closes, self.bb_period, self.bb_std)
        bb_signal, bb_score = self._bb_filter(spot, bb)

        # ── 4. EXPIRY BOOST ───────────────────────────────────────
        dte         = _days_to_expiry(sym)
        exp_factor  = _expiry_boost_factor(dte) if self.expiry_boost else 1.0
        expiry_note = f"DTE={dte}" + (" [EXPIRY WEEK BOOST]" if exp_factor > 1.0 else "")

        # ── 5. CONSENSUS — do signals agree? ─────────────────────
        # CE sell: bearish trend + overbought RSI + near BB upper
        # PE sell: bullish trend + oversold RSI  + near BB lower
        signal_type, confidence, option_type, reason = self._consensus(
            trend, trend_score,
            rsi, rsi_signal, rsi_score,
            bb_signal, bb_score,
            spot, bb,
            sym, dte, exp_factor,
        )

        # ── 5b. HTF 4H bias filter ────────────────────────────────
        try:
            _htf4h_sel = ctx.get("htf4h") or ctx.get("htf_4h") or []
            if _htf4h_sel and len(_htf4h_sel) >= 10 and signal_type != "hold":
                _4hc_sel = self._extract_closes(_htf4h_sel)
                _ema4h_sel = _ema(_4hc_sel, 20)
                if _ema4h_sel:
                    _4h_bull_sel = _4hc_sel[-1] > _ema4h_sel[-1]
                    # CE sell (bearish signal): 4H must NOT be bullish
                    if option_type == "CE" and _4h_bull_sel:
                        logger.info("NseOptionSeller 4H bullish conflicts with CE sell — skip")
                        signal_type = "hold"
                        confidence = 0.0
                        reason = "4H bias bullish conflicts with CE sell"
                    # PE sell (bullish signal): 4H must NOT be bearish
                    elif option_type == "PE" and not _4h_bull_sel:
                        logger.info("NseOptionSeller 4H bearish conflicts with PE sell — skip")
                        signal_type = "hold"
                        confidence = 0.0
                        reason = "4H bias bearish conflicts with PE sell"
                    else:
                        confidence = min(confidence + 5, 99.0)
                        reason += " | 4H_aligned ✅"
                        logger.debug("NseOptionSeller 4H ✅ aligned | %s", option_type)
        except Exception as _e4h_sel:
            logger.debug("NseOptionSeller 4H bias error: %s", _e4h_sel)

        # ── 6. Confidence threshold check ────────────────────────
        if signal_type == "hold" or confidence < self.min_conf:
            return AlgoSignal(
                signal_type="hold",
                symbol=symbol,
                price=price,
                confidence=confidence,
                reason=f"Below confidence threshold ({confidence:.1f} < {self.min_conf}) | {reason}",
                result="skipped",
            )

        # ── 7. Calculate OTM strike for metadata ─────────────────
        otm_strike = _otm_strike(spot, sym, option_type, self.otm_shift)
        cfg = NSE_SYMBOLS[sym]

        logger.info(
            "✅ NseOptionSeller SIGNAL | %s | %s %s | spot=%.2f | strike=%d | "
            "RSI=%.1f | BB_pos=%.1f%% | confidence=%.1f | %s",
            sym, signal_type.upper(), option_type,
            spot, otm_strike, rsi,
            ((spot - bb["lower"]) / (bb["upper"] - bb["lower"]) * 100),
            confidence, expiry_note,
        )

        return AlgoSignal(
            signal_type=signal_type,  # 'buy' or 'sell' (signal_router interprets this)
            symbol=symbol,
            price=price,
            confidence=confidence,
            reason=reason,
            metadata={
                # ── Core seller metadata ──────────────────────────
                "trader_type":    "seller",
                "option_type":    option_type,      # 'CE' or 'PE' being SOLD
                "otm_strike":     otm_strike,        # Recommended strike
                "otm_shift":      self.otm_shift,
                # ── Market context ────────────────────────────────
                "spot":           spot,
                "rsi":            rsi,
                "bb_upper":       bb["upper"],
                "bb_lower":       bb["lower"],
                "bb_middle":      bb["middle"],
                "bb_width_pct":   bb["width_pct"],
                "trend":          trend,
                "days_to_expiry": dte,
                "expiry_factor":  exp_factor,
                # ── Symbol info ───────────────────────────────────
                "lot_size":       cfg["lot_size"],
                "strike_step":    cfg["strike_step"],
                "nse_symbol":     sym,
                # ── Risk setup (signal_router uses these) ─────────
                "setup_type":     f"OptionSell_{option_type}_{sym}",
            },
        )

    # ── Filter Methods ────────────────────────────────────────────

    def _trend_filter(self, candles: list, spot: float) -> tuple[str, float]:
        """
        HTF EMA trend direction + score.
        Returns: (trend: 'bullish'|'bearish'|'sideways', score: 0-30)
        """
        closes = self._extract_closes(candles)

        if len(closes) < self.ema_period:
            return "sideways", 0.0

        emas = _ema(closes, self.ema_period)
        if not emas:
            return "sideways", 0.0

        current_ema = emas[-1]
        prev_ema    = emas[-2] if len(emas) >= 2 else current_ema

        # Price vs EMA position
        price_above = spot > current_ema
        ema_rising  = current_ema > prev_ema

        # Distance from EMA (%)
        distance_pct = abs(spot - current_ema) / current_ema * 100

        if price_above and ema_rising:
            trend = "bullish"
            score = min(30, 15 + distance_pct * 3)  # Strong = higher score
        elif not price_above and not ema_rising:
            trend = "bearish"
            score = min(30, 15 + distance_pct * 3)
        elif price_above and not ema_rising:
            trend = "bullish"   # Price above but EMA falling — weak bullish
            score = min(20, 10 + distance_pct * 2)
        elif not price_above and ema_rising:
            trend = "bearish"   # Price below but EMA rising — weak bearish
            score = min(20, 10 + distance_pct * 2)
        else:
            trend = "sideways"
            score = 5.0

        return trend, round(score, 1)

    def _rsi_filter(self, rsi: float) -> tuple[str, float]:
        """
        RSI signal + score (0-35).
        CE sell (bearish): high RSI = seller advantage
        PE sell (bullish): low RSI  = seller advantage
        """
        if rsi >= self.rsi_ob:
            # Overbought → sell CE
            excess = rsi - self.rsi_ob
            score  = min(35, 20 + excess * 0.5)
            return "overbought", round(score, 1)
        elif rsi <= self.rsi_os:
            # Oversold → sell PE
            deficit = self.rsi_os - rsi
            score   = min(35, 20 + deficit * 0.5)
            return "oversold", round(score, 1)
        elif rsi >= 60:
            # Mildly overbought
            return "mild_ob", 12.0
        elif rsi <= 40:
            # Mildly oversold
            return "mild_os", 12.0
        else:
            # Neutral zone — seller ka weak signal
            return "neutral", 5.0

    def _bb_filter(self, spot: float, bb: dict) -> tuple[str, float]:
        """
        BB position signal + score (0-35).
        Near upper band → CE sell zone
        Near lower band → PE sell zone
        """
        upper  = bb["upper"]
        lower  = bb["lower"]
        middle = bb["middle"]
        band_range = upper - lower if upper > lower else 1.0

        # Normalized position: 0% = lower, 100% = upper
        position_pct = (spot - lower) / band_range * 100

        if spot >= upper:
            # Above BB upper — strong CE sell zone
            overshoot = (spot - upper) / band_range * 100
            score = min(35, 28 + overshoot * 2)
            return "above_upper", round(score, 1)
        elif position_pct >= 80:
            # Near upper (top 20% of band)
            score = 20 + (position_pct - 80) * 0.75
            return "near_upper", round(score, 1)
        elif spot <= lower:
            # Below BB lower — strong PE sell zone
            overshoot = (lower - spot) / band_range * 100
            score = min(35, 28 + overshoot * 2)
            return "below_lower", round(score, 1)
        elif position_pct <= 20:
            # Near lower (bottom 20% of band)
            score = 20 + (20 - position_pct) * 0.75
            return "near_lower", round(score, 1)
        else:
            # Middle of band — neutral for sellers
            dist_from_middle = abs(position_pct - 50)
            score = max(3, dist_from_middle * 0.2)
            return "middle", round(score, 1)

    def _consensus(
        self,
        trend: str, trend_score: float,
        rsi: float, rsi_signal: str, rsi_score: float,
        bb_signal: str, bb_score: float,
        spot: float, bb: dict,
        sym: str, dte: int, exp_factor: float,
    ) -> tuple[str, float, str, str]:
        """
        Sab filters ko combine karo → final signal.

        Returns: (signal_type, confidence, option_type, reason)

        signal_type: 'buy' (signal_router CE sell ke liye) ya 'sell' (PE sell ke liye)
          NOTE: signal_router.py mein:
            direction="buy"  → seller → sells PE  (bullish fade)
            direction="sell" → seller → sells CE  (bearish fade)
          Isliye:
            CE sell = signal_type="sell" (bearish direction)
            PE sell = signal_type="buy"  (bullish direction)
        """
        # ── CE SELL scoring (bearish scenario) ───────────────────
        ce_score = 0.0
        ce_reasons = []

        if trend in ("bearish",):
            ce_score += trend_score
            ce_reasons.append(f"HTF Bearish(+{trend_score:.0f})")
        elif trend == "bullish":
            # Selling CE against bullish trend = contrarian, lower score
            ce_score += trend_score * 0.3
            ce_reasons.append(f"Contrarian CE sell({trend_score * 0.3:.0f})")

        if rsi_signal in ("overbought", "mild_ob"):
            ce_score += rsi_score
            ce_reasons.append(f"RSI OB {rsi:.1f}(+{rsi_score:.0f})")

        if bb_signal in ("above_upper", "near_upper"):
            ce_score += bb_score
            ce_reasons.append(f"BB Upper(+{bb_score:.0f})")

        # ── PE SELL scoring (bullish scenario) ───────────────────
        pe_score = 0.0
        pe_reasons = []

        if trend in ("bullish",):
            pe_score += trend_score
            pe_reasons.append(f"HTF Bullish(+{trend_score:.0f})")
        elif trend == "bearish":
            pe_score += trend_score * 0.3
            pe_reasons.append(f"Contrarian PE sell({trend_score * 0.3:.0f})")

        if rsi_signal in ("oversold", "mild_os"):
            pe_score += rsi_score
            pe_reasons.append(f"RSI OS {rsi:.1f}(+{rsi_score:.0f})")

        if bb_signal in ("below_lower", "near_lower"):
            pe_score += bb_score
            pe_reasons.append(f"BB Lower(+{bb_score:.0f})")

        # ── Expiry boost apply karo ───────────────────────────────
        ce_score *= exp_factor
        pe_score *= exp_factor
        if exp_factor > 1.0:
            ce_reasons.append(f"ExpiryBoost×{exp_factor}")
            pe_reasons.append(f"ExpiryBoost×{exp_factor}")

        # ── Winner decide karo ────────────────────────────────────
        max_possible = (30 + 35 + 35) * exp_factor  # trend + RSI + BB max
        ce_conf = min(99, ce_score / max_possible * 100)
        pe_conf = min(99, pe_score / max_possible * 100)

        if ce_conf > pe_conf and ce_conf >= self.min_conf:
            # Sell CE (bearish direction signal)
            reason = "CE SELL: " + " | ".join(ce_reasons)
            return "sell", round(ce_conf, 1), "CE", reason

        elif pe_conf >= self.min_conf:
            # Sell PE (bullish direction signal)
            reason = "PE SELL: " + " | ".join(pe_reasons)
            return "buy", round(pe_conf, 1), "PE", reason

        # Neither meets threshold
        best_conf = max(ce_conf, pe_conf)
        return "hold", round(best_conf, 1), "CE", (
            f"Low confidence: CE={ce_conf:.1f} PE={pe_conf:.1f} | "
            f"RSI={rsi:.1f} | BB={bb_signal} | Trend={trend}"
        )

    # ── Helpers ───────────────────────────────────────────────────

    def _extract_closes(self, candles: list) -> list[float]:
        """Candle list se close prices extract karo (dict ya object dono support)."""
        result = []
        for c in candles:
            if isinstance(c, dict):
                result.append(float(c.get("close", 0)))
            else:
                result.append(float(getattr(c, "close", 0)))
        return result

    def _clean_symbol(self, symbol: str) -> str:
        """NSE:NIFTY50-INDEX → NIFTY"""
        raw = symbol.upper().strip()
        for prefix in ("NSE:", "BSE:", "DELTA:"):
            if raw.startswith(prefix):
                raw = raw[len(prefix):]
        SYMBOL_MAP = {
            "NIFTY50-INDEX":    "NIFTY",
            "NIFTY50":          "NIFTY",
            "NIFTYBANK-INDEX":  "BANKNIFTY",
            "NIFTYBANK":        "BANKNIFTY",
            "FINNIFTY-INDEX":   "FINNIFTY",
            "MIDCPNIFTY-INDEX": "MIDCPNIFTY",
            "SENSEX-INDEX":     "SENSEX",
            "BANKEX-INDEX":     "BANKEX",
        }
        return SYMBOL_MAP.get(raw, raw)


# ─────────────────────────────────────────────────────────────────
#  Register — lazy import se circular import avoid hota hai
# ─────────────────────────────────────────────────────────────────
def _register():
    from apps.backtest.engine import register
    register("nse_option_seller", NseOptionSellerAlgo)


_register()

logger.info("✅ NseOptionSellerAlgo registered as 'nse_option_seller'")