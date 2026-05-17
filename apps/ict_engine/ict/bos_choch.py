from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

import pandas as pd


class BreakType(str, Enum):
    BOS = "BOS"
    CHOCH = "CHoCH"
    UNKNOWN = "unknown"


class BreakDirection(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"


@dataclass
class StructureBreak:
    break_type: BreakType
    direction: BreakDirection
    broken_swing_index: int
    broken_swing_price: float
    broken_swing_time: pd.Timestamp
    break_index: int
    break_time: pd.Timestamp
    break_close: float
    prior_structure_bullish: bool
    candles_since_swing: int
    break_strength: float


def _atr(df, period=14):
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat(
        [high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1
    ).max(axis=1)
    return tr.rolling(period).mean()


def detect_bos_choch(
    df, swing_highs, swing_lows, require_close=True, min_candles_since_swing=1
):
    atr = _atr(df)
    breaks = []
    high_set = {i: float(df["high"].iloc[i]) for i in swing_highs}
    low_set = {i: float(df["low"].iloc[i]) for i in swing_lows}
    all_levels = sorted(
        [(i, True, p) for i, p in high_set.items()]
        + [(i, False, p) for i, p in low_set.items()],
        key=lambda x: x[0],
    )
    broken_indices = set()
    last_swing_high_idx = -1
    last_swing_low_idx = -1
    for level_idx, is_high_level, level_price in all_levels:
        if is_high_level:
            last_swing_high_idx = level_idx
        else:
            last_swing_low_idx = level_idx
        prior_bias_bullish = last_swing_high_idx > last_swing_low_idx
        for bar_i in range(level_idx + min_candles_since_swing, len(df)):
            close_i = float(df["close"].iloc[bar_i])
            high_i = float(df["high"].iloc[bar_i])
            low_i = float(df["low"].iloc[bar_i])
            atr_val = float(atr.iloc[bar_i]) if bar_i < len(atr) else 1.0
            if is_high_level:
                trigger_val = close_i if require_close else high_i
                if trigger_val > level_price:
                    if level_idx in broken_indices:
                        break
                    broken_indices.add(level_idx)
                    btype = BreakType.BOS if prior_bias_bullish else BreakType.CHOCH
                    breaks.append(
                        StructureBreak(
                            break_type=btype,
                            direction=BreakDirection.BULLISH,
                            broken_swing_index=level_idx,
                            broken_swing_price=level_price,
                            broken_swing_time=df.index[level_idx],
                            break_index=bar_i,
                            break_time=df.index[bar_i],
                            break_close=close_i,
                            prior_structure_bullish=prior_bias_bullish,
                            candles_since_swing=bar_i - level_idx,
                            break_strength=round(
                                (trigger_val - level_price) / atr_val, 4
                            ),
                        )
                    )
                    break
            else:
                trigger_val = close_i if require_close else low_i
                if trigger_val < level_price:
                    if level_idx in broken_indices:
                        break
                    broken_indices.add(level_idx)
                    btype = BreakType.BOS if not prior_bias_bullish else BreakType.CHOCH
                    breaks.append(
                        StructureBreak(
                            break_type=btype,
                            direction=BreakDirection.BEARISH,
                            broken_swing_index=level_idx,
                            broken_swing_price=level_price,
                            broken_swing_time=df.index[level_idx],
                            break_index=bar_i,
                            break_time=df.index[bar_i],
                            break_close=close_i,
                            prior_structure_bullish=prior_bias_bullish,
                            candles_since_swing=bar_i - level_idx,
                            break_strength=round(
                                (level_price - trigger_val) / atr_val, 4
                            ),
                        )
                    )
                    break
    return sorted(breaks, key=lambda x: x.break_index)


def latest_choch(breaks):
    chochs = [b for b in breaks if b.break_type == BreakType.CHOCH]
    return chochs[-1] if chochs else None


def latest_bos(breaks):
    bos_list = [b for b in breaks if b.break_type == BreakType.BOS]
    return bos_list[-1] if bos_list else None


def current_bias(breaks):
    if not breaks:
        return None
    return breaks[-1].direction
