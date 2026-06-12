# apps/ict_engine/silver_bullet.py
#
# ICT Silver Bullet 2M Strategy
# ---------------------------------------------------------
# Setup: Liquidity Raid -> MSS -> Immediate Market Entry
# RR: 1:3 fixed
# Timeframes: 1H (bias) + 5M (structure) + 2M (entry)
# Killzones: All (flexible)

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Optional

import numpy as np
import pandas as pd

from .base import RiskParameters, Signal, SignalDirection, SignalStatus, SignalStrength
from .ict import (
    BreakDirection,
    BreakType,
    FVGStatus,
    FVGType,
    LiqStatus,
    OBStatus,
    OBType,
    detect_bos_choch,
    detect_fvg,
    detect_liquidity,
    detect_order_blocks,
    get_killzone_context,
    run_mtf_analysis,
    swing_indices,
)

logger = logging.getLogger(__name__)


# --- Setup Result -------------------------------------------------------------
class SBDirection(str, Enum):
    LONG = "long"
    SHORT = "short"
    NONE = "none"


@dataclass
class SilverBulletSignal:
    direction: SBDirection
    symbol: str
    entry_price: float
    stop_loss: float
    take_profit1: float  # 1:2 RR - 50% close
    take_profit2: float  # 1:3 RR - final target

    # Context
    bias: str  # "bullish" | "bearish"
    killzone: str
    raid_price: float
    mss_price: float
    fvg_top: float
    fvg_bottom: float

    # Risk
    risk_points: float
    reward_points: float
    rr_ratio: float
    position_size: float  # lots
    risk_amount: float    # INR
    risk_pct: float

    # Meta
    confluence_score: float
    tags: list = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "direction": self.direction.value,
            "symbol": self.symbol,
            "entry_price": round(self.entry_price, 2),
            "stop_loss": round(self.stop_loss, 2),
            "take_profit_1": round(self.take_profit1, 2),
            "take_profit_2": round(self.take_profit2, 2),
            "bias": self.bias,
            "killzone": self.killzone,
            "raid_price": round(self.raid_price, 2),
            "mss_price": round(self.mss_price, 2),
            "fvg_top": round(self.fvg_top, 2),
            "fvg_bottom": round(self.fvg_bottom, 2),
            "risk_points": round(self.risk_points, 2),
            "reward_points": round(self.reward_points, 2),
            "rr_ratio": round(self.rr_ratio, 2),
            "position_size": round(self.position_size, 2),
            "risk_amount": round(self.risk_amount, 0),
            "risk_pct": round(self.risk_pct, 2),
            "confluence": round(self.confluence_score, 1),
            "tags": self.tags,
            "notes": self.notes,
        }


