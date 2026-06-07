# apps/options/signal_engine.py
# Options prediction signal — PCR + Max Pain + OI Walls + IV Rank

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def compute_max_pain(chain_data: list) -> Optional[float]:
    """
    Chain data: [{strike, CE:{oi}, PE:{oi}}, ...]
    Max pain = strike jahan total option writers ka loss minimum ho.
    """
    if not chain_data:
        return None

    strikes = [row["strike"] for row in chain_data]
    pain_map = {}

    for test_strike in strikes:
        total_pain = 0
        for row in chain_data:
            s = row["strike"]
            ce_oi = row.get("CE", {}).get("oi", 0)
            pe_oi = row.get("PE", {}).get("oi", 0)
            if test_strike > s:
                total_pain += (test_strike - s) * ce_oi
            if test_strike < s:
                total_pain += (s - test_strike) * pe_oi
        pain_map[test_strike] = total_pain

    return min(pain_map, key=pain_map.get)


def find_oi_walls(chain_data: list) -> dict:
    """Find call wall (max CE OI) and put wall (max PE OI)."""
    if not chain_data:
        return {"call_wall": None, "put_wall": None}

    max_ce = max(chain_data, key=lambda x: x.get("CE", {}).get("oi", 0), default=None)
    max_pe = max(chain_data, key=lambda x: x.get("PE", {}).get("oi", 0), default=None)

    return {
        "call_wall": max_ce["strike"] if max_ce else None,
        "put_wall":  max_pe["strike"] if max_pe else None,
    }


def compute_pcr(chain_data: list) -> dict:
    """Compute PCR by OI and by Volume."""
    total_ce_oi = sum(row.get("CE", {}).get("oi", 0) for row in chain_data)
    total_pe_oi = sum(row.get("PE", {}).get("oi", 0) for row in chain_data)
    total_ce_vol = sum(row.get("CE", {}).get("volume", 0) for row in chain_data)
    total_pe_vol = sum(row.get("PE", {}).get("volume", 0) for row in chain_data)

    return {
        "pcr_oi":     round(total_pe_oi / total_ce_oi, 3) if total_ce_oi else 0,
        "pcr_volume": round(total_pe_vol / total_ce_vol, 3) if total_ce_vol else 0,
    }


def compute_iv_rank(symbol_obj, current_iv: float) -> Optional[float]:
    """IV Rank = (current_iv - 52w_low) / (52w_high - 52w_low) * 100"""
    from apps.options.models import IVHistory
    from datetime import date, timedelta

    one_year_ago = date.today() - timedelta(days=365)
    history = IVHistory.objects.filter(
        symbol=symbol_obj, date__gte=one_year_ago
    ).values_list("atm_iv", flat=True)

    if len(history) < 10:
        return None

    low_52w = min(history)
    high_52w = max(history)

    if high_52w == low_52w:
        return 50.0

    return round((current_iv - low_52w) / (high_52w - low_52w) * 100, 1)


