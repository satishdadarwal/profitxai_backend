"""
Confluence Options Buyer Strategy
==================================
Silver Bullet (ICT sweep+MSS) + Multi Confirm (RSI/MACD/BB scoring)
= High confidence CE/PE buy

Signal fire hoga sirf jab:
1. Silver Bullet sweep + MSS detected
2. Multi Confirm score >= min_confidence
3. Momentum aligned (RSI/MACD both confirm)
4. ATR-based SL/TP 1:3 RR
"""
from __future__ import annotations
import logging
from typing import Optional, Tuple, List
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# ── NSE Lot Sizes ────────────────────────────────────────────────────────────
NSE_LOT_SIZES = {
    "NIFTY":      65,
    "BANKNIFTY":  30,
    "FINNIFTY":   40,
    "MIDCPNIFTY": 120,
    "SENSEX":     10,
}

# ── Strike Step ──────────────────────────────────────────────────────────────
STRIKE_STEPS = {
    "NIFTY":      50,
    "BANKNIFTY":  100,
    "FINNIFTY":   50,
    "MIDCPNIFTY": 25,
    "SENSEX":     100,
}

def _atr(candles: list, period: int = 14) -> float:
    if len(candles) < period + 1:
        return 0.0
    def _h(c): return float(c.get("high", 0)) if isinstance(c, dict) else float(getattr(c, "high", 0))
    def _l(c): return float(c.get("low", 0))  if isinstance(c, dict) else float(getattr(c, "low", 0))
    def _c(c): return float(c.get("close", 0)) if isinstance(c, dict) else float(getattr(c, "close", 0))
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = _h(candles[i]), _l(candles[i]), _c(candles[i-1])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if not trs:
        return 0.0
    atr_val = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr_val = (atr_val * (period - 1) + tr) / period
    return round(atr_val, 2)

def _ema(values: list, period: int) -> Optional[float]:
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return round(ema, 4)

