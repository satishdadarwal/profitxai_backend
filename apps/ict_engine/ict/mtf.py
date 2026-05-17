from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from .bos_choch import (
    BreakDirection,
    BreakType,
    StructureBreak,
    current_bias,
    detect_bos_choch,
)
from .fvg import FairValueGap, FVGStatus, FVGType, detect_fvg
from .killzone import KillZoneContext, get_killzone_context
from .liquidity import LiqStatus, LiquidityMap, detect_liquidity
from .order_block import OBStatus, OBType, OrderBlock, detect_order_blocks
from .structures import MarketStructureAnalysis, analyse_structure
from .swings import detect_swings, swing_indices


@dataclass
class TFSnapshot:
    timeframe: str
    weight: float
    structure: MarketStructureAnalysis
    breaks: list
    order_blocks: list
    fvgs: list
    liquidity: LiquidityMap
    bias: Optional[BreakDirection] = None
    last_choch: Optional[StructureBreak] = None
    last_bos: Optional[StructureBreak] = None
    nearest_ob: Optional[OrderBlock] = None
    nearest_fvg: Optional[FairValueGap] = None


@dataclass
class ConfluenceScore:
    total: float
    breakdown: dict = field(default_factory=dict)
    direction: Optional[str] = None
    confidence: str = "low"


@dataclass
class MTFAnalysis:
    symbol: str
    anchor_tf: str
    execution_tf: str
    snapshots: dict = field(default_factory=dict)
    killzone: Optional[KillZoneContext] = None
    confluence: Optional[ConfluenceScore] = None
    aligned: bool = False
    primary_bias: Optional[str] = None


_DEFAULT_WEIGHTS = {
    "1W": 5.0,
    "3D": 4.5,
    "1D": 4.0,
    "12H": 3.5,
    "4H": 3.0,
    "2H": 2.5,
    "1H": 2.0,
    "30m": 1.5,
    "15m": 1.2,
    "5m": 1.0,
    "1m": 0.5,
}


def _tf_weight(label):
    return _DEFAULT_WEIGHTS.get(label, 1.0)


def analyse_timeframe(df, label, swing_method="fractal", swing_left=3, swing_right=3):
    sh_idx, sl_idx = swing_indices(
        df, method=swing_method, left_bars=swing_left, right_bars=swing_right
    )
    structure = analyse_structure(df, sh_idx, sl_idx)
    breaks = detect_bos_choch(df, sh_idx, sl_idx)
    obs = detect_order_blocks(df, sh_idx, sl_idx, update_status=True)
    fvgs = detect_fvg(df, update_status=True)
    liq = detect_liquidity(df, sh_idx, sl_idx, update_status=True)
    bias = current_bias(breaks)
    last_choch = next(
        (b for b in reversed(breaks) if b.break_type == BreakType.CHOCH), None
    )
    last_bos = next(
        (b for b in reversed(breaks) if b.break_type == BreakType.BOS), None
    )
    current_price = float(df["close"].iloc[-1])
    active_obs = [ob for ob in obs if ob.status in {OBStatus.PRISTINE, OBStatus.TESTED}]
    nearest_ob = (
        min(active_obs, key=lambda ob: abs(ob.mid - current_price))
        if active_obs
        else None
    )
    open_fvgs_list = [
        f for f in fvgs if f.status in {FVGStatus.OPEN, FVGStatus.PARTIAL}
    ]
    nearest_fvg = (
        min(open_fvgs_list, key=lambda f: abs(f.mid - current_price))
        if open_fvgs_list
        else None
    )
    return TFSnapshot(
        timeframe=label,
        weight=_tf_weight(label),
        structure=structure,
        breaks=breaks,
        order_blocks=obs,
        fvgs=fvgs,
        liquidity=liq,
        bias=bias,
        last_choch=last_choch,
        last_bos=last_bos,
        nearest_ob=nearest_ob,
        nearest_fvg=nearest_fvg,
    )


def _score_bias_alignment(snapshots):
    bull_weight = 0.0
    bear_weight = 0.0
    total_weight = 0.0
    for snap in snapshots.values():
        w = snap.weight
        total_weight += w
        if snap.bias == BreakDirection.BULLISH:
            bull_weight += w
        elif snap.bias == BreakDirection.BEARISH:
            bear_weight += w
    if total_weight == 0:
        return 0.0, None
    bull_pct = bull_weight / total_weight
    bear_pct = bear_weight / total_weight
    if bull_pct > bear_pct:
        return round(bull_pct * 50, 2), "long"
    elif bear_pct > bull_pct:
        return round(bear_pct * 50, 2), "short"
    return 0.0, None


