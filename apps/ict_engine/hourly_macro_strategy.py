"""
Hourly Macro Scalp Strategy
=============================
Concept (inspired by ICT "Hourly Macro" / Mini_ORB wick-zone scalping):

1. Previous CLOSED 1H candle's high/low = "macro zone" (top wick zone / bottom
   wick zone), with a midline at 50%.
2. On the 15m timeframe (entry trigger), evaluate TWO possible setups every
   cycle:
     a) REVERSAL (fade): price wicks INTO the zone (top or bottom) and shows
        rejection (long wick / engulfing) on 15m -> trade towards the
        opposite zone edge / midline.
     b) CONTINUATION (breakout): price cleanly BREAKS the zone edge with a
        strong 15m close + ICT MSS (CHoCH) confirmation in that direction ->
        trade towards 1:2 / 1:3 RR.
3. Both setups are scored (confluence_score). Whichever setup scores higher
   is selected for the signal. If neither crosses min_score, hold.
4. SL: just beyond the zone edge (+/- small ATR buffer).
   TP (reversal): opposite zone edge / midline.
   TP (continuation): risk * min_rr.

Symbols: BTCUSD, ETHUSD (Delta Exchange perps) AND NIFTY/BANKNIFTY/SENSEX
(NSE options buyer). Auto-detected from symbol name; the zone/reversal/
continuation/scoring logic is instrument-agnostic. For options, direction
is encoded as CE/PE (both are "buy" orders); Greeks gate (delta 0.25-0.65,
theta > -15) is applied before emitting a signal.
"""
from __future__ import annotations

import logging
import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Optional

import pandas as pd

from .ict import (
    BreakDirection,
    detect_bos_choch,
    swing_indices,
)
from apps.backtest.algos.confluence_options import _atr, _strike_price, _dte

_OPTIONS_SYMBOLS = frozenset({"NIFTY", "BANKNIFTY", "SENSEX"})

logger = logging.getLogger(__name__)


class MacroDirection(str, Enum):
    LONG = "long"
    SHORT = "short"
    NONE = "none"


class MacroSetupType(str, Enum):
    REVERSAL = "reversal"
    CONTINUATION = "continuation"


@dataclass
class HourlyMacroSignal:
    direction: MacroDirection
    setup_type: MacroSetupType
    symbol: str
    entry_price: float
    stop_loss: float
    take_profit: float
    zone_high: float
    zone_low: float
    zone_mid: float
    risk_points: float
    reward_points: float
    rr_ratio: float
    atr: float
    confluence_score: float
    tags: list = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "direction": self.direction.value,
            "setup_type": self.setup_type.value,
            "symbol": self.symbol,
            "entry_price": round(self.entry_price, 4),
            "stop_loss": round(self.stop_loss, 4),
            "take_profit": round(self.take_profit, 4),
            "zone_high": round(self.zone_high, 4),
            "zone_low": round(self.zone_low, 4),
            "zone_mid": round(self.zone_mid, 4),
            "risk_points": round(self.risk_points, 4),
            "reward_points": round(self.reward_points, 4),
            "rr_ratio": round(self.rr_ratio, 2),
            "atr": round(self.atr, 4),
            "confluence": round(self.confluence_score, 1),
            "tags": self.tags,
            "notes": self.notes,
        }