def _rsi(closes: list, period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        ag = (ag * (period-1) + gains[i]) / period
        al = (al * (period-1) + losses[i]) / period
    return round(100 - 100 / (1 + ag / al) if al != 0 else 100, 2)

def _macd(closes: list, fast=12, slow=26, signal=9):
    if len(closes) < slow + signal:
        return None
    fast_ema = _ema(closes, fast)
    slow_ema = _ema(closes, slow)
    if not fast_ema or not slow_ema:
        return None
    macd_line = fast_ema - slow_ema
    # Signal line
    macd_values = []
    for i in range(slow-1, len(closes)):
        fe = _ema(closes[:i+1], fast)
        se = _ema(closes[:i+1], slow)
        if fe and se:
            macd_values.append(fe - se)
    sig_line = _ema(macd_values, signal) if len(macd_values) >= signal else macd_line
    return {
        "macd": round(macd_line, 4),
        "signal": round(sig_line, 4) if sig_line else round(macd_line, 4),
        "histogram": round(macd_line - (sig_line or macd_line), 4),
    }

def _bollinger(closes: list, period=20, std_dev=2.0):
    if len(closes) < period:
        return None
    recent = closes[-period:]
    mid = sum(recent) / period
    variance = sum((x - mid) ** 2 for x in recent) / period
    std = variance ** 0.5
    return {
        "upper": round(mid + std_dev * std, 2),
        "mid":   round(mid, 2),
        "lower": round(mid - std_dev * std, 2),
        "width_pct": round((2 * std_dev * std / mid) * 100, 3) if mid else 0,
    }

def _strike_price(spot: float, symbol: str, option_type: str, otm_shift: int = 0) -> int:
    step = STRIKE_STEPS.get(symbol.upper(), 50)
    atm = round(spot / step) * step
    if option_type == "CE":
        return atm + otm_shift * step
    else:
        return atm - otm_shift * step

def _dte(symbol: str) -> int:
    from datetime import date
    today = date.today()
    expiry_days = {"NIFTY": 3, "BANKNIFTY": 3, "FINNIFTY": 1, "SENSEX": 4}
    exp_day = expiry_days.get(symbol.upper(), 3)
    days_ahead = (exp_day - today.weekday()) % 7
    return max(days_ahead, 1)


class ConfluenceOptionsAlgo:
    """
    Confluence Options Buyer
    Silver Bullet sweep+MSS + Multi Confirm momentum scoring
    """

    DEFAULT_PARAMS = {
        "min_confidence":  65,    # Combined score threshold
        "sb_weight":       40,    # Silver Bullet weight in score
        "mc_weight":       60,    # Multi Confirm weight in score
        "min_sb_score":    65,    # Silver Bullet minimum score
        "min_mc_score":    60,    # Multi Confirm minimum score
        "rsi_bull_min":    52,    # RSI minimum for bullish
        "rsi_bear_max":    48,    # RSI maximum for bearish
        "otm_shift":       0,     # ATM options
        "atr_sl_mult":     1.0,
        "atr_tp_mult":     3.0,
        "qty":             1,
    }

    def __init__(self, parameters: dict = None, risk_config: dict = None):
        self.parameters  = {**self.DEFAULT_PARAMS, **(parameters or {})}
        self.risk_config = risk_config or {}

    def _p(self, key):
        return self.parameters.get(key, self.DEFAULT_PARAMS.get(key))

    def generate_signal(self, symbol: str, candles_5m: list, candles_15m: list,
                        candles_1h: list, sb_signal: dict = None) -> Optional[dict]:
        """
        Main signal generation.
        sb_signal: Silver Bullet signal dict (from execute_silver_bullet_cycle)
        """
        if len(candles_5m) < 30:
            return None

        # ── VIX Filter ───────────────────────────────────────────────────────
        try:
            import yfinance as yf
            vix = float(yf.Ticker("^INDIAVIX").fast_info.get("lastPrice", 14.0) or 14.0)
        except Exception:
            vix = 14.0
        if vix > 20.0:
            logger.debug("ConfluenceOptions VIX=%.1f > 20 — skipping", vix)
            return None

        closes_5m  = [c["close"] if isinstance(c, dict) else c.close for c in candles_5m]
        closes_15m = [c["close"] if isinstance(c, dict) else c.close for c in candles_15m] if candles_15m else closes_5m
        spot       = closes_5m[-1]

        # ── Indicators ───────────────────────────────────────────────────────
        rsi_val  = _rsi(closes_5m, 14)
        macd_val = _macd(closes_5m)
        bb_val   = _bollinger(closes_5m, 20)
        atr_val  = _atr(candles_5m, 14)
        ema9     = _ema(closes_5m, 9)
        ema21    = _ema(closes_5m, 21)

        if not rsi_val or not macd_val or not bb_val:
            return None

        macd_hist    = macd_val["histogram"]
        bb_pos       = (spot - bb_val["lower"]) / (bb_val["upper"] - bb_val["lower"]) if bb_val["upper"] != bb_val["lower"] else 0.5
        ema_bullish  = ema9 > ema21 if ema9 and ema21 else False
        ema_bearish  = ema9 < ema21 if ema9 and ema21 else False

        # ── Silver Bullet gate ───────────────────────────────────────────────
        sb_direction = None
        sb_score     = 0
        if sb_signal:
            sb_direction = sb_signal.get("direction")   # "long" or "short"
            sb_score     = float(sb_signal.get("confluence_score", sb_signal.get("score", 0)))

        sb_long  = sb_direction == "long"  and sb_score >= self._p("min_sb_score")
        sb_short = sb_direction == "short" and sb_score >= self._p("min_sb_score")

        # ── Multi Confirm scoring ────────────────────────────────────────────
        def _score_bull() -> Tuple[float, List[str]]:
            score, reasons = 0.0, []
            # RSI
            if rsi_val >= self._p("rsi_bull_min"):
                pts = min(25, (rsi_val - 50) * 1.5)
                score += pts; reasons.append(f"RSI={rsi_val:.1f}")
            # MACD
            if macd_hist > 0:
                score += 20; reasons.append("MACD+")
            if macd_val["macd"] > macd_val["signal"]:
                score += 10; reasons.append("MACD_cross")
            # BB
            if bb_pos > 0.6:
                score += 15; reasons.append(f"BB_bull={bb_pos:.2f}")
            # EMA
            if ema_bullish:
                score += 15; reasons.append("EMA_bull")
            # ATR filter
            atr_pct = (atr_val / spot * 100) if spot > 0 else 0
            if 0.2 < atr_pct < 3.0:
                score += 10; reasons.append(f"ATR_ok={atr_pct:.1f}%")
            return score, reasons

        def _score_bear() -> Tuple[float, List[str]]:
            score, reasons = 0.0, []
            if rsi_val <= self._p("rsi_bear_max"):
                pts = min(25, (50 - rsi_val) * 1.5)
                score += pts; reasons.append(f"RSI={rsi_val:.1f}")
            if macd_hist < 0:
                score += 20; reasons.append("MACD-")
            if macd_val["macd"] < macd_val["signal"]:
                score += 10; reasons.append("MACD_cross")
            if bb_pos < 0.4:
                score += 15; reasons.append(f"BB_bear={bb_pos:.2f}")
            if ema_bearish:
                score += 15; reasons.append("EMA_bear")
            atr_pct = (atr_val / spot * 100) if spot > 0 else 0
            if 0.2 < atr_pct < 3.0:
                score += 10; reasons.append(f"ATR_ok={atr_pct:.1f}%")
            return score, reasons

        bull_score, bull_reasons = _score_bull()
        bear_score, bear_reasons = _score_bear()
        min_mc = self._p("min_mc_score")

        # ── Combined confluence score ─────────────────────────────────────────
        # CE signal: SB long + MC bullish
        if sb_long and bull_score >= min_mc:
            combined = (sb_score * self._p("sb_weight") + bull_score * self._p("mc_weight")) / 100
            if combined >= self._p("min_confidence"):
                strike = _strike_price(spot, symbol, "CE", self._p("otm_shift"))
                dte    = _dte(symbol)
                # ATR SL/TP
                sl_spot  = round(spot - self._p("atr_sl_mult") * atr_val, 2)
                tgt_spot = round(spot + self._p("atr_tp_mult") * atr_val, 2)
                # ── Greeks filter ──────────────────────────────────
                try:
                    from apps.options.black_scholes import compute_greeks
                    import math
                    T = max(dte / 365, 0.001)
                    g = compute_greeks(spot, strike, T, 0.065, 0.15, 'call')
                    ce_delta = g['delta']
                    ce_theta = g['theta']
                    ce_gamma = g['gamma']
                    if not (0.25 <= ce_delta <= 0.65):
                        logger.debug("ConfluenceOptions CE delta=%.3f out of range", ce_delta)
                        return None
                    if ce_theta < -15:
                        logger.debug("ConfluenceOptions CE theta=%.2f too high decay", ce_theta)
                        return None
                except Exception:
                    ce_delta = ce_theta = ce_gamma = None
                logger.info(
                    "✅ ConfluenceOptions BUY CE | %s | spot=%.2f | strike=%d | "
                    "combined=%.1f | SB=%.1f | MC=%.1f | RSI=%.1f | DTE=%d",
                    symbol, spot, strike, combined, sb_score, bull_score, rsi_val, dte
                )
                return {
                    "signal_type":  "buy",
                    "symbol":       symbol,
                    "price":        spot,
                    "option_type":  "CE",
                    "strike":       strike,
                    "confidence":   round(combined, 1),
                    "sb_score":     round(sb_score, 1),
                    "mc_score":     round(bull_score, 1),
                    "atr":          atr_val,
                    "spot":         spot,
                    "sl_spot":      sl_spot,
                    "tp_spot":      tgt_spot,
                    "dte":          dte,
                    "reasons":      bull_reasons,
                    "setup_type":   f"Confluence_CE_{symbol}",
                }

        # PE signal: SB short + MC bearish
        if sb_short and bear_score >= min_mc:
            combined = (sb_score * self._p("sb_weight") + bear_score * self._p("mc_weight")) / 100
            if combined >= self._p("min_confidence"):
                strike = _strike_price(spot, symbol, "PE", self._p("otm_shift"))
                dte    = _dte(symbol)
                sl_spot  = round(spot + self._p("atr_sl_mult") * atr_val, 2)
                tgt_spot = round(spot - self._p("atr_tp_mult") * atr_val, 2)
                # ── Greeks filter ──────────────────────────────────
                try:
                    from apps.options.black_scholes import compute_greeks
                    T = max(dte / 365, 0.001)
                    g = compute_greeks(spot, strike, T, 0.065, 0.15, 'put')
                    pe_delta = abs(g['delta'])
                    pe_theta = g['theta']
                    pe_gamma = g['gamma']
                    if not (0.25 <= pe_delta <= 0.65):
                        logger.debug("ConfluenceOptions PE delta=%.3f out of range", pe_delta)
                        return None
                    if pe_theta < -15:
                        logger.debug("ConfluenceOptions PE theta=%.2f too high decay", pe_theta)
                        return None
                except Exception:
                    pe_delta = pe_theta = pe_gamma = None
                logger.info(
                    "✅ ConfluenceOptions BUY PE | %s | spot=%.2f | strike=%d | "
                    "combined=%.1f | SB=%.1f | MC=%.1f | RSI=%.1f | DTE=%d",
                    symbol, spot, strike, combined, sb_score, bear_score, rsi_val, dte
                )
                return {
                    "signal_type":  "sell",
                    "symbol":       symbol,
                    "price":        spot,
                    "option_type":  "PE",
                    "strike":       strike,
                    "confidence":   round(combined, 1),
                    "sb_score":     round(sb_score, 1),
                    "mc_score":     round(bear_score, 1),
                    "atr":          atr_val,
                    "spot":         spot,
                    "sl_spot":      sl_spot,
                    "tp_spot":      tgt_spot,
                    "dte":          dte,
                    "reasons":      bear_reasons,
                    "setup_type":   f"Confluence_PE_{symbol}",
                }

        logger.debug(
            "ConfluenceOptions no signal | %s | SB=%s(%.1f) | bull=%.1f | bear=%.1f",
            symbol, sb_direction, sb_score, bull_score, bear_score,
        )
        return None
