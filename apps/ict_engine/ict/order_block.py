from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import numpy as np
import pandas as pd


class OBType(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"


class OBStatus(str, Enum):
    PRISTINE = "pristine"
    TESTED = "tested"
    MITIGATED = "mitigated"
    BROKEN = "broken"


@dataclass
class OrderBlock:
    ob_type: OBType
    status: OBStatus
    formation_index: int
    formation_time: pd.Timestamp
    top: float
    bottom: float
    mid: float
    impulse_index: int
    impulse_pct: float
    distal: float
    proximal: float
    first_test_index: Optional[int] = None
    mitigation_index: Optional[int] = None
    touches: int = 0
    volume_at_formation: float = 0.0
    relative_volume: float = 0.0
    is_institutional: bool = False


def _atr(df, period=14):
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat(
        [high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1
    ).max(axis=1)
    return tr.rolling(period).mean()


def _rel_vol(df, idx, lookback=20):
    if "volume" not in df.columns:
        return 1.0
    start = max(0, idx - lookback)
    avg = df["volume"].iloc[start:idx].mean()
    if avg == 0:
        return 1.0
    return float(df["volume"].iloc[idx] / avg)


def detect_order_blocks(
    df,
    swing_highs,
    swing_lows,
    min_impulse_candles=2,
    min_impulse_pct=0.5,
    volume_threshold=1.5,
    update_status=True,
    status_filter=None,
):
    obs = []
    n = len(df)
    # Bullish OBs
    for sl_idx in swing_lows:
        for start in range(sl_idx, min(sl_idx + 10, n - min_impulse_candles)):
            if float(df["close"].iloc[start]) <= float(df["open"].iloc[start]):
                continue
            bull_count = sum(
                1
                for j in range(start, min(start + min_impulse_candles + 5, n))
                if float(df["close"].iloc[j]) > float(df["open"].iloc[j])
            )
            if bull_count < min_impulse_candles:
                continue
            ob_idx = None
            for k in range(start - 1, max(sl_idx - 5, -1), -1):
                if float(df["close"].iloc[k]) < float(df["open"].iloc[k]):
                    ob_idx = k
                    break
            if ob_idx is None:
                continue
            o = float(df["open"].iloc[ob_idx])
            c = float(df["close"].iloc[ob_idx])
            top = max(o, c)
            bottom = min(o, c)
            rvol = _rel_vol(df, ob_idx)
            obs.append(
                OrderBlock(
                    ob_type=OBType.BULLISH,
                    status=OBStatus.PRISTINE,
                    formation_index=ob_idx,
                    formation_time=df.index[ob_idx],
                    top=top,
                    bottom=bottom,
                    mid=(top + bottom) / 2,
                    impulse_index=start,
                    impulse_pct=0.0,
                    distal=bottom,
                    proximal=top,
                    relative_volume=rvol,
                    is_institutional=rvol >= volume_threshold,
                )
            )
            break
    # Bearish OBs
    for sh_idx in swing_highs:
        for start in range(sh_idx, min(sh_idx + 10, n - min_impulse_candles)):
            if float(df["close"].iloc[start]) >= float(df["open"].iloc[start]):
                continue
            bear_count = sum(
                1
                for j in range(start, min(start + min_impulse_candles + 5, n))
                if float(df["close"].iloc[j]) < float(df["open"].iloc[j])
            )
            if bear_count < min_impulse_candles:
                continue
            ob_idx = None
            for k in range(start - 1, max(sh_idx - 5, -1), -1):
                if float(df["close"].iloc[k]) > float(df["open"].iloc[k]):
                    ob_idx = k
                    break
            if ob_idx is None:
                continue
            o = float(df["open"].iloc[ob_idx])
            c = float(df["close"].iloc[ob_idx])
            top = max(o, c)
            bottom = min(o, c)
            rvol = _rel_vol(df, ob_idx)
            obs.append(
                OrderBlock(
                    ob_type=OBType.BEARISH,
                    status=OBStatus.PRISTINE,
                    formation_index=ob_idx,
                    formation_time=df.index[ob_idx],
                    top=top,
                    bottom=bottom,
                    mid=(top + bottom) / 2,
                    impulse_index=start,
                    impulse_pct=0.0,
                    distal=top,
                    proximal=bottom,
                    relative_volume=rvol,
                    is_institutional=rvol >= volume_threshold,
                )
            )
            break
    all_obs = sorted(obs, key=lambda x: x.formation_index)
    if update_status:
        for ob in all_obs:
            for i in range(ob.impulse_index + 1, len(df)):
                lo = float(df["low"].iloc[i])
                hi = float(df["high"].iloc[i])
                cl = float(df["close"].iloc[i])
                if ob.ob_type == OBType.BULLISH:
                    if lo <= ob.top and hi >= ob.bottom:
                        ob.touches += 1
                        if ob.first_test_index is None:
                            ob.first_test_index = i
                        if cl < ob.bottom:
                            ob.status = OBStatus.MITIGATED
                            ob.mitigation_index = i
                            break
                        ob.status = OBStatus.TESTED
                else:
                    if hi >= ob.bottom and lo <= ob.top:
                        ob.touches += 1
                        if ob.first_test_index is None:
                            ob.first_test_index = i
                        if cl > ob.top:
                            ob.status = OBStatus.MITIGATED
                            ob.mitigation_index = i
                            break
                        ob.status = OBStatus.TESTED
    if status_filter:
        all_obs = [ob for ob in all_obs if ob.status in status_filter]
    return all_obs


def active_order_blocks(df, swing_highs, swing_lows, **kwargs):
    return detect_order_blocks(
        df,
        swing_highs,
        swing_lows,
        status_filter=[OBStatus.PRISTINE, OBStatus.TESTED],
        **kwargs,
    )