class HourlyMacroStrategy:
    """
    Previous-1H-candle wick-zone scalper. Entry trigger TF = 15m.
    """

    def __init__(
        self,
        min_rr: float = 2.0,
        atr_sl_mult: float = 0.5,
        min_score: float = 50.0,
        zone_buffer_atr_mult: float = 0.25,
    ):
        self.min_rr = min_rr
        self.atr_sl_mult = atr_sl_mult
        self.min_score = min_score
        self.zone_buffer_atr_mult = zone_buffer_atr_mult

    # ------------------------------------------------------------------
    # Zone detection
    # ------------------------------------------------------------------
    def _get_macro_zone(self, df_1h: pd.DataFrame) -> Optional[dict]:
        """
        Returns the previous CLOSED 1H candle's high/low/mid + colour.
        df_1h must have at least 2 rows (last row = current/forming candle,
        second-last = last closed candle).
        """
        if df_1h is None or len(df_1h) < 2:
            return None

        prev = df_1h.iloc[-2]
        zone_high = float(prev["high"])
        zone_low = float(prev["low"])
        if zone_high <= zone_low:
            return None

        is_green = float(prev["close"]) >= float(prev["open"])
        return {
            "high": zone_high,
            "low": zone_low,
            "mid": (zone_high + zone_low) / 2.0,
            "is_green": is_green,
            "ts": prev.name,
        }

    # ------------------------------------------------------------------
    # MSS confirmation (reuse ICT bos/choch on 15m)
    # ------------------------------------------------------------------
    def _confirm_mss(self, df_15m: pd.DataFrame, direction: str) -> bool:
        if len(df_15m) < 10:
            return False
        sh_idx, sl_idx = swing_indices(df_15m, method="fractal", left_bars=2, right_bars=2)
        breaks = detect_bos_choch(df_15m, sh_idx, sl_idx)
        if not breaks:
            return False
        want_dir = BreakDirection.BULLISH if direction == "long" else BreakDirection.BEARISH
        last_break = breaks[-1]
        return last_break.direction == want_dir
    # ------------------------------------------------------------------
    # Reversal setup evaluation (fade into zone)
    # ------------------------------------------------------------------
    def _eval_reversal(
        self, df_15m: pd.DataFrame, zone: dict, atr_val: float
    ) -> Optional[HourlyMacroSignal]:
        """
        Price wicks INTO the zone (touches/crosses zone_high from below, or
        zone_low from above) and the 15m candle shows rejection (closes back
        outside the zone, away from the wick). Trade towards opposite edge.
        """
        last = df_15m.iloc[-1]
        h, l, o, c = float(last["high"]), float(last["low"]), float(last["open"]), float(last["close"])
        zone_high, zone_low, zone_mid = zone["high"], zone["low"], zone["mid"]

        direction = None
        rejection_strength = 0.0

        # Wick into top zone from below, close back below zone_high -> SHORT (fade down)
        if h >= zone_low and c < zone_high and l < zone_high:
            # candle poked into or through the lower part of the top zone but closed below it
            if h >= zone_low and c <= zone_high:
                wick_into_zone = min(h, zone_high) - max(zone_low, min(o, c))
                candle_range = max(h - l, 1e-9)
                rejection_strength = wick_into_zone / candle_range
                if rejection_strength > 0.2 and c < o:
                    direction = "short"

        # Wick into bottom zone from above, close back above zone_low -> LONG (fade up)
        if direction is None and l <= zone_high and c > zone_low and h > zone_low:
            if l <= zone_high and c >= zone_low:
                wick_into_zone = min(zone_high, max(o, c)) - max(l, zone_low)
                candle_range = max(h - l, 1e-9)
                rejection_strength = wick_into_zone / candle_range
                if rejection_strength > 0.2 and c > o:
                    direction = "long"

        if direction is None:
            return None

        entry = c
        buffer = atr_val * self.zone_buffer_atr_mult

        if direction == "long":
            sl = zone_low - buffer
            tp = zone_mid if entry < zone_mid else zone_high
            if tp <= entry:
                tp = zone_high
            risk = entry - sl
            reward = tp - entry
        else:
            sl = zone_high + buffer
            tp = zone_mid if entry > zone_mid else zone_low
            if tp >= entry:
                tp = zone_low
            risk = sl - entry
            reward = entry - tp

        if risk <= 0 or reward <= 0:
            return None

        rr = reward / risk

        # Score: base 40 + rejection strength (up to 30) + zone-colour alignment (up to 20)
        score = 40.0
        score += min(rejection_strength * 60.0, 30.0)
        # Reversal aligns better when fading INTO a zone of opposite colour
        # (e.g. fading a red candle's low from above suggests exhaustion)
        if (direction == "long" and not zone["is_green"]) or (
            direction == "short" and zone["is_green"]
        ):
            score += 20.0
        else:
            score += 8.0
        if rr >= 1.0:
            score += 10.0
        score = min(score, 100.0)

        return HourlyMacroSignal(
            direction=MacroDirection(direction),
            setup_type=MacroSetupType.REVERSAL,
            symbol="",
            entry_price=entry,
            stop_loss=sl,
            take_profit=tp,
            zone_high=zone_high,
            zone_low=zone_low,
            zone_mid=zone_mid,
            risk_points=risk,
            reward_points=reward,
            rr_ratio=rr,
            atr=atr_val,
            confluence_score=score,
            tags=["hourly_macro", "reversal", "wick_zone_fade"],
            notes=f"Fade {direction} from prev-1H zone [{zone_low:.2f}-{zone_high:.2f}]",
        )

    # ------------------------------------------------------------------
    # Continuation setup evaluation (zone breakout + MSS)
    # ------------------------------------------------------------------
    def _eval_continuation(
        self, df_15m: pd.DataFrame, zone: dict, atr_val: float
    ) -> Optional[HourlyMacroSignal]:
        """
        15m candle CLOSES beyond the zone edge (clean break, not just a wick)
        with ICT MSS confirmation in that direction -> continuation trade.
        """
        last = df_15m.iloc[-1]
        c = float(last["close"])
        zone_high, zone_low, zone_mid = zone["high"], zone["low"], zone["mid"]

        direction = None
        if c > zone_high:
            direction = "long"
        elif c < zone_low:
            direction = "short"

        if direction is None:
            return None

        if not self._confirm_mss(df_15m, direction):
            return None

        entry = c
        buffer = atr_val * self.zone_buffer_atr_mult

        if direction == "long":
            sl = max(zone_low, entry - atr_val * max(self.atr_sl_mult, 0.1)) - buffer
            risk = entry - sl
            if risk <= 0:
                return None
            tp = entry + risk * self.min_rr
            reward = tp - entry
        else:
            sl = min(zone_high, entry + atr_val * max(self.atr_sl_mult, 0.1)) + buffer
            risk = sl - entry
            if risk <= 0:
                return None
            tp = entry - risk * self.min_rr
            reward = entry - tp

        rr = reward / risk if risk > 0 else 0.0
        if rr < self.min_rr * 0.8:
            return None

        # Score: base 45 + breakout distance beyond zone (up to 25) + MSS confirmed (20)
        zone_range = max(zone_high - zone_low, 1e-9)
        breakout_dist = (c - zone_high) if direction == "long" else (zone_low - c)
        breakout_pct = min(breakout_dist / zone_range, 1.0)

        score = 45.0
        score += breakout_pct * 25.0
        score += 20.0  # MSS confirmed
        if rr >= self.min_rr:
            score += 10.0
        score = min(score, 100.0)

        return HourlyMacroSignal(
            direction=MacroDirection(direction),
            setup_type=MacroSetupType.CONTINUATION,
            symbol="",
            entry_price=entry,
            stop_loss=sl,
            take_profit=tp,
            zone_high=zone_high,
            zone_low=zone_low,
            zone_mid=zone_mid,
            risk_points=risk,
            reward_points=reward,
            rr_ratio=rr,
            atr=atr_val,
            confluence_score=score,
            tags=["hourly_macro", "continuation", "zone_breakout_mss"],
            notes=f"Breakout {direction} beyond prev-1H zone [{zone_low:.2f}-{zone_high:.2f}] + MSS",
        )

    # ------------------------------------------------------------------
    # Main entrypoint
    # ------------------------------------------------------------------
    def analyze(
        self, symbol: str, df_1h: pd.DataFrame, df_15m: pd.DataFrame
    ) -> Optional[HourlyMacroSignal]:
        if df_1h is None or df_15m is None or df_1h.empty or df_15m.empty:
            return None
        if len(df_15m) < 15:
            return None

        zone = self._get_macro_zone(df_1h)
        if zone is None:
            return None

        candles_list = [
            {"high": float(r.high), "low": float(r.low), "close": float(r.close)}
            for _, r in df_15m.iterrows()
        ]
        atr_val = _atr(candles_list, period=14)
        if not atr_val or atr_val <= 0:
            return None

        rev_sig = self._eval_reversal(df_15m, zone, atr_val)
        cont_sig = self._eval_continuation(df_15m, zone, atr_val)

        best: Optional[HourlyMacroSignal] = None
        if rev_sig and rev_sig.confluence_score >= self.min_score:
            best = rev_sig
        if cont_sig and cont_sig.confluence_score >= self.min_score:
            if best is None or cont_sig.confluence_score > best.confluence_score:
                best = cont_sig

        if best is None:
            return None

        best.symbol = symbol
        return best