def _score_structure(snapshots, direction):
    if direction is None:
        return 0.0
    from .structures import MarketStructure

    score = 0.0
    for snap in snapshots.values():
        if snap.weight < 2.0:
            continue
        ms = snap.structure.structure
        ts = snap.structure.trend_strength
        if direction == "long" and ms == MarketStructure.BULLISH:
            score += snap.weight * ts
        elif direction == "short" and ms == MarketStructure.BEARISH:
            score += snap.weight * ts
    return round(min(score * 2.0, 15.0), 2)


def _score_ob_confluence(snapshots, direction):
    if direction is None:
        return 0.0
    score = 0.0
    for snap in snapshots.values():
        ob = snap.nearest_ob
        if ob is None:
            continue
        correct = (direction == "long" and ob.ob_type == OBType.BULLISH) or (
            direction == "short" and ob.ob_type == OBType.BEARISH
        )
        if correct:
            base = 3.0 * snap.weight / _DEFAULT_WEIGHTS.get("1H", 2.0)
            if ob.is_institutional:
                base *= 1.5
            if ob.status == OBStatus.PRISTINE:
                base *= 1.2
            score += base
    return round(min(score, 15.0), 2)


def _score_fvg_confluence(snapshots, direction):
    if direction is None:
        return 0.0
    score = 0.0
    for snap in snapshots.values():
        fvg = snap.nearest_fvg
        if fvg is None:
            continue
        correct = (direction == "long" and fvg.fvg_type == FVGType.BULLISH) or (
            direction == "short" and fvg.fvg_type == FVGType.BEARISH
        )
        if correct and fvg.is_significant:
            score += 2.0 * snap.weight / _DEFAULT_WEIGHTS.get("1H", 2.0)
    return round(min(score, 10.0), 2)


def _score_liquidity(snapshots, direction):
    if direction is None:
        return 0.0
    score = 0.0
    for snap in snapshots.values():
        for sweep in snap.liquidity.recent_sweeps[:3]:
            if (
                direction == "long"
                and sweep.liq_type.value == "SSL"
                and sweep.is_stop_hunt
            ):
                score += 3.0
                break
            if (
                direction == "short"
                and sweep.liq_type.value == "BSL"
                and sweep.is_stop_hunt
            ):
                score += 3.0
                break
    return round(min(score, 10.0), 2)


def _score_killzone(kz):
    if kz is None or not kz.in_any_killzone:
        return 0.0
    raw = kz.combined_priority_score
    if kz.is_preferred_day:
        raw *= 1.1
    return round(min(raw, 10.0), 2)


def compute_confluence(snapshots, killzone):
    bias_score, direction = _score_bias_alignment(snapshots)
    structure_score = _score_structure(snapshots, direction)
    ob_score = _score_ob_confluence(snapshots, direction)
    fvg_score = _score_fvg_confluence(snapshots, direction)
    liq_score = _score_liquidity(snapshots, direction)
    kz_score = _score_killzone(killzone)
    total = round(
        min(
            bias_score + structure_score + ob_score + fvg_score + liq_score + kz_score,
            100.0,
        ),
        2,
    )
    confidence = "high" if total >= 70 else "medium" if total >= 45 else "low"
    return ConfluenceScore(
        total=total,
        breakdown={
            "bias_alignment": bias_score,
            "market_structure": structure_score,
            "order_block": ob_score,
            "fair_value_gap": fvg_score,
            "liquidity_sweep": liq_score,
            "kill_zone": kz_score,
        },
        direction=direction,
        confidence=confidence,
    )


def run_mtf_analysis(
    symbol,
    tf_data,
    anchor_tf,
    execution_tf,
    swing_method="fractal",
    swing_left=3,
    swing_right=3,
):
    snapshots = {}
    for label, df in tf_data.items():
        if df.empty:
            continue
        snapshots[label] = analyse_timeframe(
            df, label, swing_method, swing_left, swing_right
        )
    kz = None
    if execution_tf in tf_data and not tf_data[execution_tf].empty:
        last_ts = tf_data[execution_tf].index[-1]
        kz = get_killzone_context(last_ts, df=tf_data[execution_tf])
    confluence = compute_confluence(snapshots, kz)
    aligned = False
    if anchor_tf in snapshots and confluence.direction:
        anchor_snap = snapshots[anchor_tf]
        anchor_dir = (
            "long"
            if anchor_snap.bias == BreakDirection.BULLISH
            else "short" if anchor_snap.bias == BreakDirection.BEARISH else None
        )
        aligned = anchor_dir == confluence.direction
    return MTFAnalysis(
        symbol=symbol,
        anchor_tf=anchor_tf,
        execution_tf=execution_tf,
        snapshots=snapshots,
        killzone=kz,
        confluence=confluence,
        aligned=aligned,
        primary_bias=confluence.direction,
    )
