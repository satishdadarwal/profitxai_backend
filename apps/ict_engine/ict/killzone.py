from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import pandas as pd


class KZName(str, Enum):
    ASIAN_RANGE = "asian_range"
    LONDON_OPEN = "london_open"
    LONDON_CLOSE = "london_close"
    NEW_YORK_OPEN = "new_york_open"
    NEW_YORK_PM = "new_york_pm"
    LONDON_NY_OVERLAP = "london_ny_overlap"
    INDIA_OPENING = "india_opening"
    INDIA_MID = "india_mid"
    INDIA_CLOSING = "india_closing"


KZ_PRIORITY = {
    KZName.LONDON_NY_OVERLAP: 10.0,
    KZName.NEW_YORK_OPEN: 9.0,
    KZName.LONDON_OPEN: 8.0,
    KZName.LONDON_CLOSE: 6.0,
    KZName.NEW_YORK_PM: 5.0,
    KZName.INDIA_OPENING: 9.0,
    KZName.INDIA_CLOSING: 8.0,
    KZName.INDIA_MID: 6.0,
    KZName.ASIAN_RANGE: 3.0,
}

KZ_WINDOWS = {
    KZName.ASIAN_RANGE: (0, 4),
    KZName.LONDON_OPEN: (2, 5),
    KZName.LONDON_CLOSE: (10, 12),
    KZName.LONDON_NY_OVERLAP: (12, 13),
    KZName.NEW_YORK_OPEN: (12, 15),
    KZName.NEW_YORK_PM: (15, 16),
    KZName.INDIA_OPENING: (3, 5),
    KZName.INDIA_MID: (5, 7),
    KZName.INDIA_CLOSING: (8, 10),
}


@dataclass
class KillZone:
    name: KZName
    priority: float
    utc_start_hour: int
    utc_end_hour: int
    is_active: bool = False
    minutes_into_session: int = 0
    minutes_remaining: int = 0


@dataclass
class KillZoneContext:
    timestamp: pd.Timestamp
    active_zones: list = field(default_factory=list)
    highest_priority_zone: Optional[KillZone] = None
    combined_priority_score: float = 0.0
    in_any_killzone: bool = False
    is_preferred_day: bool = True
    asian_high: Optional[float] = None
    asian_low: Optional[float] = None
    asian_mid: Optional[float] = None
    asian_range_size: Optional[float] = None


def _to_utc(ts):
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def get_killzone_context(timestamp, df=None):
    utc_ts = _to_utc(timestamp)
    hour = utc_ts.hour
    minute = utc_ts.minute
    active = []
    for kz_name, (start_h, end_h) in KZ_WINDOWS.items():
        if start_h <= hour < end_h:
            elapsed = (hour - start_h) * 60 + minute
            session_len = (end_h - start_h) * 60
            active.append(
                KillZone(
                    name=kz_name,
                    priority=KZ_PRIORITY[kz_name],
                    utc_start_hour=start_h,
                    utc_end_hour=end_h,
                    is_active=True,
                    minutes_into_session=elapsed,
                    minutes_remaining=max(0, session_len - elapsed),
                )
            )
    active.sort(key=lambda z: z.priority, reverse=True)
    highest = active[0] if active else None
    combined = sum(z.priority for z in active)
    is_preferred = utc_ts.weekday() in {0, 1, 2, 3, 4}
    return KillZoneContext(
        timestamp=utc_ts,
        active_zones=active,
        highest_priority_zone=highest,
        combined_priority_score=round(combined, 2),
        in_any_killzone=len(active) > 0,
        is_preferred_day=is_preferred,
    )


def killzone_score(timestamp):
    ctx = get_killzone_context(timestamp)
    if not ctx.active_zones:
        return 0.0
    return min(ctx.combined_priority_score, 10.0)


def annotate_killzones(df):
    df = df.copy()
    utc_index = df.index if df.index.tzinfo else df.index.tz_localize("UTC")
    kz_active = []
    kz_name = []
    kz_priority = []
    kz_preferred = []
    for ts in utc_index:
        ctx = get_killzone_context(ts)
        kz_active.append(ctx.in_any_killzone)
        kz_name.append(
            ctx.highest_priority_zone.name.value if ctx.highest_priority_zone else ""
        )
        kz_priority.append(ctx.combined_priority_score)
        kz_preferred.append(ctx.is_preferred_day)
    df["kz_active"] = kz_active
    df["kz_name"] = kz_name
    df["kz_priority"] = kz_priority
    df["kz_preferred_day"] = kz_preferred
    return df