def _null_macro_signal(symbol: str) -> dict:
    return {
        "signal_type": "hold",
        "symbol": symbol,
        "price": Decimal("0"),
        "reason": "No Hourly Macro setup",
        "metadata": {},
        "result": "skipped",
        "order": None,
    }


def _execute_hourly_macro_options(strategy, symbol: str, sig: HourlyMacroSignal) -> dict:
    """Options execution wrapper for HourlyMacro signals (NIFTY/BANKNIFTY/SENSEX)."""
    try:
        from apps.orders.models import Order as _Order
        from django.db.models import Q
        _user = getattr(strategy, "user", None)
        _qs = _Order.objects.filter(
            Q(symbol_display__icontains=symbol) | Q(asset__symbol__icontains=symbol),
            status__in=["open", "pending"],
        )
        if _user:
            _qs = _qs.filter(user=_user)
        if _qs.exists():
            logger.info("[HourlyMacro] Duplicate skip: open position exists for %s", symbol)
            return _null_macro_signal(symbol)
    except Exception as e:
        logger.warning("[HourlyMacro] Duplicate check failed | %s", e)

    option_type = "CE" if sig.direction == MacroDirection.LONG else "PE"
    spot = sig.entry_price
    strike = _strike_price(spot, symbol, option_type, otm_shift=0)
    dte = _dte(symbol)

    try:
        from apps.options.black_scholes import compute_greeks
        T = max(dte / 365, 0.001)
        bs_type = "call" if option_type == "CE" else "put"
        g = compute_greeks(spot, strike, T, 0.065, 0.15, bs_type)
        delta = abs(g["delta"])
        theta = g["theta"]
        if not (0.25 <= delta <= 0.65):
            logger.debug("[HourlyMacro] %s delta=%.3f out of range | %s", option_type, delta, symbol)
            return _null_macro_signal(symbol)
        if theta < -15:
            logger.debug("[HourlyMacro] %s theta=%.2f too high decay | %s", option_type, theta, symbol)
            return _null_macro_signal(symbol)
    except Exception:
        pass

    logger.info(
        "✅ HourlyMacro Options signal | %s | %s | dir=%s | setup=%s | entry=%.2f | "
        "SL=%.2f | TP=%.2f | RR=%.2f | score=%.1f | strike=%d | DTE=%d",
        symbol, option_type, sig.direction.value, sig.setup_type.value,
        sig.entry_price, sig.stop_loss, sig.take_profit, sig.rr_ratio,
        sig.confluence_score, strike, dte,
    )

    sig_meta = sig.to_dict()
    sig_meta.update({
        "option_type": option_type,
        "strike": strike,
        "dte": dte,
        "setup_type": f"HourlyMacro_{sig.setup_type.value}_{option_type}_{symbol}",
    })

    return {
        "signal_type": "buy",
        "symbol": symbol,
        "price": Decimal(str(spot)),
        "reason": f"HourlyMacro {sig.setup_type.value} {option_type} | {sig.notes}",
        "metadata": sig_meta,
        "result": "executed",
        "order": None,
    }


