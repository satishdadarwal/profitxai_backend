from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import pandas as pd


class LiqStatus(str, Enum):
    INTACT = "intact"
    SWEPT = "swept"
    PARTIAL = "partial"


class LiqType(str, Enum):
    BSL = "BSL"
    SSL = "SSL"


@dataclass
class LiquidityLevel:
    liq_type: LiqType
    status: LiqStatus
    price: float
    swing_index: int
    formed_time: pd.Timestamp
    sweep_index: Optional[int] = None
    sweep_time: Optional[pd.Timestamp] = None
    is_stop_hunt: bool = False
    sweep_strength: float = 0.0


@dataclass
class LiquidityMap:
    bsl_levels: list = field(default_factory=list)
    ssl_levels: list = field(default_factory=list)
    recent_sweeps: list = field(default_factory=list)


def detect_liquidity(df, swing_highs, swing_lows, update_status=True):
    bsl = []
    for i in swing_highs:
        bsl.append(
            LiquidityLevel(
                liq_type=LiqType.BSL,
                status=LiqStatus.INTACT,
                price=float(df["high"].iloc[i]),
                swing_index=i,
                formed_time=df.index[i],
            )
        )
    ssl = []
    for i in swing_lows:
        ssl.append(
            LiquidityLevel(
                liq_type=LiqType.SSL,
                status=LiqStatus.INTACT,
                price=float(df["low"].iloc[i]),
                swing_index=i,
                formed_time=df.index[i],
            )
        )
    if update_status:
        for level in bsl:
            for bar_i in range(level.swing_index + 1, len(df)):
                if float(df["high"].iloc[bar_i]) > level.price:
                    level.status = LiqStatus.SWEPT
                    level.sweep_index = bar_i
                    level.sweep_time = df.index[bar_i]
                    level.is_stop_hunt = True
                    break
        for level in ssl:
            for bar_i in range(level.swing_index + 1, len(df)):
                if float(df["low"].iloc[bar_i]) < level.price:
                    level.status = LiqStatus.SWEPT
                    level.sweep_index = bar_i
                    level.sweep_time = df.index[bar_i]
                    level.is_stop_hunt = True
                    break
    recent_sweeps = sorted(
        [l for l in bsl + ssl if l.status == LiqStatus.SWEPT],
        key=lambda x: x.sweep_index or 0,
        reverse=True,
    )[:5]
    return LiquidityMap(bsl_levels=bsl, ssl_levels=ssl, recent_sweeps=recent_sweeps)
