# apps/ict_engine/scanner.py
#
# ── FIXES ────────────────────────────────────────────────────────────────────
#  #S1  scan_sync() method added — SignalHandler yahi call karta tha,
#       lekin exist hi nahi karta tha → AttributeError → saare signals fail
#       ab: candle_service se data fetch → run_mtf_analysis → scan()
#  #S2  _tf_label_map: candle_service timeframe strings ("15") →
#       runner timeframe labels ("15m") correct mapping
#  #S3  Insufficient data guard: agar candle_service empty return kare
#       toh clear error log, no crash
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import logging
import time
from typing import Optional

import numpy as np
import pandas as pd

from .base import RiskParameters, Signal, SignalDirection, SignalStatus, SignalStrength
from .ict import (
    BreakDirection,
    FairValueGap,
    FVGStatus,
    FVGType,
    KillZoneContext,
    LiqStatus,
    LiquidityMap,
    MTFAnalysis,
    OBStatus,
    OBType,
    OrderBlock,
    TFSnapshot,
)

logger = logging.getLogger(__name__)

# ── candle_service timeframe string → ICT engine TF label ─────────────────────
# candle_service uses "1", "5", "15", "60", "240", "1440"
# run_mtf_analysis expects "1m", "5m", "15m", "1H", "4H", "1D"
_TF_TO_LABEL: dict[str, str] = {
    "1":    "1m",
    "5":    "5m",
    "15":   "15m",
    "30":   "30m",
    "60":   "1H",
    "120":  "2H",
    "240":  "4H",
    "1440": "1D",
    # already-label passthroughs
    "1m":   "1m",
    "5m":   "5m",
    "15m":  "15m",
    "30m":  "30m",
    "1H":   "1H",
    "4H":   "4H",
    "1D":   "1D",
}

# MTF config per execution timeframe — anchor higher TF se confluence leta hai
_MTF_CONFIG: dict[str, dict] = {
    "1m":  {"anchor": "15m",  "tfs": ["1H", "15m", "5m",  "1m"]},
    "5m":  {"anchor": "1H",   "tfs": ["4H", "1H",  "15m", "5m"]},
    "15m": {"anchor": "4H",   "tfs": ["1D", "4H",  "1H",  "15m"]},
    "30m": {"anchor": "4H",   "tfs": ["1D", "4H",  "1H",  "30m"]},
    "1H":  {"anchor": "1D",   "tfs": ["1D", "4H",  "1H"]},
    "4H":  {"anchor": "1D",   "tfs": ["1D", "4H"]},
    "1D":  {"anchor": "1D",   "tfs": ["1D"]},
}


