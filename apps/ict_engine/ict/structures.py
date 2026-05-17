from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import numpy as np
import pandas as pd


class MarketStructure(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    RANGING = "ranging"


class SwingType(str, Enum):
    HH = "HH"
    HL = "HL"
    LH = "LH"
    LL = "LL"
    EH = "EH"
    EL = "EL"


@dataclass
class StructurePoint:
    index: int
    timestamp: pd.Timestamp
    price: float
    swing_type: SwingType
    is_swing_high: bool
    confirmed: bool = False
    strength: float = 0.0
    candles_from_prior: int = 0


@dataclass
class MarketStructureAnalysis:
    structure: MarketStructure
    points: list = field(default_factory=list)
    last_hh: Optional[StructurePoint] = None
    last_hl: Optional[StructurePoint] = None
    last_lh: Optional[StructurePoint] = None
    last_ll: Optional[StructurePoint] = None
    trend_strength: float = 0.0
    consecutive_count: int = 0
    structure_break: bool = False


def _atr(df, period=14):
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat(
        [high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1
    ).max(axis=1)
    return tr.rolling(period).mean()


def _classify_swing(price, is_high, prior_high, prior_low, tolerance_pct=0.0005):
    ref = prior_high if is_high else prior_low
    if ref is None:
        return SwingType.HH if is_high else SwingType.LL
    diff_pct = (price - ref.price) / ref.price
    if is_high:
        if diff_pct > tolerance_pct:
            return SwingType.HH
        elif diff_pct < -tolerance_pct:
            return SwingType.LH
        else:
            return SwingType.EH
    else:
        if diff_pct < -tolerance_pct:
            return SwingType.LL
        elif diff_pct > tolerance_pct:
            return SwingType.HL
        else:
            return SwingType.EL


def _determine_structure(points):
    if len(points) < 4:
        return MarketStructure.RANGING, 0.0, 0
    scores = []
    bullish_types = {SwingType.HH, SwingType.HL}
    bearish_types = {SwingType.LH, SwingType.LL}
    consecutive = 0
    last_direction = None
    for p in points[-10:]:
        if p.swing_type in bullish_types:
            scores.append(1.0)
            direction = True
        elif p.swing_type in bearish_types:
            scores.append(-1.0)
            direction = False
        else:
            scores.append(0.0)
            direction = None
        if direction is not None:
            if direction == last_direction:
                consecutive += 1
            else:
                consecutive = 1
            last_direction = direction
    if not scores:
        return MarketStructure.RANGING, 0.0, 0
    import numpy as np

    avg = float(np.mean(scores))
    strength = abs(avg)
    if avg > 0.3:
        structure = MarketStructure.BULLISH
    elif avg < -0.3:
        structure = MarketStructure.BEARISH
    else:
        structure = MarketStructure.RANGING
    return structure, round(strength, 4), consecutive


def analyse_structure(df, swing_highs, swing_lows, tolerance_pct=0.0005):
    import numpy as np

    atr = _atr(df)
    points = []
    last_high_sp = None
    last_low_sp = None
    events = [(i, True) for i in swing_highs] + [(i, False) for i in swing_lows]
    events.sort(key=lambda x: x[0])
    for idx, is_high in events:
        price = float(df["high"].iloc[idx] if is_high else df["low"].iloc[idx])
        swing_type = _classify_swing(
            price, is_high, last_high_sp, last_low_sp, tolerance_pct
        )
        atr_val = atr.iloc[idx] if not np.isnan(atr.iloc[idx]) else 1.0
        ref = last_high_sp if is_high else last_low_sp
        if ref is not None:
            strength = abs(price - ref.price) / atr_val
            candles = idx - ref.index
        else:
            strength = 0.0
            candles = 0
        sp = StructurePoint(
            index=idx,
            timestamp=df.index[idx],
            price=price,
            swing_type=swing_type,
            is_swing_high=is_high,
            confirmed=True,
            strength=round(strength, 4),
            candles_from_prior=candles,
        )
        points.append(sp)
        if is_high:
            last_high_sp = sp
        else:
            last_low_sp = sp
    structure, trend_strength, consecutive = _determine_structure(points)
    structure_break = False
    if points:
        last = points[-1]
        if structure == MarketStructure.BULLISH and last.swing_type in {
            SwingType.LH,
            SwingType.LL,
        }:
            structure_break = True
        elif structure == MarketStructure.BEARISH and last.swing_type in {
            SwingType.HH,
            SwingType.HL,
        }:
            structure_break = True
    return MarketStructureAnalysis(
        structure=structure,
        points=points,
        last_hh=next(
            (p for p in reversed(points) if p.swing_type == SwingType.HH), None
        ),
        last_hl=next(
            (p for p in reversed(points) if p.swing_type == SwingType.HL), None
        ),
        last_lh=next(
            (p for p in reversed(points) if p.swing_type == SwingType.LH), None
        ),
        last_ll=next(
            (p for p in reversed(points) if p.swing_type == SwingType.LL), None
        ),
        trend_strength=trend_strength,
        consecutive_count=consecutive,
        structure_break=structure_break,
    )