def derive_signal(snapshot) -> dict:
    """
    Multi-factor signal from OptionChainSnapshot.
    Returns: direction, confidence_pct, signal_score, probabilities, strategy, factors.
    """
    score = 0
    factors = {}
    spot = snapshot.spot

    pcr = snapshot.pcr_oi or 0
    if pcr < 0.7:
        score += 30
        factors["pcr"] = {"value": pcr, "signal": "strong_bullish", "pts": 30}
    elif pcr < 0.85:
        score += 15
        factors["pcr"] = {"value": pcr, "signal": "mild_bullish", "pts": 15}
    elif pcr > 1.5:
        score -= 30
        factors["pcr"] = {"value": pcr, "signal": "strong_bearish", "pts": -30}
    elif pcr > 1.2:
        score -= 15
        factors["pcr"] = {"value": pcr, "signal": "mild_bearish", "pts": -15}
    else:
        factors["pcr"] = {"value": pcr, "signal": "neutral", "pts": 0}

    max_pain = snapshot.max_pain or spot
    if spot > max_pain:
        score -= 10
        factors["max_pain"] = {"value": max_pain, "signal": "bearish_gravity", "pts": -10}
    else:
        score += 10
        factors["max_pain"] = {"value": max_pain, "signal": "bullish_gravity", "pts": 10}

    call_wall = snapshot.call_wall or 0
    put_wall = snapshot.put_wall or 0
    if call_wall and put_wall:
        if put_wall > spot > 0:
            score += 5
            factors["oi_walls"] = {
                "call_wall": call_wall,
                "put_wall": put_wall,
                "signal": "above_put_wall",
                "pts": 5,
            }
        if call_wall < spot:
            score -= 5
            factors["oi_walls"] = {
                "call_wall": call_wall,
                "put_wall": put_wall,
                "signal": "above_call_wall",
                "pts": -5,
            }
        else:
            factors["oi_walls"] = {
                "call_wall": call_wall,
                "put_wall": put_wall,
                "signal": "in_range",
                "pts": 0,
            }

    vix = snapshot.vix or 14
    if vix < 12:
        score += 10
        factors["vix"] = {"value": vix, "signal": "low_fear_bullish", "pts": 10}
    elif vix > 20:
        score -= 20
        factors["vix"] = {"value": vix, "signal": "high_fear_bearish", "pts": -20}
    elif vix > 16:
        score -= 10
        factors["vix"] = {"value": vix, "signal": "elevated_fear", "pts": -10}
    else:
        factors["vix"] = {"value": vix, "signal": "normal", "pts": 0}

    if score > 15:
        direction = "bullish"
    elif score < -15:
        direction = "bearish"
    else:
        direction = "neutral"

    raw_confidence = min(abs(score) / 55 * 100, 95)

    if direction == "bullish":
        up_prob = round(raw_confidence * 0.5, 1)
        flat_prob = round(30 + (100 - raw_confidence) * 0.2, 1)
        down_prob = round(100 - up_prob - flat_prob, 1)
    elif direction == "bearish":
        down_prob = round(raw_confidence * 0.5, 1)
        flat_prob = round(30 + (100 - raw_confidence) * 0.2, 1)
        up_prob = round(100 - down_prob - flat_prob, 1)
    else:
        up_prob = down_prob = 27.0
        flat_prob = 46.0

    iv_rank = getattr(snapshot, "_iv_rank", None)
    strategy, legs = _pick_strategy(direction, raw_confidence, iv_rank, spot, snapshot.atm_strike)

    return {
        "direction":         direction,
        "confidence_pct":    round(raw_confidence, 1),
        "signal_score":      score,
        "up_prob":           up_prob,
        "flat_prob":         flat_prob,
        "down_prob":         down_prob,
        "suggested_strategy": strategy,
        "strategy_legs":     legs,
        "signal_factors":    factors,
    }


def _pick_strategy(direction: str, confidence: float, iv_rank, spot: float, atm: int) -> tuple:
    """Select best options strategy based on direction + IV environment."""
    atm = atm or int(round(spot / 100) * 100)

    if iv_rank is None:
        iv_rank = 50

    if direction == "bullish":
        if iv_rank < 30:
            return "Long Call", [
                {"type": "CE", "strike": atm, "action": "BUY", "ratio": 1}
            ]
        else:
            return "Bull Put Spread", [
                {"type": "PE", "strike": atm - 100, "action": "SELL", "ratio": 1},
                {"type": "PE", "strike": atm - 200, "action": "BUY",  "ratio": 1},
            ]

    elif direction == "bearish":
        if iv_rank < 30:
            return "Long Put", [
                {"type": "PE", "strike": atm, "action": "BUY", "ratio": 1}
            ]
        else:
            return "Bear Call Spread", [
                {"type": "CE", "strike": atm + 100, "action": "SELL", "ratio": 1},
                {"type": "CE", "strike": atm + 200, "action": "BUY",  "ratio": 1},
            ]

    else:
        if iv_rank > 60:
            return "Short Strangle", [
                {"type": "CE", "strike": atm + 100, "action": "SELL", "ratio": 1},
                {"type": "PE", "strike": atm - 100, "action": "SELL", "ratio": 1},
            ]
        else:
            return "Iron Condor", [
                {"type": "CE", "strike": atm + 200, "action": "SELL", "ratio": 1},
                {"type": "CE", "strike": atm + 300, "action": "BUY",  "ratio": 1},
                {"type": "PE", "strike": atm - 200, "action": "SELL", "ratio": 1},
                {"type": "PE", "strike": atm - 300, "action": "BUY",  "ratio": 1},
            ]