# --- Main Strategy ------------------------------------------------------------
class SilverBullet2MStrategy:
    """
    ICT Silver Bullet 2M - Professional intraday setup.

    Signal flow:
      1. Compute 1H daily bias from BOS direction
      2. Detect recent liquidity sweep (stop raid)
         - Bullish bias -> SSL sweep required
         - Bearish bias -> BSL sweep required
      3. Wait for MSS (CHoCH) on 2M against sweep direction
      4. Entry = Market (after MSS confirmation)
      5. SL   = Sweep extreme + buffer
      6. TP1  = 1:2 RR (50% partial)
      7. TP2  = 1:3 RR (final target)
    """

    LOT_SIZES = {
        "NIFTY": 75,
        "BANKNIFTY": 15,
        "FINNIFTY": 40,
        "MIDCPNIFTY": 75,
        "SENSEX": 10,
    }

    def __init__(
        self,
        account_balance: float = 100_000,
        risk_per_trade_pct: float = 1.0,
        min_rr: float = 3.0,
        sweep_lookback_bars: int = 100,
        mss_lookback_bars: int = 20,
        sl_buffer_points: float = 5.0,
        enable_pd_zone_reanchor: bool = True,
    ):
        self.account_balance = account_balance
        self.risk_per_trade_pct = risk_per_trade_pct
        self.min_rr = min_rr
        self.sweep_lookback = sweep_lookback_bars
        self.mss_lookback = mss_lookback_bars
        self.sl_buffer = sl_buffer_points
        self.enable_pd_zone_reanchor = enable_pd_zone_reanchor

    # --- Daily bias from 1H BOS ----------------------------------------------
    def _compute_bias(self, df_1h: pd.DataFrame) -> str:
        """1H last BOS + EMA20 confirmation = daily bias."""
        if df_1h.empty or len(df_1h) < 50:
            return "neutral"

        sh_idx, sl_idx = swing_indices(
            df_1h, method="fractal", left_bars=3, right_bars=3
        )
        breaks = detect_bos_choch(df_1h, sh_idx, sl_idx)

        if not breaks:
            return "neutral"

        last_bos = None
        for b in reversed(breaks):
            if b.break_type == BreakType.BOS:
                last_bos = b
                break

        if last_bos is None:
            last_bos = breaks[-1]

        if last_bos.direction == BreakDirection.BULLISH:
            bos_bias = "bullish"
        elif last_bos.direction == BreakDirection.BEARISH:
            bos_bias = "bearish"
        else:
            return "neutral"

        # EMA20 confirmation — current price vs 1H EMA20
        ema20 = df_1h["close"].ewm(span=20, adjust=False).mean()
        current_price = float(df_1h["close"].iloc[-1])
        ema20_val = float(ema20.iloc[-1])
        if bos_bias == "bullish" and current_price < ema20_val * 0.998:
            logger.info("Bias override bullish->bearish | price=%.2f EMA20=%.2f", current_price, ema20_val)
            return "bearish"
        if bos_bias == "bearish" and current_price > ema20_val * 1.002:
            logger.info("Bias override bearish->bullish | price=%.2f EMA20=%.2f", current_price, ema20_val)
            return "bullish"
        return bos_bias

    # --- Liquidity raid detection ---------------------------------------------
    def _detect_sweep(self, df_2m: pd.DataFrame, bias: str) -> Optional[dict]:
        if len(df_2m) < 20:
            return None

        window_start = max(0, len(df_2m) - self.sweep_lookback)
        window = df_2m.iloc[window_start:]

        sh_idx, sl_idx = swing_indices(
            window, method="fractal", left_bars=2, right_bars=2
        )

        if bias == "bullish":
            for i in range(len(window) - 4, 2, -1):
                low_i = float(window["low"].iloc[i])
                close_i = float(window["close"].iloc[i])

                prior_lows = [float(window["low"].iloc[j]) for j in sl_idx if j < i]
                if not prior_lows:
                    continue
                recent_sl = (
                    min(prior_lows[-3:]) if len(prior_lows) >= 3 else min(prior_lows)
                )

                if low_i < recent_sl and close_i > recent_sl:
                    bars_since_sweep = (len(window) - 1) - i
                    if bars_since_sweep < 3:
                        logger.debug(
                            "Sweep too recent (%d bars ago), skipping",
                            bars_since_sweep,
                        )
                        continue
                    return {
                        "type": "SSL",
                        "sweep_bar": window_start + i,
                        "sweep_price": low_i,
                        "swept_level": recent_sl,
                        "rejection": close_i - low_i,
                        "bars_ago": bars_since_sweep,
                    }

        elif bias == "bearish":
            for i in range(len(window) - 4, 2, -1):
                high_i = float(window["high"].iloc[i])
                close_i = float(window["close"].iloc[i])

                prior_highs = [float(window["high"].iloc[j]) for j in sh_idx if j < i]
                if not prior_highs:
                    continue
                recent_sh = (
                    max(prior_highs[-3:]) if len(prior_highs) >= 3 else max(prior_highs)
                )

                if high_i > recent_sh and close_i < recent_sh:
                    bars_since_sweep = (len(window) - 1) - i
                    if bars_since_sweep < 3:
                        logger.debug(
                            "Sweep too recent (%d bars ago), skipping",
                            bars_since_sweep,
                        )
                        continue
                    return {
                        "type": "BSL",
                        "sweep_bar": window_start + i,
                        "sweep_price": high_i,
                        "swept_level": recent_sh,
                        "rejection": high_i - close_i,
                        "bars_ago": bars_since_sweep,
                    }

        return None

    # --- MSS detection after sweep -------------------------------------------
    def _detect_mss(
        self,
        df_2m: pd.DataFrame,
        sweep_bar: int,
        bias: str,
    ) -> Optional[dict]:
        after_sweep = df_2m.iloc[sweep_bar : sweep_bar + self.mss_lookback + 1]

        available = len(after_sweep)
        logger.debug(
            "MSS window: sweep_bar=%d, total_bars=%d, after_sweep_bars=%d",
            sweep_bar,
            len(df_2m),
            available,
        )

        if available < 6:
            logger.info(
                "MSS skipped: only %d bars after sweep (need 6+).",
                available,
            )
            return None

        # Dynamic fractal sensitivity based on available bars
        lb = rb = 1 if available < 15 else 2

        sh_idx, sl_idx = swing_indices(
            after_sweep, method="fractal", left_bars=lb, right_bars=rb
        )

        if bias == "bullish":
            if not sh_idx:
                logger.debug("MSS: no swing highs found after sweep (bullish)")
                return None
            for sh in sh_idx:
                sh_price = float(after_sweep["high"].iloc[sh])
                for j in range(sh + 2, len(after_sweep)):
                    close_j = float(after_sweep["close"].iloc[j])
                    if close_j > sh_price:
                        logger.info(
                            "MSS confirmed BULLISH: broke %.2f at bar %d",
                            sh_price,
                            sweep_bar + j,
                        )
                        return {
                            "mss_bar": sweep_bar + j,
                            "mss_price": sh_price,
                            "break_close": close_j,
                            "direction": "bullish",
                        }
            logger.debug("MSS: bullish CHoCH not found. sh_idx=%s", sh_idx)

        elif bias == "bearish":
            if not sl_idx:
                logger.debug("MSS: no swing lows found after sweep (bearish)")
                return None
            for sl in sl_idx:
                sl_price = float(after_sweep["low"].iloc[sl])
                for j in range(sl + 2, len(after_sweep)):
                    close_j = float(after_sweep["close"].iloc[j])
                    if close_j < sl_price:
                        logger.info(
                            "MSS confirmed BEARISH: broke %.2f at bar %d",
                            sl_price,
                            sweep_bar + j,
                        )
                        return {
                            "mss_bar": sweep_bar + j,
                            "mss_price": sl_price,
                            "break_close": close_j,
                            "direction": "bearish",
                        }
            logger.debug("MSS: bearish CHoCH not found. sl_idx=%s", sl_idx)

        return None

    # --- FVG near MSS --------------------------------------------------------
    def _find_fvg_after_mss(
        self,
        df_2m: pd.DataFrame,
        mss_bar: int,
        direction: str,
    ) -> Optional[dict]:
        """Find the first FVG that formed at/after MSS."""
        window = df_2m.iloc[max(0, mss_bar - 2):]
        if len(window) < 3:
            return None

        fvgs = detect_fvg(window, update_status=False)

        target_type = FVGType.BULLISH if direction == "bullish" else FVGType.BEARISH
        matching = [f for f in fvgs if f.fvg_type == target_type]

        if not matching:
            return None

        fvg = matching[-1]
        return {
            "top": fvg.top,
            "bottom": fvg.bottom,
            "mid": fvg.mid,
        }

    # --- Position sizing ------------------------------------------------------
    def _size_position(
        self,
        entry: float,
        stop: float,
        symbol: str,
    ) -> tuple[float, float]:
        """Returns (lots, risk_amount_inr)."""
        risk_pct = self.risk_per_trade_pct
        risk_amount = self.account_balance * (risk_pct / 100.0)
        risk_per_pt = abs(entry - stop)

        if risk_per_pt == 0:
            return 0.0, 0.0

        lot_size = self.LOT_SIZES.get(symbol.upper(), 75)
        risk_per_lot = risk_per_pt * lot_size

        if risk_per_lot == 0:
            return 0.0, 0.0

        lots = max(0.25, round(risk_amount / risk_per_lot * 4) / 4)
        actual_risk = lots * risk_per_lot

        return round(lots, 2), round(actual_risk, 0)

    # --- Main signal generation -----------------------------------------------
    def analyze(
        self,
        symbol: str,
        df_1h: pd.DataFrame,
        df_5m: pd.DataFrame,
        df_2m: pd.DataFrame,
        df_4h: pd.DataFrame = None,
    ) -> Optional[SilverBulletSignal]:
        """
        Complete Silver Bullet analysis.
        Returns a signal if valid setup, None otherwise.
        """
        # Step 1: Daily bias
        bias = self._compute_bias(df_1h)
        if bias == "neutral":
            logger.debug("[%s] Silver Bullet: No clear bias", symbol)
            return None

        logger.info("[%s] Silver Bullet bias: %s", symbol, bias)

        # Step 1b: 5M structure confirmation (EMA20 alignment)
        if not df_5m.empty and len(df_5m) >= 20:
            _ema20_5m = df_5m["close"].ewm(span=20, adjust=False).mean()
            _close_5m = float(df_5m["close"].iloc[-1])
            _ema20_5m_val = float(_ema20_5m.iloc[-1])
            if bias == "bullish" and _close_5m < _ema20_5m_val:
                logger.info("[%s] 5M structure bearish (close=%.2f < EMA20=%.2f), skip", symbol, _close_5m, _ema20_5m_val)
                return None
            if bias == "bearish" and _close_5m > _ema20_5m_val:
                logger.info("[%s] 5M structure bullish (close=%.2f > EMA20=%.2f), skip", symbol, _close_5m, _ema20_5m_val)
                return None

        # Step 1c: 4H bias filter — conflict = skip, alignment = A+ bonus later
        _4h_bias_ok = False
        try:
            if df_4h is not None and not df_4h.empty and len(df_4h) >= 10:
                _ema4h = df_4h["close"].ewm(span=20, adjust=False).mean()
                _close4h = float(df_4h["close"].iloc[-1])
                _ema4h_val = float(_ema4h.iloc[-1])
                _bias4h = "bullish" if _close4h > _ema4h_val else "bearish"
                if _bias4h != bias:
                    logger.warning(
                        "[%s] 4H bias %s conflicts with %s bias — proceeding anyway",
                        symbol, _bias4h, bias
                    )
                    _4h_bias_ok = False  # no A+ bonus
                else:
                    _4h_bias_ok = True
                    logger.info("[%s] 4H bias ✅ %s aligns with entry", symbol, _bias4h)
        except Exception as _4he:
            logger.debug("[%s] 4H bias check error: %s", symbol, _4he)

        # Step 2: Liquidity sweep
        sweep = self._detect_sweep(df_2m, bias)
        if not sweep:
            logger.debug("[%s] Silver Bullet: No recent sweep", symbol)
            return None

        logger.info(
            "[%s] Sweep detected: %s @ %.2f",
            symbol,
            sweep["type"],
            sweep["sweep_price"],
        )

        # Step 3: MSS confirmation
        mss = self._detect_mss(df_2m, sweep["sweep_bar"], bias)
        if not mss:
            logger.debug("[%s] Silver Bullet: No MSS after sweep", symbol)
            return None

        logger.info(
            "[%s] MSS confirmed: %s break @ %.2f",
            symbol,
            mss["direction"],
            mss["mss_price"],
        )

        # Step 4: FVG (optional - enhances setup)
        fvg = self._find_fvg_after_mss(df_2m, mss["mss_bar"], mss["direction"])

        # Step 5: Entry - market entry at current price after MSS
        current_price = float(df_2m["close"].iloc[-1])
        entry_price = current_price

        # Step 5b: ICT Premium/Discount Zone filter (with dynamic range re-anchoring).
        #
        # On expansion days price breaks beyond PDH/PDL making the static PDH-PDL
        # equilibrium permanently stale.  When a breakout is detected we re-anchor
        # the dealing range to the most recent structure low/high seen AFTER the
        # breakout (ICT PO3 "range expansion" concept).
        #
        # Re-anchor is controlled by self.enable_pd_zone_reanchor (default True).
        try:
            from datetime import timedelta
            today = df_2m.index[-1].date()
            yesterday = today - timedelta(days=1)
            prev_day = df_2m[df_2m.index.date == yesterday]
            if not prev_day.empty and len(prev_day) > 5:
                _ph = float(prev_day['high'].max())
                _pl = float(prev_day['low'].min())
                logger.debug("[%s] PD Zone using PDH=%.2f PDL=%.2f", symbol, _ph, _pl)
            else:
                _ph = float(df_2m['high'].max())
                _pl = float(df_2m['low'].min())
                logger.debug("[%s] PD Zone fallback: today's range", symbol)
            _range = _ph - _pl
            if _range > 0:
                _eq = _pl + (_range * 0.5)

                # ── Bullish breakout beyond PDH ─────────────────────────────────
                if self.enable_pd_zone_reanchor and bias == 'bullish' and entry_price > _ph:
                    today_bars = df_2m[df_2m.index.date == today]
                    today_high = float(today_bars['high'].max()) if not today_bars.empty else entry_price

                    # Find first bar where close crossed above _ph (PDH)
                    breakout_idx = None
                    for _k in range(len(today_bars)):
                        if float(today_bars['close'].iloc[_k]) > _ph:
                            breakout_idx = _k
                            break

                    _pl_new = None
                    if breakout_idx is not None:
                        post_break = today_bars.iloc[breakout_idx:]
                        if len(post_break) >= 5:
                            _, _post_sl = swing_indices(
                                post_break, method="fractal", left_bars=2, right_bars=2
                            )
                            if _post_sl:
                                # Lowest fractal low post-PDH = origin of expansion leg
                                _pl_new = min(
                                    float(post_break['low'].iloc[j]) for j in _post_sl
                                )

                    if _pl_new is None:
                        # Fallback: the current sweep extreme anchors the new range
                        _pl_new = sweep['sweep_price']
                        logger.debug(
                            "[%s] PD Zone re-anchor fallback: no post-PDH fractals, "
                            "using sweep_price=%.2f", symbol, _pl_new
                        )

                    _new_range = today_high - _pl_new
                    if _new_range > 0:
                        _eq_new = _pl_new + _new_range * 0.5
                        logger.info(
                            "[%s] PD Zone re-anchored (bullish, price=%.2f > PDH=%.2f) | "
                            "new range [%.2f–%.2f] eq_new=%.2f (orig eq=%.2f)",
                            symbol, entry_price, _ph, _pl_new, today_high, _eq_new, _eq
                        )
                        if entry_price <= _eq_new:
                            logger.info(
                                "[%s] PD Zone re-anchored ✅ | price=%.2f in DISCOUNT "
                                "of new range (eq_new=%.2f), LONG allowed",
                                symbol, entry_price, _eq_new
                            )
                            # fall through — allow the signal
                        else:
                            logger.info(
                                "[%s] PD Zone re-anchored ❌ | price=%.2f in PREMIUM "
                                "of new range (eq_new=%.2f), skip LONG",
                                symbol, entry_price, _eq_new
                            )
                            return None
                    else:
                        logger.info(
                            "[%s] Silver Bullet: price %.2f in PREMIUM zone (eq=%.2f) "
                            "[re-anchor invalid: new_range=%.2f], skip LONG",
                            symbol, entry_price, _eq, _new_range
                        )
                        return None

                # ── Bearish breakdown beyond PDL ────────────────────────────────
                elif self.enable_pd_zone_reanchor and bias == 'bearish' and entry_price < _pl:
                    today_bars = df_2m[df_2m.index.date == today]
                    today_low = float(today_bars['low'].min()) if not today_bars.empty else entry_price

                    breakdown_idx = None
                    for _k in range(len(today_bars)):
                        if float(today_bars['close'].iloc[_k]) < _pl:
                            breakdown_idx = _k
                            break

                    _ph_new = None
                    if breakdown_idx is not None:
                        post_break = today_bars.iloc[breakdown_idx:]
                        if len(post_break) >= 5:
                            _post_sh, _ = swing_indices(
                                post_break, method="fractal", left_bars=2, right_bars=2
                            )
                            if _post_sh:
                                # Highest fractal high post-PDL = origin of expansion leg
                                _ph_new = max(
                                    float(post_break['high'].iloc[j]) for j in _post_sh
                                )

                    if _ph_new is None:
                        _ph_new = sweep['sweep_price']
                        logger.debug(
                            "[%s] PD Zone re-anchor fallback: no post-PDL fractals, "
                            "using sweep_price=%.2f", symbol, _ph_new
                        )

                    _new_range = _ph_new - today_low
                    if _new_range > 0:
                        _eq_new = today_low + _new_range * 0.5
                        logger.info(
                            "[%s] PD Zone re-anchored (bearish, price=%.2f < PDL=%.2f) | "
                            "new range [%.2f–%.2f] eq_new=%.2f (orig eq=%.2f)",
                            symbol, entry_price, _pl, today_low, _ph_new, _eq_new, _eq
                        )
                        if entry_price >= _eq_new:
                            logger.info(
                                "[%s] PD Zone re-anchored ✅ | price=%.2f in PREMIUM "
                                "of new range (eq_new=%.2f), SHORT allowed",
                                symbol, entry_price, _eq_new
                            )
                        else:
                            logger.info(
                                "[%s] PD Zone re-anchored ❌ | price=%.2f in DISCOUNT "
                                "of new range (eq_new=%.2f), skip SHORT",
                                symbol, entry_price, _eq_new
                            )
                            return None
                    else:
                        logger.info(
                            "[%s] Silver Bullet: price %.2f in DISCOUNT zone (eq=%.2f) "
                            "[re-anchor invalid: new_range=%.2f], skip SHORT",
                            symbol, entry_price, _eq, _new_range
                        )
                        return None

                # ── Normal case: price within PDH–PDL range ──────────────────────
                elif bias == 'bullish' and entry_price > _eq:
                    logger.info(
                        "[%s] Silver Bullet: price %.2f in PREMIUM zone (eq=%.2f), skip LONG",
                        symbol, entry_price, _eq
                    )
                    return None
                elif bias == 'bearish' and entry_price < _eq:
                    logger.info(
                        "[%s] Silver Bullet: price %.2f in DISCOUNT zone (eq=%.2f), skip SHORT",
                        symbol, entry_price, _eq
                    )
                    return None
                else:
                    logger.info(
                        "[%s] PD Zone ✅ | price=%.2f eq=%.2f | %s",
                        symbol, entry_price, _eq,
                        'DISCOUNT' if bias == 'bullish' else 'PREMIUM'
                    )
        except Exception as e:
            logger.debug("[%s] PD Zone check skipped: %s", symbol, e)

        # Step 6: Stop loss = sweep extreme + buffer
        # Min SL distance: ATR based (at least 0.1% of price)
        min_sl_dist = entry_price * 0.001
        if bias == "bullish":
            stop_loss = sweep["sweep_price"] - self.sl_buffer
            if stop_loss >= entry_price:
                # Sweep too close — use ATR-based SL
                stop_loss = entry_price - max(min_sl_dist, self.sl_buffer)
                logger.debug("[%s] SL adjusted (bullish) to %.2f", symbol, stop_loss)
        else:
            stop_loss = sweep["sweep_price"] + self.sl_buffer
            if stop_loss <= entry_price:
                # Sweep too close — use ATR-based SL
                stop_loss = entry_price + max(min_sl_dist, self.sl_buffer)
                logger.debug("[%s] SL adjusted (bearish) to %.2f", symbol, stop_loss)

        # Step 7: Targets (1:2 and 1:3 RR)
        risk_points = abs(entry_price - stop_loss)
        reward_points = risk_points * self.min_rr

        # Sweep rejection strength filter
        if sweep.get("rejection", 0) < risk_points * 0.3:
            logger.info(
                "[%s] Weak sweep rejection: %.2f < %.2f (threshold)",
                symbol, sweep.get("rejection", 0), risk_points * 0.3,
            )
            return None

        if bias == "bullish":
            tp1 = entry_price + (risk_points * 2.0)
            tp2 = entry_price + reward_points
        else:
            tp1 = entry_price - (risk_points * 2.0)
            tp2 = entry_price - reward_points

        # Validate min risk
        if risk_points < 5:
            logger.debug("[%s] Risk too tight: %.2f pts", symbol, risk_points)
            return None

        # Step 8: Position size
        lots, risk_amount = self._size_position(entry_price, stop_loss, symbol)
        if lots == 0:
            return None

        # Step 9: Killzone context
        last_ts = df_2m.index[-1]
        try:
            kz_ctx = get_killzone_context(last_ts)
            kz_name = (
                kz_ctx.highest_priority_zone.name.value
                if kz_ctx.highest_priority_zone
                else "regular"
            )
        except Exception:
            kz_name = "regular"

        # Killzone mandatory — skip regular market hours
        if kz_name == "regular":
            logger.info("[%s] Silver Bullet: No killzone context, skipping", symbol)
            return None

        # Step 10: Confluence score
        score = 50.0
        if fvg:
            score += 20
        if mss.get("break_close"):
            break_strength = abs(mss["break_close"] - mss["mss_price"])
            if break_strength > risk_points * 0.3:
                score += 10
        if kz_name != "regular":
            score += 15
        if sweep.get("rejection", 0) > risk_points * 0.5:
            score += 5

        # OTE bonus (+5 score)
        try:
            if _range > 0:
                _ote_low = _ph - (_range * 0.79)
                _ote_high = _ph - (_range * 0.62)
                if _ote_low <= entry_price <= _ote_high:
                    score += 5
                    tags.append('OTE')
        except Exception:
            pass

        # 4H bias A+ bonus (+5 score)
        if _4h_bias_ok:
            score += 5

        # DOL (Draw on Liquidity) — nearest BSL/SSL as target
        _dol_found = False
        try:
            if bias == 'bullish':
                _above_dol = [float(h) for h in df_2m['high'].values if h > entry_price]
                _dol_level_sb = float(min(_above_dol)) if _above_dol else None
            else:
                _below_dol = [float(l) for l in df_2m['low'].values if 0 < l < entry_price]
                _dol_level_sb = float(max(_below_dol)) if _below_dol else None
            if _dol_level_sb:
                _dol_dp = abs(_dol_level_sb - entry_price) / entry_price * 100
                if _dol_dp >= 0.3:
                    score += 10
                    _dol_found = True
                    logger.info("[%s] DOL ✅ level=%.2f dist=%.2f%%", symbol, _dol_level_sb, _dol_dp)
        except Exception as _dole:
            logger.debug("[%s] DOL check error: %s", symbol, _dole)

        score = min(round(score, 1), 100.0)

        # Score threshold filter
        if score < 65:
            logger.info("[%s] Score too low: %.1f < 65, skipping", symbol, score)
            return None

        # Greeks filter — delta 0.25–0.65, theta > -15 for ATM option
        try:
            import datetime as _dt
            from apps.options.black_scholes import compute_greeks as _cg
            _opt_type = "call" if bias == "bullish" else "put"
            _sym_up = symbol.upper()
            if "BANKNIFTY" in _sym_up or "FINNIFTY" in _sym_up:
                _strike_step, _exp_wday = 100, 3  # Thursday
            else:
                _strike_step, _exp_wday = 50, 1   # Tuesday (NIFTY)
            _today = _dt.date.today()
            _days_ahead = (_exp_wday - _today.weekday()) % 7 or 7
            _T = max(_days_ahead / 365, 1 / 365)
            _atm = round(entry_price / _strike_step) * _strike_step
            _g = _cg(entry_price, _atm, _T, 0.065, 0.15, _opt_type)
            _delta_abs = abs(_g["delta"])
            if not (0.25 <= _delta_abs <= 0.65):
                logger.info("[%s] Greeks filter: delta=%.4f out of [0.25, 0.65], skip", symbol, _delta_abs)
                return None
            if _g["theta"] < -15:
                logger.info("[%s] Greeks filter: theta=%.2f < -15, skip", symbol, _g["theta"])
                return None
        except Exception as _ge:
            logger.debug("[%s] Greeks filter error (skipping check): %s", symbol, _ge)

        tags = [bias.upper(), f"SWEEP_{sweep['type']}", "MSS"]
        if fvg:
            tags.append("FVG")
        if kz_name != "regular":
            tags.append(f"KZ_{kz_name.upper()}")
        if _dol_found:
            tags.append('DOL')

        signal = SilverBulletSignal(
            direction=SBDirection.LONG if bias == "bullish" else SBDirection.SHORT,
            symbol=symbol,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit1=tp1,
            take_profit2=tp2,
            bias=bias,
            killzone=kz_name,
            raid_price=sweep["sweep_price"],
            mss_price=mss["mss_price"],
            fvg_top=fvg["top"] if fvg else 0.0,
            fvg_bottom=fvg["bottom"] if fvg else 0.0,
            risk_points=risk_points,
            reward_points=reward_points,
            rr_ratio=self.min_rr,
            position_size=lots,
            risk_amount=risk_amount,
            risk_pct=self.risk_per_trade_pct,
            confluence_score=score,
            tags=tags,
            notes=f"Silver Bullet: {sweep['type']} raid -> MSS -> Entry",
        )

        logger.info(
            "[%s] SILVER BULLET SIGNAL | %s | Entry=%.2f SL=%.2f TP=%.2f | RR=1:%.1f | Score=%.1f",
            symbol,
            signal.direction.value.upper(),
            signal.entry_price,
            signal.stop_loss,
            signal.take_profit2,
            signal.rr_ratio,
            signal.confluence_score,
        )

        return signal