def _atr_at(df, period=14):
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat(
        [high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1
    ).max(axis=1)
    atr_series = tr.rolling(period).mean()
    val = atr_series.iloc[-1]
    return float(val) if not np.isnan(val) else float((df["high"] - df["low"]).mean())


def _select_entry(direction, snap, current_price, atr):
    ob = snap.nearest_ob
    fvg = snap.nearest_fvg
    if ob and ob.status in {OBStatus.PRISTINE, OBStatus.TESTED}:
        if direction == "long" and ob.ob_type == OBType.BULLISH:
            return ob.proximal
        if direction == "short" and ob.ob_type == OBType.BEARISH:
            return ob.proximal
    if fvg and fvg.status in {FVGStatus.OPEN, FVGStatus.PARTIAL}:
        if direction == "long" and fvg.fvg_type == FVGType.BULLISH:
            return fvg.mid
        if direction == "short" and fvg.fvg_type == FVGType.BEARISH:
            return fvg.mid
    return current_price


def _place_stop(direction, entry, snap, current_price, atr, atr_sl_mult=1.5):
    buffer = atr * 0.25
    ob = snap.nearest_ob
    if ob:
        if direction == "long" and ob.ob_type == OBType.BULLISH:
            return ob.distal - buffer
        if direction == "short" and ob.ob_type == OBType.BEARISH:
            return ob.distal + buffer
    if direction == "long":
        hl = snap.structure.last_hl
        if hl:
            return hl.price - buffer
    else:
        lh = snap.structure.last_lh
        if lh:
            return lh.price + buffer
    return (
        entry - atr * atr_sl_mult if direction == "long" else entry + atr * atr_sl_mult
    )


def _select_targets(direction, entry, stop, liq_map, atr, min_rr=2.0):
    risk = abs(entry - stop)
    if direction == "long":
        tp1 = entry + risk * min_rr
        tp2 = entry + risk * 3.0
        tp3 = entry + risk * 4.5
        bsl = [
            l for l in liq_map.bsl_levels
            if l.price > entry and l.status == LiqStatus.INTACT
        ]
        if bsl:
            tp1 = bsl[0].price
            if len(bsl) > 1:
                tp2 = bsl[1].price
            if len(bsl) > 2:
                tp3 = bsl[2].price
    else:
        tp1 = entry - risk * min_rr
        tp2 = entry - risk * 3.0
        tp3 = entry - risk * 4.5
        ssl = [
            l for l in liq_map.ssl_levels
            if l.price < entry and l.status == LiqStatus.INTACT
        ]
        if ssl:
            tp1 = ssl[0].price
            if len(ssl) > 1:
                tp2 = ssl[1].price
            if len(ssl) > 2:
                tp3 = ssl[2].price
    actual_rr = abs(tp1 - entry) / risk if risk else 0
    if actual_rr < min_rr:
        tp1 = entry + risk * min_rr if direction == "long" else entry - risk * min_rr
    return round(tp1, 8), round(tp2, 8), round(tp3, 8)


def _size_position(entry, stop, risk_params):
    risk_amount = risk_params.account_balance * (risk_params.risk_per_trade_pct / 100.0)
    risk_per_unit = abs(entry - stop)
    if risk_per_unit == 0:
        return 0.0, 0.0
    return round(risk_amount / risk_per_unit, 6), round(risk_amount, 2)


def _classify_strength(confluence, rr, aligned):
    if confluence >= 80 and rr >= 3.0 and aligned:
        return SignalStrength.VERY_STRONG
    if confluence >= 65 and rr >= 2.5:
        return SignalStrength.STRONG
    if confluence >= 50 and rr >= 2.0:
        return SignalStrength.MODERATE
    return SignalStrength.WEAK


def _build_tags(direction, snap, mtf):
    tags = [direction.upper()]
    ob = snap.nearest_ob
    if ob and ob.ob_type.value == direction.replace("long", "bullish").replace("short", "bearish"):
        tags.append("OB")
        if ob.is_institutional:
            tags.append("INST_OB")
    if snap.nearest_fvg:
        tags.append("FVG")
    if snap.last_choch:
        tags.append("CHoCH")
    if snap.last_bos:
        tags.append("BOS")
    if (
        mtf.killzone
        and mtf.killzone.in_any_killzone
        and mtf.killzone.highest_priority_zone
    ):
        tags.append(f"KZ_{mtf.killzone.highest_priority_zone.name.value.upper()}")
    for s in snap.liquidity.recent_sweeps[:2]:
        if s.is_stop_hunt:
            tags.append("STOP_HUNT")
            break
    if mtf.aligned:
        tags.append("MTF_ALIGNED")
    return list(dict.fromkeys(tags))


def _candle_bars_to_df(candles: list) -> pd.DataFrame:
    """CandleBar list → pandas DataFrame (ICT engine ka expected format)."""
    if not candles:
        return pd.DataFrame()

    rows = [
        {
            "open":   c.open,
            "high":   c.high,
            "low":    c.low,
            "close":  c.close,
            "volume": c.volume,
        }
        for c in candles
    ]
    df = pd.DataFrame(rows)
    # DatetimeIndex set karo (UTC)
    df.index = pd.to_datetime(
        [c.timestamp for c in candles], unit="s", utc=True
    )
    return df


class Scanner:
    def __init__(
        self, risk_params=None, min_confluence=60.0, min_rr=2.0, atr_period=14
    ):
        self.risk = risk_params or RiskParameters()
        self.min_confluence = min_confluence
        self.min_rr = min_rr
        self.atr_period = atr_period

    # ── Existing method (unchanged) ────────────────────────────
    def scan(self, mtf, execution_df):
        confluence = mtf.confluence
        if confluence is None:
            return None
        if confluence.total < self.min_confluence:
            return None
        direction = confluence.direction
        if direction not in ("long", "short"):
            return None
        exec_snap = mtf.snapshots.get(mtf.execution_tf)
        if exec_snap is None:
            return None
        if execution_df.empty:
            return None
        current_price = float(execution_df["close"].iloc[-1])
        atr = _atr_at(execution_df, self.atr_period)
        entry = _select_entry(direction, exec_snap, current_price, atr)
        if entry is None:
            return None
        stop = _place_stop(direction, entry, exec_snap, current_price, atr)
        if direction == "long" and stop >= entry:
            return None
        if direction == "short" and stop <= entry:
            return None
        tp1, tp2, tp3 = _select_targets(
            direction, entry, stop, exec_snap.liquidity, atr, self.min_rr
        )
        risk_pips = abs(entry - stop)
        rr = round(abs(tp1 - entry) / risk_pips, 2) if risk_pips else 0.0
        if rr < self.min_rr:
            return None
        size, risk_amount = _size_position(entry, stop, self.risk)
        strength = _classify_strength(confluence.total, rr, mtf.aligned)
        tags = _build_tags(direction, exec_snap, mtf)
        kz_name = ""
        if mtf.killzone and mtf.killzone.highest_priority_zone:
            kz_name = mtf.killzone.highest_priority_zone.name.value
        return Signal(
            symbol=mtf.symbol,
            direction=SignalDirection(direction),
            strength=strength,
            status=SignalStatus.PENDING,
            entry_price=round(entry, 8),
            stop_loss=round(stop, 8),
            take_profit_1=round(tp1, 8),
            take_profit_2=round(tp2, 8) if tp2 else None,
            take_profit_3=round(tp3, 8) if tp3 else None,
            risk_reward=rr,
            risk_amount=risk_amount,
            position_size=size,
            confluence_score=confluence.total,
            confluence_breakdown=confluence.breakdown,
            timeframes=list(mtf.snapshots.keys()),
            anchor_tf=mtf.anchor_tf,
            execution_tf=mtf.execution_tf,
            killzone=kz_name,
            tags=tags,
            raw_analysis=mtf,
        )

    def scan_many(self, analyses):
        signals = []
        for mtf, df in analyses:
            sig = self.scan(mtf, df)
            if sig and sig.is_actionable():
                signals.append(sig)
        return signals

    # ── NEW: scan_sync() — SignalHandler yahi call karta hai ────
    def scan_sync(
        self,
        symbol: str,
        timeframe: str,         # candle_service format: "15", "60", "240"
        mode: str,              # "auto" | "semi_auto" | "manual" (unused currently)
        bars: int = 500,
        strategy=None,          # optional — Fyers credentials ke liye
    ) -> list[dict]:
        """
        Complete synchronous scan pipeline:
          candle_service → DataFrame → run_mtf_analysis → scan()

        Returns list of raw signal dicts (SignalHandler ko yahi chahiye).
        Empty list on no signal or error (never raises).

        Args:
            symbol    — e.g. "NSE:NIFTY50-INDEX", "BTC-USDT"
            timeframe — candle_service format: "1","5","15","60","240","1440"
            mode      — trading mode string (future use)
            bars      — how many candles to fetch per timeframe
            strategy  — optional Strategy instance (Fyers credentials ke liye)
        """
        from .ict.mtf import run_mtf_analysis

        t0 = time.perf_counter()

        # ── Step 1: execution TF label resolve karo ────────────
        exec_label = _TF_TO_LABEL.get(str(timeframe), "15m")
        mtf_cfg = _MTF_CONFIG.get(exec_label, _MTF_CONFIG["15m"])
        anchor_label   = mtf_cfg["anchor"]
        all_tf_labels  = mtf_cfg["tfs"]

        # label → candle_service timeframe string (reverse map)
        _LABEL_TO_TF = {v: k for k, v in _TF_TO_LABEL.items() if len(k) <= 5}

        # ── Step 2: Har TF ke liye candles fetch karo ──────────
        from apps.common.candle_service import fetch_candles_for_strategy

        tf_data: dict[str, pd.DataFrame] = {}
        for label in all_tf_labels:
            tf_str = _LABEL_TO_TF.get(label, "15")
            tf_fetch_start = time.perf_counter()
            try:
                raw_candles = fetch_candles_for_strategy(
                    strategy=strategy,
                    symbol=symbol,
                    timeframe=tf_str,
                    bars=bars,
                )
                df = _candle_bars_to_df(raw_candles)
                tf_elapsed = round(time.perf_counter() - tf_fetch_start, 3)
                if len(df) < 30:
                    logger.warning(
                        "scan_sync: insufficient data | symbol=%s | tf=%s | bars=%d | fetch=%.3fs",
                        symbol, label, len(df), tf_elapsed,
                    )
                    tf_data[label] = pd.DataFrame()
                else:
                    tf_data[label] = df
                    # ✅ FIX: Per-TF fetch time log karo — slow fetches visible honge
                    # Normal: ~0.5-2s. Agar 4s+ dikhe toh network/Fyers slow hai.
                    log_fn = logger.warning if tf_elapsed > 3.0 else logger.debug
                    log_fn(
                        "scan_sync: fetched %d candles | symbol=%s | tf=%s | fetch=%.3fs",
                        len(df), symbol, label, tf_elapsed,
                    )
            except Exception as exc:
                tf_elapsed = round(time.perf_counter() - tf_fetch_start, 3)
                logger.error(
                    "scan_sync: fetch failed | symbol=%s | tf=%s | fetch=%.3fs | %s",
                    symbol, label, tf_elapsed, exc,
                )
                tf_data[label] = pd.DataFrame()

        # ── Step 3: Minimum data check ─────────────────────────
        valid_count = sum(1 for df in tf_data.values() if not df.empty)
        if valid_count < 2:
            logger.warning(
                "scan_sync: only %d/%d TFs have data for %s — skipping analysis",
                valid_count, len(all_tf_labels), symbol,
            )
            return []

        # ── Step 4: ICT MTF Analysis ───────────────────────────
        try:
            mtf: MTFAnalysis = run_mtf_analysis(
                symbol=symbol,
                tf_data={k: v for k, v in tf_data.items() if not v.empty},
                anchor_tf=anchor_label,
                execution_tf=exec_label,
            )
        except Exception as exc:
            logger.error(
                "scan_sync: ICT analysis failed | symbol=%s | %s", symbol, exc,
                exc_info=True,
            )
            return []

        # ── Step 5: Signal build ───────────────────────────────
        exec_df = tf_data.get(exec_label, pd.DataFrame())
        signal = self.scan(mtf, exec_df)

        elapsed = round(time.perf_counter() - t0, 3)

        if signal is None or not signal.is_actionable():
            logger.info(
                "scan_sync: no actionable signal | symbol=%s | confluence=%.1f | elapsed=%.3fs",
                symbol,
                mtf.confluence.total if mtf.confluence else 0,
                elapsed,
            )
            return []

        # ── Step 6: Signal dict banana (SignalHandler expects dicts) ─
        raw = {
            "symbol":       signal.symbol,
            "direction":    signal.direction.value if signal.direction else "buy",
            "signal_type":  _pick_signal_type(signal.tags),
            "strength":     signal.strength.value,
            "entry_price":  signal.entry_price,
            "stop_loss":    signal.stop_loss,
            "take_profit":  signal.take_profit_1,
            "rr_ratio":     signal.risk_reward,
            "lots":         signal.position_size or 1,
            "confluence":   signal.confluence_score,
            "tags":         signal.tags,
            "killzone":     signal.killzone,
            "tp2":          signal.take_profit_2,
            "tp3":          signal.take_profit_3,
        }

        logger.info(
            "scan_sync: SIGNAL | symbol=%s | dir=%s | entry=%.4f | rr=%.2f | "
            "confluence=%.1f | tags=%s | elapsed=%.3fs",
            symbol, raw["direction"], raw["entry_price"],
            raw["rr_ratio"], raw["confluence"], raw["tags"], elapsed,
        )

        return [raw]


def _pick_signal_type(tags: list[str]) -> str:
    """Tags se primary signal type nikalo."""
    priority = ["INST_OB", "OB", "FVG", "CHoCH", "BOS", "STOP_HUNT"]
    for tag in priority:
        if tag in tags:
            return tag.lower().replace("inst_ob", "institutionalOrderBlock")
    return "orderBlock"