def execute_hourly_macro_cycle(strategy, symbol: str) -> dict:
    """
    Live/paper cycle entrypoint for the Hourly Macro Scalp strategy.

    Phase 1 (this implementation): crypto perps via Delta Exchange
    (BTCUSD / ETHUSD). The candle-fetch helper here mirrors
    execute_orb_gap_cycle's pattern but pulls 1H + 15m candles instead
    of D + 1m.

    Order placement / execution is left to the existing strategy router
    (apps.strategies.services / signal_router), which already knows how
    to dispatch buy/sell signals for crypto symbols to Delta. This
    function only returns the standard signal dict
    (signal_type, symbol, price, reason, metadata, result, order) so it
    plugs into that router the same way ict_mtf / orb_gap_options do.
    """
    from apps.common.candle_service import fetch_candles_for_strategy

    def _to_df(candles: list) -> pd.DataFrame:
        rows = []
        for c in candles:
            if hasattr(c, "open"):
                rows.append({
                    "ts": c.timestamp,
                    "open": float(c.open), "high": float(c.high),
                    "low": float(c.low), "close": float(c.close),
                    "volume": float(c.volume),
                })
            else:
                rows.append({
                    "ts": c.get("ts", 0),
                    "open": float(c.get("open", 0)), "high": float(c.get("high", 0)),
                    "low": float(c.get("low", 0)), "close": float(c.get("close", 0)),
                    "volume": float(c.get("volume", 0)),
                })
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        df.index = pd.to_datetime(df["ts"], unit="s", utc=True)
        df = df.drop(columns=["ts"])
        return df.sort_index()

    try:
        ltf_raw = fetch_candles_for_strategy(strategy, symbol, "15", bars=300) or []
    except Exception as e:
        logger.error("HourlyMacro candle fetch error | symbol=%s | err=%s", symbol, e)
        return _null_macro_signal(symbol)

    df_15m = _to_df(ltf_raw)
    if df_15m.empty or len(df_15m) < 15:
        return _null_macro_signal(symbol)

    df_1h = df_15m.resample("1h").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    }).dropna()

    if len(df_1h) < 2:
        return _null_macro_signal(symbol)

    strat = HourlyMacroStrategy(
        min_rr=float(strategy.parameters.get("min_rr", 2.0)),
        atr_sl_mult=float(strategy.parameters.get("atr_sl_mult", 0.5)),
        min_score=float(strategy.parameters.get("min_score", 50.0)),
        zone_buffer_atr_mult=float(strategy.parameters.get("zone_buffer_atr_mult", 0.25)),
    )

    try:
        sig = strat.analyze(symbol=symbol, df_1h=df_1h, df_15m=df_15m)
    except Exception as e:
        logger.error("HourlyMacro analyze error | symbol=%s | err=%s", symbol, e, exc_info=True)
        return _null_macro_signal(symbol)

    if sig is None:
        return _null_macro_signal(symbol)

    if symbol.upper() in _OPTIONS_SYMBOLS:
        return _execute_hourly_macro_options(strategy, symbol, sig)

    # --- Crypto perp path (BTCUSD / ETHUSD) ---
    try:
        from apps.orders.models import Order as _Order
        from django.db.models import Q
        clean_symbol = symbol.replace("USD", "").replace("-USDT", "").strip()
        _user = getattr(strategy, "user", None)
        _qs = _Order.objects.filter(
            Q(symbol_display__icontains=clean_symbol) | Q(asset__symbol__icontains=clean_symbol),
            status__in=["open", "pending"],
        )
        if _user:
            _qs = _qs.filter(user=_user)
        if _qs.exists():
            logger.info("[HourlyMacro] Duplicate skip: open position exists for %s", symbol)
            return _null_macro_signal(symbol)
    except Exception as e:
        logger.warning("[HourlyMacro] Duplicate check failed | %s", e)

    side = "buy" if sig.direction == MacroDirection.LONG else "sell"

    logger.info(
        "✅ HourlyMacro signal | %s | dir=%s | setup=%s | entry=%.4f | "
        "SL=%.4f | TP=%.4f | RR=%.2f | score=%.1f | zone=[%.4f-%.4f]",
        symbol, sig.direction.value, sig.setup_type.value, sig.entry_price,
        sig.stop_loss, sig.take_profit, sig.rr_ratio, sig.confluence_score,
        sig.zone_low, sig.zone_high,
    )

    sig_meta = sig.to_dict()
    sig_meta.update({
        "setup_type": f"HourlyMacro_{sig.setup_type.value}_{symbol}",
    })

    return {
        "signal_type": side,
        "symbol": symbol,
        "price": Decimal(str(sig.entry_price)),
        "reason": f"HourlyMacro {sig.setup_type.value} {sig.direction.value} | {sig.notes}",
        "metadata": sig_meta,
        "result": "executed",
        "order": None,
    }