# --- Backtest ----------------------------------------------------------------
def run_silver_bullet_backtest(
    strategy,
    from_date: str,
    to_date: str,
) -> dict:
    """Walk-forward Silver Bullet backtest."""
    import datetime as _dt

    from fyers_apiv3 import fyersModel

    from apps.brokers.models import BrokerAccount

    account = BrokerAccount.objects.filter(
        user=strategy.user,
        broker="fyers",
        is_active=True,
        is_verified=True,
    ).first()
    if not account:
        raise RuntimeError("Fyers account not connected")

    fyers = fyersModel.FyersModel(
        client_id=account.app_id,
        token=account.access_token,
        log_path="",
        is_async=False,
    )

    from apps.brokers.symbol_mapper import normalize_for_fyers
    fyers_sym = normalize_for_fyers(strategy.symbol)

    def _fetch(resolution, max_days=95):
        all_candles = []
        start = _dt.datetime.strptime(from_date, "%Y-%m-%d")
        end = _dt.datetime.strptime(to_date, "%Y-%m-%d")
        cur = start
        while cur < end:
            chunk_end = min(cur + _dt.timedelta(days=max_days), end)
            r = fyers.history(
                data={
                    "symbol": fyers_sym,
                    "resolution": resolution,
                    "date_format": "1",
                    "range_from": cur.strftime("%Y-%m-%d"),
                    "range_to": chunk_end.strftime("%Y-%m-%d"),
                    "cont_flag": "1",
                }
            )
            if r.get("s") == "ok":
                all_candles.extend(r.get("candles", []))
            cur = chunk_end + _dt.timedelta(days=1)
        return all_candles

    candles_2m = _fetch("2")
    if len(candles_2m) < 100:
        raise RuntimeError(f"Insufficient 2M data: {len(candles_2m)}")

    candles_2m = candles_2m[-3000:]

    df_2m = pd.DataFrame(
        candles_2m, columns=["ts", "open", "high", "low", "close", "volume"]
    )
    df_2m.index = pd.to_datetime(df_2m["ts"], unit="s", utc=True)
    df_2m = df_2m.drop(columns=["ts"])

    candles_1h = _fetch("60")
    df_1h = pd.DataFrame(
        candles_1h, columns=["ts", "open", "high", "low", "close", "volume"]
    )
    df_1h.index = pd.to_datetime(df_1h["ts"], unit="s", utc=True)
    df_1h = df_1h.drop(columns=["ts"])

    sb = SilverBullet2MStrategy(
        account_balance=float(strategy.parameters.get("capital", 100000)),
        risk_per_trade_pct=float(strategy.parameters.get("risk_pct", 1.0)),
        min_rr=float(strategy.parameters.get("min_rr", 3.0)),
    )

    trades = []
    capital = sb.account_balance
    balance = capital
    warmup = 60
    open_trade_bt = None  # ✅ renamed: backtest local var, not the imported function

    step = max(2, len(df_2m) // 200)

    for i in range(warmup, len(df_2m), step):
        window_2m = df_2m.iloc[: i + 1]
        last_ts = window_2m.index[-1]

        if open_trade_bt:
            bar_high = float(df_2m["high"].iloc[i])
            bar_low = float(df_2m["low"].iloc[i])
            closed = False
            exit_px = 0.0
            reason = ""

            if open_trade_bt["direction"] == "long":
                if bar_low <= open_trade_bt["sl"]:
                    exit_px = open_trade_bt["sl"]
                    reason = "SL"
                    closed = True
                elif bar_high >= open_trade_bt["tp2"]:
                    exit_px = open_trade_bt["tp2"]
                    reason = "TP2"
                    closed = True
            else:
                if bar_high >= open_trade_bt["sl"]:
                    exit_px = open_trade_bt["sl"]
                    reason = "SL"
                    closed = True
                elif bar_low <= open_trade_bt["tp2"]:
                    exit_px = open_trade_bt["tp2"]
                    reason = "TP2"
                    closed = True

            if closed:
                pnl = (exit_px - open_trade_bt["entry"]) * open_trade_bt["qty"]
                if open_trade_bt["direction"] == "short":
                    pnl = -pnl
                balance += pnl
                trades.append(
                    {
                        "entry_ts": open_trade_bt["entry_ts"],
                        "exit_ts": str(last_ts),
                        "side": open_trade_bt["direction"],
                        "entry_price": round(open_trade_bt["entry"], 2),
                        "exit_price": round(exit_px, 2),
                        "qty": open_trade_bt["qty"],
                        "pnl": round(pnl, 2),
                        "balance": round(balance, 2),
                        "reason": reason,
                        "tags": open_trade_bt["tags"],
                    }
                )
                open_trade_bt = None

        if open_trade_bt:
            continue

        df_1h_sliced = df_1h[df_1h.index <= last_ts]
        if len(df_1h_sliced) < 30:
            continue

        try:
            sig = sb.analyze(
                symbol=strategy.symbol,
                df_1h=df_1h_sliced,
                df_5m=window_2m,
                df_2m=window_2m,
            )
        except Exception as e:
            logger.debug("SB analyze error: %s", e)
            continue

        if sig is None:
            continue

        lot_size = sb.LOT_SIZES.get(strategy.symbol.upper(), 75)
        qty = sig.position_size * lot_size

        open_trade_bt = {
            "direction": sig.direction.value,
            "entry": sig.entry_price,
            "sl": sig.stop_loss,
            "tp1": sig.take_profit1,
            "tp2": sig.take_profit2,
            "qty": qty,
            "entry_ts": str(last_ts),
            "tags": sig.tags,
        }

    total = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    net = round(balance - capital, 2)

    pnls = [t["pnl"] for t in trades]
    avg_win = round(sum(w["pnl"] for w in wins) / len(wins), 2) if wins else 0
    avg_loss = round(sum(l["pnl"] for l in losses) / len(losses), 2) if losses else 0

    gross_profit = sum(w["pnl"] for w in wins)
    gross_loss = abs(sum(l["pnl"] for l in losses))
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss else 0.0

    peak = capital
    bal = capital
    max_dd = 0
    for t in trades:
        bal += t["pnl"]
        if bal > peak:
            peak = bal
        dd = (peak - bal) / peak * 100
        if dd > max_dd:
            max_dd = dd
    max_dd = round(max_dd, 2)

    if len(pnls) > 1:
        returns = [p / capital for p in pnls]
        avg_r = float(np.mean(returns))
        std_r = float(np.std(returns))
        sharpe = round(avg_r / std_r * np.sqrt(252), 2) if std_r else 0.0
        neg_r = [r for r in returns if r < 0]
        down_std = float(np.std(neg_r)) if neg_r else 0.0
        sortino = round(avg_r / down_std * np.sqrt(252), 2) if down_std else 0.0
    else:
        sharpe = sortino = 0.0

    calmar = round(net / capital * 100 / max_dd, 2) if max_dd > 0 else 0.0
    wr_dec = len(wins) / total if total else 0
    expectancy = round((wr_dec * avg_win) + ((1 - wr_dec) * avg_loss), 2)

    equity_curve = [{"ts": t["exit_ts"], "equity": t["balance"]} for t in trades]

    return {
        "strategy_name": strategy.name,
        "algo_name": "ict_silver_bullet",
        "symbol": strategy.symbol,
        "from_date": from_date,
        "to_date": to_date,
        "timeframe": "2m",
        "total_candles": len(df_2m),
        "total_trades": total,
        "win_trades": len(wins),
        "loss_trades": len(losses),
        "win_rate": round(len(wins) / total * 100, 1) if total else 0,
        "initial_capital": capital,
        "final_balance": round(balance, 2),
        "net_pnl": net,
        "return_pct": round(net / capital * 100, 2),
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": profit_factor,
        "max_drawdown": max_dd,
        "sharpe_ratio": sharpe,
        "sortino_ratio": sortino,
        "calmar_ratio": calmar,
        "expectancy": expectancy,
        "equity_curve": equity_curve,
        "trades": trades[-100:],
    }


# --- Live cycle --------------------------------------------------------------
def execute_silver_bullet_cycle(strategy, symbol: str) -> dict:
    """Live/paper cycle."""
    from decimal import Decimal
    import pandas as pd
    from apps.common.candle_service import fetch_candles_for_strategy

    htf_tf = strategy.parameters.get("htf", "60")
    mtf_tf = strategy.parameters.get("mtf", "5")
    ltf_tf = strategy.parameters.get("ltf", "1")

    htf_raw = []
    mtf_raw = []
    ltf_raw = []

    try:
        htf_raw = fetch_candles_for_strategy(strategy, symbol, htf_tf, bars=300) or []
        mtf_raw = fetch_candles_for_strategy(strategy, symbol, mtf_tf, bars=200) or []
        ltf_raw = fetch_candles_for_strategy(strategy, symbol, ltf_tf, bars=500) or []
    except TypeError:
        try:
            htf_raw = fetch_candles_for_strategy(strategy, symbol, htf_tf) or []
            mtf_raw = fetch_candles_for_strategy(strategy, symbol, mtf_tf) or []
            ltf_raw = fetch_candles_for_strategy(strategy, symbol, ltf_tf) or []
        except Exception as e:
            logger.error("SB candle fetch error | symbol=%s | err=%s", symbol, e)
            return _null_sb_signal(symbol)
    except Exception as e:
        logger.error("SB candle fetch error | symbol=%s | err=%s", symbol, e)
        return _null_sb_signal(symbol)

    # 4H candles — graceful fallback if unavailable
    try:
        htf4h_raw = fetch_candles_for_strategy(strategy, symbol, "240", bars=100) or []
    except Exception:
        htf4h_raw = []

    if not htf_raw or not ltf_raw:
        # ✅ FIX: LTF 0 candles aana common hai after-hours (Fyers 1m data nahi deta)
        # MTF se fallback karo instead of returning null signal immediately
        if htf_raw and not ltf_raw and mtf_raw:
            logger.warning(
                "SB: LTF empty (after-hours?) | symbol=%s | htf=%d ltf=0 mtf=%d — "
                "using MTF as LTF fallback",
                symbol,
                len(htf_raw),
                len(mtf_raw),
            )
            ltf_raw = mtf_raw  # MTF ko LTF ke roop mein use karo
        else:
            logger.warning(
                "SB: Insufficient candles | symbol=%s | htf=%d ltf=%d",
                symbol,
                len(htf_raw),
                len(ltf_raw),
            )
            return _null_sb_signal(symbol)

    logger.info(
        "SB candle counts | symbol=%s | htf=%d mtf=%d ltf=%d",
        symbol,
        len(htf_raw),
        len(mtf_raw),
        len(ltf_raw),
    )

    def _to_df(candles: list) -> pd.DataFrame:
        rows = []
        for c in candles:
            if hasattr(c, "open"):
                rows.append({
                    "ts": c.timestamp,
                    "open": float(c.open),
                    "high": float(c.high),
                    "low": float(c.low),
                    "close": float(c.close),
                    "volume": float(c.volume),
                })
            else:
                rows.append({
                    "ts": c.get("ts", 0),
                    "open": float(c.get("open", 0)),
                    "high": float(c.get("high", 0)),
                    "low": float(c.get("low", 0)),
                    "close": float(c.get("close", 0)),
                    "volume": float(c.get("volume", 0)),
                })

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)
        df.index = pd.to_datetime(df["ts"], unit="s", utc=True)
        df = df.drop(columns=["ts"])
        return df

    df_1h = _to_df(htf_raw)
    df_5m = _to_df(mtf_raw) if mtf_raw else _to_df(ltf_raw)
    df_2m = _to_df(ltf_raw)
    df_4h = _to_df(htf4h_raw) if htf4h_raw else pd.DataFrame()

    if ltf_tf == "1":
        df_2m = df_2m.resample("2min").agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }).dropna()
        logger.info("SB resampled 1M->2M | bars=%d", len(df_2m))

    if df_1h.empty or df_2m.empty:
        return _null_sb_signal(symbol)

    # ── User ka actual capital aur risk % fetch karo ─────────────
    try:
        from apps.wallet.models import Wallet
        from decimal import Decimal
        _wallet = Wallet.objects.get(user=strategy.user, currency="INR")
        _capital = float(_wallet.available_balance + _wallet.locked_balance)
    except Exception:
        _capital = float(strategy.parameters.get("capital", 100_000))

    try:
        _tp = strategy.user.trading_profile
        if _tp.risk_per_trade_pct:
            _risk_pct = float(_tp.risk_per_trade_pct) * 100  # 0.10 → 10.0
        else:
            _risk_pct = float(strategy.parameters.get("risk_pct", 1.0))
    except Exception:
        _risk_pct = float(strategy.parameters.get("risk_pct", 1.0))

    logger.info(
        "SB init | user=%s | capital=%.0f | risk_pct=%.1f%%",
        strategy.user.id, _capital, _risk_pct,
    )

    sb = SilverBullet2MStrategy(
        account_balance=_capital,
        risk_per_trade_pct=_risk_pct,
        min_rr=float(strategy.parameters.get("min_rr", 3.0)),
        sl_buffer_points=float(strategy.parameters.get("sl_buffer", 5.0)),
        enable_pd_zone_reanchor=bool(
            strategy.parameters.get("enable_pd_zone_reanchor", True)
        ),
    )

    try:
        sig = sb.analyze(
            symbol=symbol,
            df_1h=df_1h,
            df_5m=df_5m,
            df_2m=df_2m,
            df_4h=df_4h if not df_4h.empty else None,
        )
    except Exception as e:
        logger.error("SB analyze error | symbol=%s | err=%s", symbol, e, exc_info=True)
        return _null_sb_signal(symbol)

    if sig is None:
        if getattr(strategy, "algo_name", "") != "confluence_options":
            return _null_sb_signal(symbol)

    # Duplicate check — Order model se check karo (symbol + user)
    try:
        from apps.orders.models import Order as _Order
        from django.db.models import Q

        clean_symbol = symbol.replace("NSE:", "").replace("-INDEX", "").strip()
        _user = getattr(strategy, 'user', None)
        _qs = _Order.objects.filter(
            Q(symbol_display__icontains=clean_symbol) | Q(asset__symbol__icontains=clean_symbol),
            status__in=["open", "pending"],
        )
        if _user:
            _qs = _qs.filter(user=_user)
        already_open = _qs.exists()

        if already_open:
            logger.info(
                "[SB] Duplicate skip: open position exists for %s", symbol,
            )
            return _null_sb_signal(symbol)

    except Exception as e:
        logger.warning("Duplicate check failed | %s", e)

    # ── Confluence Options check ─────────────────────────────────
    try:
        if getattr(strategy, "algo_name", "") == "confluence_options":
            from apps.backtest.algos.confluence_options import ConfluenceOptionsAlgo
            df5_list = [
                {"high": float(r.high), "low": float(r.low), "close": float(r.close)}
                for _, r in df_5m.iterrows()
            ] if not df_5m.empty else []
            if sig is not None:
                sb_dict = {"direction": sig.direction.value, "confluence_score": sig.confluence_score, "score": sig.confluence_score}
            else:
                sb_dict = {"direction": None, "confluence_score": 0, "score": 0}
            algo = ConfluenceOptionsAlgo(parameters=getattr(strategy, "parameters", {}), risk_config=getattr(strategy, "risk_config", {}))
            conf_signal = algo.generate_signal(symbol=symbol, candles_5m=df5_list, candles_15m=[], candles_1h=[], sb_signal=sb_dict)
            if conf_signal:
                logger.info("✅ Confluence signal | %s | %s | combined=%.1f", symbol, conf_signal["option_type"], conf_signal["confidence"])
                sig_meta = sig.to_dict() if sig is not None else {}
                sig_meta.update(conf_signal)
                return {"signal_type": conf_signal["signal_type"], "symbol": symbol, "price": Decimal(str(conf_signal["price"])), "reason": f'Confluence {conf_signal["option_type"]} | SB={conf_signal["sb_score"]} MC={conf_signal["mc_score"]}', "metadata": sig_meta, "result": "executed", "order": None}
            else:
                if sig is not None:
                    logger.info("Confluence: SB ok but MC failed | %s", symbol)
                else:
                    logger.debug("Confluence: no SB + MC criteria not met | %s", symbol)
                return _null_sb_signal(symbol)
    except Exception as _ce:
        logger.warning("Confluence check error | %s", _ce)

    # Order placement is handled by services.py _handle_ict_signal
    # Do NOT place order here — it causes duplicates

    # Save signal + WebSocket push
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer
        from apps.strategies.models import StrategySignal

        StrategySignal.objects.create(
            strategy=strategy,
            signal_type=sig.direction.value,
            symbol=symbol,
            price=Decimal(str(sig.entry_price)),
            reason=sig.notes,
            metadata=sig.to_dict(),
            result="executed",
        )

        layer = get_channel_layer()
        if layer:
            async_to_sync(layer.group_send)(
                f"user_{strategy.user_id}",
                {
                    "type": "new_signal",
                    "direction": sig.direction.value,
                    "symbol": sig.symbol,
                    "entry": sig.entry_price,
                    "sl": sig.stop_loss,
                    "target1": sig.take_profit1,
                    "target2": sig.take_profit2,
                    "confidence": sig.confluence_score,
                    "reason": sig.notes,
                    "strategy_id": str(strategy.id),
                    "algo": "ict_silver_bullet",
                    "rr": sig.rr_ratio,
                    "tags": sig.tags,
                    "position": sig.position_size,
                    "risk_inr": sig.risk_amount,
                    "bias": sig.bias,
                    "killzone": sig.killzone,
                },
            )
    except Exception as e:
        logger.warning("SB WS push failed | %s", e)

    return {
        "signal_type": sig.direction.value,
        "symbol": sig.symbol,
        "price": Decimal(str(sig.entry_price)),
        "reason": sig.notes,
        "metadata": sig.to_dict(),
        "result": "executed",
        "order": None,
    }


def _null_sb_signal(symbol: str) -> dict:
    from decimal import Decimal

    return {
        "signal_type": "hold",
        "symbol": symbol,
        "price": Decimal("0"),
        "reason": "No Silver Bullet setup",
        "metadata": {},
        "result": "skipped",
        "order": None,
    }