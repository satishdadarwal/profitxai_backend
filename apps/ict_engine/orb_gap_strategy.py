"""
ORB + Gap Strategy (Options Buyer)
====================================
Opening Range Breakout (9:20-9:45) + Prior Day Gap analysis
+ ICT MSS confirmation = High-confidence CE/PE buy

Logic:
1. Prior day close vs today's open -> gap %
2. Opening range (9:20-9:45) high/low captured
3. Gap bias:
   - Large gap (>1%): continuation bias (gap-and-go direction)
   - Small/medium gap (<1%): fade-first (opposite of gap), fallback to
     continuation if range breaks against gap direction
4. Entry: range breakout in bias direction + 2M MSS (CHoCH) confirmation
5. SL: ATR-based (entry +/- ATR*1.0), clipped to range boundary
6. TP: 1:3 RR
7. Pyramiding: after 1:1 RR hit, if fresh HH/HL (long) or LH/LL (short)
   structure forms, add position (max 2x base lots)
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
    BreakType,
    detect_bos_choch,
    swing_indices,
)

logger = logging.getLogger(__name__)

from apps.backtest.algos.confluence_options import (
    NSE_LOT_SIZES,
    STRIKE_STEPS,
    _atr,
    _strike_price,
    _dte,
)


class ORBDirection(str, Enum):
    LONG = "long"
    SHORT = "short"
    NONE = "none"


@dataclass
class ORBGapSignal:
    direction: ORBDirection
    symbol: str
    entry_price: float
    stop_loss: float
    take_profit1: float
    take_profit2: float

    gap_pct: float
    gap_type: str
    bias: str
    orb_high: float
    orb_low: float

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
            "symbol": self.symbol,
            "entry_price": round(self.entry_price, 2),
            "stop_loss": round(self.stop_loss, 2),
            "take_profit_1": round(self.take_profit1, 2),
            "take_profit_2": round(self.take_profit2, 2),
            "gap_pct": round(self.gap_pct, 3),
            "gap_type": self.gap_type,
            "bias": self.bias,
            "orb_high": round(self.orb_high, 2),
            "orb_low": round(self.orb_low, 2),
            "risk_points": round(self.risk_points, 2),
            "reward_points": round(self.reward_points, 2),
            "rr_ratio": round(self.rr_ratio, 2),
            "atr": round(self.atr, 2),
            "confluence": round(self.confluence_score, 1),
            "tags": self.tags,
            "notes": self.notes,
        }


class ORBGapStrategy:
    """
    ORB (9:20-9:45 IST) + Prior day Gap + ICT MSS confirmation.
    """

    LARGE_GAP_PCT = 1.0
    SMALL_GAP_PCT = 0.2

    ORB_START = dt.time(9, 20)
    ORB_END = dt.time(9, 45)

    def __init__(
        self,
        min_rr: float = 3.0,
        atr_sl_mult: float = 1.0,
        mss_lookback_bars: int = 30,
    ):
        self.min_rr = min_rr
        self.atr_sl_mult = atr_sl_mult
        self.mss_lookback = mss_lookback_bars

    def _compute_gap(self, df_daily: pd.DataFrame, today_open: float) -> tuple[float, str]:
        if df_daily.empty or len(df_daily) < 2:
            return 0.0, "flat"
        prev_close = float(df_daily["close"].iloc[-2])
        if prev_close == 0:
            return 0.0, "flat"
        gap_pct = (today_open - prev_close) / prev_close * 100
        if gap_pct > self.SMALL_GAP_PCT:
            gap_type = "gap_up"
        elif gap_pct < -self.SMALL_GAP_PCT:
            gap_type = "gap_down"
        else:
            gap_type = "flat"
        return gap_pct, gap_type

    def _opening_range(self, df_intraday: pd.DataFrame) -> Optional[tuple[float, float]]:
        if df_intraday.empty:
            return None
        ist = df_intraday.index.tz_convert("Asia/Kolkata")
        mask = (ist.time >= self.ORB_START) & (ist.time <= self.ORB_END)
        window = df_intraday[mask]
        if window.empty:
            return None
        return float(window["high"].max()), float(window["low"].min())

    def _confirm_mss(self, df_2m: pd.DataFrame, direction: str, after_time: pd.Timestamp) -> bool:
        df_after = df_2m[df_2m.index > after_time]
        if len(df_after) < 6:
            return False

        sh_idx, sl_idx = swing_indices(df_after, method="fractal", left_bars=2, right_bars=2)
        breaks = detect_bos_choch(df_after, sh_idx, sl_idx)
        if not breaks:
            return False

        want_dir = BreakDirection.BULLISH if direction == "long" else BreakDirection.BEARISH
        for b in reversed(breaks):
            if b.direction == want_dir:
                return True
        return False

    def analyze(
        self,
        symbol: str,
        df_daily: pd.DataFrame,
        df_2m: pd.DataFrame,
    ) -> Optional[ORBGapSignal]:
        if df_daily.empty or len(df_daily) < 2 or df_2m.empty:
            return None

        ist = df_2m.index.tz_convert("Asia/Kolkata")
        today = ist[-1].date()
        today_mask = ist.date == today
        df_today = df_2m[today_mask]
        if df_today.empty:
            return None

        today_open = float(df_today["open"].iloc[0])
        gap_pct, gap_type = self._compute_gap(df_daily, today_open)

        orb = self._opening_range(df_today)
        if orb is None:
            return None
        orb_high, orb_low = orb

        spot = float(df_today["close"].iloc[-1])
        last_ts = df_today.index[-1]

        ist_today = df_today.index.tz_convert("Asia/Kolkata")
        orb_end_mask = ist_today.time <= self.ORB_END
        if not orb_end_mask.any():
            return None
        orb_end_ts = df_today.index[orb_end_mask][-1]

        if last_ts <= orb_end_ts:
            return None

        direction: Optional[str] = None
        bias_label = "continuation"

        broke_high = spot > orb_high
        broke_low = spot < orb_low

        if not broke_high and not broke_low:
            return None

        if abs(gap_pct) >= self.LARGE_GAP_PCT:
            if gap_type == "gap_up" and broke_high:
                direction = "long"
                bias_label = "continuation"
            elif gap_type == "gap_down" and broke_low:
                direction = "short"
                bias_label = "continuation"
            else:
                return None
        else:
            if broke_high:
                direction = "long"
                bias_label = "fade" if gap_type == "gap_down" else "continuation"
            elif broke_low:
                direction = "short"
                bias_label = "fade" if gap_type == "gap_up" else "continuation"

        if direction is None:
            return None

        if not self._confirm_mss(df_today, direction, orb_end_ts):
            logger.debug("[%s] ORB breakout %s but no MSS confirmation yet", symbol, direction)
            return None

        candles_list = [
            {"high": float(r.high), "low": float(r.low), "close": float(r.close)}
            for _, r in df_today.iterrows()
        ]
        atr_val = _atr(candles_list, period=14)
        if atr_val <= 0:
            return None

        if direction == "long":
            sl = spot - self.atr_sl_mult * atr_val
            sl = max(sl, orb_low - atr_val * 0.25)
            risk = spot - sl
            if risk <= 0:
                return None
            tp1 = spot + risk * 1.0
            tp2 = spot + risk * self.min_rr
        else:
            sl = spot + self.atr_sl_mult * atr_val
            sl = min(sl, orb_high + atr_val * 0.25)
            risk = sl - spot
            if risk <= 0:
                return None
            tp1 = spot - risk * 1.0
            tp2 = spot - risk * self.min_rr

        reward = abs(tp2 - spot)
        rr = round(reward / risk, 2) if risk > 0 else 0.0
        if rr < self.min_rr * 0.8:
            return None

        score = 40.0
        tags = ["ORB_BREAK", "MSS"]
        if abs(gap_pct) >= self.LARGE_GAP_PCT:
            score += 30
            tags.append("LARGE_GAP_CONT")
        elif bias_label == "fade":
            score += 15
            tags.append("GAP_FADE")
        else:
            score += 10
            tags.append("GAP_CONT")
        if rr >= self.min_rr:
            score += 15
            tags.append("RR_OK")
        score = min(score, 100.0)

        notes = (
            f"ORB {orb_low:.2f}-{orb_high:.2f} | gap={gap_pct:.2f}% ({gap_type}) | "
            f"bias={bias_label} | dir={direction}"
        )

        return ORBGapSignal(
            direction=ORBDirection(direction),
            symbol=symbol,
            entry_price=spot,
            stop_loss=sl,
            take_profit1=tp1,
            take_profit2=tp2,
            gap_pct=gap_pct,
            gap_type=gap_type,
            bias=bias_label,
            orb_high=orb_high,
            orb_low=orb_low,
            risk_points=risk,
            reward_points=reward,
            rr_ratio=rr,
            atr=atr_val,
            confluence_score=score,
            tags=tags,
            notes=notes,
        )

    def check_pyramid(
        self,
        df_2m: pd.DataFrame,
        direction: str,
        entry_price: float,
        tp1_hit: bool,
    ) -> bool:
        if not tp1_hit or df_2m.empty or len(df_2m) < 10:
            return False

        sh_idx, sl_idx = swing_indices(df_2m, method="fractal", left_bars=2, right_bars=2)
        breaks = detect_bos_choch(df_2m, sh_idx, sl_idx)
        if not breaks:
            return False

        want_dir = BreakDirection.BULLISH if direction == "long" else BreakDirection.BEARISH
        last_break = breaks[-1]
        if last_break.direction != want_dir:
            return False

        if direction == "long":
            return last_break.break_close > entry_price
        else:
            return last_break.break_close < entry_price


def _null_orb_signal(symbol: str) -> dict:
    return {
        "signal_type": "hold",
        "symbol": symbol,
        "price": Decimal("0"),
        "reason": "No ORB+Gap setup",
        "metadata": {},
        "result": "skipped",
        "order": None,
    }


def execute_orb_gap_cycle(strategy, symbol: str) -> dict:
    """Live/paper cycle entrypoint, mirrors execute_silver_bullet_cycle pattern."""
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
        return df

    try:
        daily_raw = fetch_candles_for_strategy(strategy, symbol, "D", bars=30) or []
        ltf_raw = fetch_candles_for_strategy(strategy, symbol, "1", bars=500) or []
    except TypeError:
        try:
            daily_raw = fetch_candles_for_strategy(strategy, symbol, "D") or []
            ltf_raw = fetch_candles_for_strategy(strategy, symbol, "1") or []
        except Exception as e:
            logger.error("ORB candle fetch error | symbol=%s | err=%s", symbol, e)
            return _null_orb_signal(symbol)
    except Exception as e:
        logger.error("ORB candle fetch error | symbol=%s | err=%s", symbol, e)
        return _null_orb_signal(symbol)

    df_daily = _to_df(daily_raw)
    df_2m = _to_df(ltf_raw)

    if not df_2m.empty:
        df_2m = df_2m.resample("2min").agg({
            "open": "first", "high": "max", "low": "min",
            "close": "last", "volume": "sum",
        }).dropna()

    if df_daily.empty or df_2m.empty:
        return _null_orb_signal(symbol)

    orb = ORBGapStrategy(
        min_rr=float(strategy.parameters.get("min_rr", 3.0)),
        atr_sl_mult=float(strategy.parameters.get("atr_sl_mult", 1.0)),
    )

    try:
        sig = orb.analyze(symbol=symbol, df_daily=df_daily, df_2m=df_2m)
    except Exception as e:
        logger.error("ORB analyze error | symbol=%s | err=%s", symbol, e, exc_info=True)
        return _null_orb_signal(symbol)

    if sig is None:
        return _null_orb_signal(symbol)

    try:
        from apps.orders.models import Order as _Order
        from django.db.models import Q
        clean_symbol = symbol.replace("NSE:", "").replace("-INDEX", "").strip()
        _user = getattr(strategy, "user", None)
        _qs = _Order.objects.filter(
            Q(symbol_display__icontains=clean_symbol) | Q(asset__symbol__icontains=clean_symbol),
            status__in=["open", "pending"],
        )
        if _user:
            _qs = _qs.filter(user=_user)
        if _qs.exists():
            logger.info("[ORB] Duplicate skip: open position exists for %s", symbol)
            return _null_orb_signal(symbol)
    except Exception as e:
        logger.warning("[ORB] Duplicate check failed | %s", e)

    option_type = "CE" if sig.direction == ORBDirection.LONG else "PE"
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
            logger.debug("[ORB] %s delta=%.3f out of range | %s", option_type, delta, symbol)
            return _null_orb_signal(symbol)
        if theta < -15:
            logger.debug("[ORB] %s theta=%.2f too high decay | %s", option_type, theta, symbol)
            return _null_orb_signal(symbol)
    except Exception:
        pass

    logger.info(
        "✅ ORB+Gap signal | %s | %s | dir=%s | gap=%.2f%% (%s) | bias=%s | "
        "score=%.1f | RR=%.2f | strike=%d | DTE=%d",
        symbol, option_type, sig.direction.value, sig.gap_pct, sig.gap_type,
        sig.bias, sig.confluence_score, sig.rr_ratio, strike, dte,
    )

    sig_meta = sig.to_dict()
    sig_meta.update({
        "option_type": option_type,
        "strike": strike,
        "dte": dte,
        "setup_type": f"ORB_Gap_{option_type}_{symbol}",
    })

    return {
        "signal_type": "buy",
        "symbol": symbol,
        "price": Decimal(str(spot)),
        "reason": f"ORB+Gap {option_type} | {sig.notes}",
        "metadata": sig_meta,
        "result": "executed",
        "order": None,
    }
