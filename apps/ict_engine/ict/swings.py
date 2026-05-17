from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd


@dataclass
class SwingPoint:
    index: int
    timestamp: pd.Timestamp
    price: float
    kind: str
    left_bars: int
    right_bars: int
    confirmed: bool
    strength: int = 0


def _fractal_swings(df, left, right, kind):
    col = "high" if kind == "high" else "low"
    series = df[col]
    n = len(series)
    swings = []
    for i in range(left, n - right):
        val = series.iloc[i]
        left_slice = series.iloc[i - left : i]
        right_slice = series.iloc[i + 1 : i + right + 1]
        if kind == "high":
            valid = (val > left_slice).all() and (val > right_slice).all()
        else:
            valid = (val < left_slice).all() and (val < right_slice).all()
        if valid:
            swings.append(
                SwingPoint(
                    index=i,
                    timestamp=df.index[i],
                    price=float(val),
                    kind=kind,
                    left_bars=left,
                    right_bars=right,
                    confirmed=True,
                    strength=left + right,
                )
            )
    return swings


def _atr_series(df, period=14):
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat(
        [high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1
    ).max(axis=1)
    return tr.rolling(period).mean()


def _zigzag_swings(df, atr_multiplier=0.5, atr_period=14):
    atr = _atr_series(df, atr_period)
    highs = []
    lows = []
    direction = 0
    extreme_idx = 0
    extreme_val = df["close"].iloc[0]
    for i in range(1, len(df)):
        high_i = float(df["high"].iloc[i])
        low_i = float(df["low"].iloc[i])
        threshold = float(atr.iloc[i]) * atr_multiplier
        if direction >= 0:
            if high_i > extreme_val:
                extreme_idx, extreme_val = i, high_i
            elif extreme_val - low_i > threshold:
                highs.append(
                    SwingPoint(
                        index=extreme_idx,
                        timestamp=df.index[extreme_idx],
                        price=extreme_val,
                        kind="high",
                        left_bars=0,
                        right_bars=i - extreme_idx,
                        confirmed=True,
                        strength=i - extreme_idx,
                    )
                )
                direction = -1
                extreme_idx, extreme_val = i, low_i
        else:
            if low_i < extreme_val:
                extreme_idx, extreme_val = i, low_i
            elif high_i - extreme_val > threshold:
                lows.append(
                    SwingPoint(
                        index=extreme_idx,
                        timestamp=df.index[extreme_idx],
                        price=extreme_val,
                        kind="low",
                        left_bars=0,
                        right_bars=i - extreme_idx,
                        confirmed=True,
                        strength=i - extreme_idx,
                    )
                )
                direction = 1
                extreme_idx, extreme_val = i, high_i
    return highs, lows


def detect_swings(
    df, method="fractal", left_bars=3, right_bars=3, atr_multiplier=0.5, min_strength=0
):
    if method == "fractal":
        raw_highs = _fractal_swings(df, left_bars, right_bars, "high")
        raw_lows = _fractal_swings(df, left_bars, right_bars, "low")
    else:
        raw_highs, raw_lows = _zigzag_swings(df, atr_multiplier)
    highs = [s for s in raw_highs if s.strength >= min_strength]
    lows = [s for s in raw_lows if s.strength >= min_strength]
    return highs, lows


def swing_indices(df, method="fractal", left_bars=3, right_bars=3, atr_multiplier=0.5):
    highs, lows = detect_swings(df, method, left_bars, right_bars, atr_multiplier)
    return [h.index for h in highs], [l.index for l in lows]
