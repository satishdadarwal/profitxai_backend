from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

import numpy as np
import pandas as pd


class FVGType(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"


class FVGStatus(str, Enum):
    OPEN = "open"
    PARTIAL = "partial"
    FILLED = "filled"
    INVERSE = "inverse"


@dataclass
class FairValueGap:
    fvg_type: FVGType
    status: FVGStatus
    candle_index: int
    formed_time: pd.Timestamp
    top: float
    bottom: float
    mid: float
    size: float
    size_atr_pct: float
    fill_pct: float = 0.0
    first_touch_index: Optional[int] = None
    fill_index: Optional[int] = None
    is_ifvg: bool = False
    is_significant: bool = False
    impulse_body_ratio: float = 0.0


def _atr(df, period=14):
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat(
        [high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1
    ).max(axis=1)
    return tr.rolling(period).mean()


def detect_fvg(
    df,
    atr_period=14,
    atr_significance_threshold=0.3,
    update_status=True,
    status_filter=None,
    significant_only=False,
):
    atr = _atr(df, atr_period)
    fvgs = []
    n = len(df)
    for i in range(1, n - 1):
        prev_high = float(df["high"].iloc[i - 1])
        prev_low = float(df["low"].iloc[i - 1])
        next_high = float(df["high"].iloc[i + 1])
        next_low = float(df["low"].iloc[i + 1])
        atr_val = float(atr.iloc[i]) if not np.isnan(atr.iloc[i]) else 1.0
        if prev_high < next_low:
            top = next_low
            bottom = prev_high
            size = top - bottom
            fvgs.append(
                FairValueGap(
                    fvg_type=FVGType.BULLISH,
                    status=FVGStatus.OPEN,
                    candle_index=i,
                    formed_time=df.index[i],
                    top=top,
                    bottom=bottom,
                    mid=(top + bottom) / 2,
                    size=size,
                    size_atr_pct=round(size / atr_val, 4),
                    is_significant=(size / atr_val) >= atr_significance_threshold,
                )
            )
        elif prev_low > next_high:
            top = prev_low
            bottom = next_high
            size = top - bottom
            fvgs.append(
                FairValueGap(
                    fvg_type=FVGType.BEARISH,
                    status=FVGStatus.OPEN,
                    candle_index=i,
                    formed_time=df.index[i],
                    top=top,
                    bottom=bottom,
                    mid=(top + bottom) / 2,
                    size=size,
                    size_atr_pct=round(size / atr_val, 4),
                    is_significant=(size / atr_val) >= atr_significance_threshold,
                )
            )
    if update_status:
        for fvg in fvgs:
            for i in range(fvg.candle_index + 2, len(df)):
                high_i = float(df["high"].iloc[i])
                low_i = float(df["low"].iloc[i])
                close_i = float(df["close"].iloc[i])
                if fvg.fvg_type == FVGType.BULLISH:
                    if low_i <= fvg.top:
                        if fvg.first_touch_index is None:
                            fvg.first_touch_index = i
                        pen = fvg.top - max(low_i, fvg.bottom)
                        fvg.fill_pct = min(1.0, round(pen / fvg.size, 4))
                        if close_i < fvg.bottom:
                            fvg.status = FVGStatus.INVERSE
                            fvg.is_ifvg = True
                            fvg.fill_index = i
                            break
                        elif low_i <= fvg.bottom:
                            fvg.status = FVGStatus.FILLED
                            fvg.fill_index = i
                            break
                        else:
                            fvg.status = FVGStatus.PARTIAL
                else:
                    if high_i >= fvg.bottom:
                        if fvg.first_touch_index is None:
                            fvg.first_touch_index = i
                        pen = min(high_i, fvg.top) - fvg.bottom
                        fvg.fill_pct = min(1.0, round(pen / fvg.size, 4))
                        if close_i > fvg.top:
                            fvg.status = FVGStatus.INVERSE
                            fvg.is_ifvg = True
                            fvg.fill_index = i
                            break
                        elif high_i >= fvg.top:
                            fvg.status = FVGStatus.FILLED
                            fvg.fill_index = i
                            break
                        else:
                            fvg.status = FVGStatus.PARTIAL
    if significant_only:
        fvgs = [f for f in fvgs if f.is_significant]
    if status_filter:
        fvgs = [f for f in fvgs if f.status in status_filter]
    return sorted(fvgs, key=lambda x: x.candle_index)


def open_fvgs(df, **kwargs):
    return detect_fvg(df, status_filter=[FVGStatus.OPEN, FVGStatus.PARTIAL], **kwargs)


def nearest_fvg(df, current_price, fvg_type, **kwargs):
    fvgs = open_fvgs(df, **kwargs)
    candidates = [f for f in fvgs if f.fvg_type == fvg_type]
    if not candidates:
        return None
    return min(candidates, key=lambda f: abs(f.mid - current_price))
