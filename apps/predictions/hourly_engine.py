# apps/predictions/hourly_engine.py
# Hourly prediction engine — uses 1H + 30m + 15m timeframes

import logging
from datetime import datetime
from typing import Optional

import pytz

logger = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")


def generate_hourly_prediction(
    symbol: str,
    user,
    force_regenerate: bool = False,
) -> Optional[object]:
    """
    Generate hourly prediction for current/next hour.
    Uses 1H anchor, 30m + 15m execution timeframes.
    Reuses existing _fetch_candles / _fetch_candles_crypto from engine.py.
    """
    from apps.predictions.models import HourlyPrediction
    from apps.predictions.engine import (
        _fetch_candles,
        _fetch_candles_crypto,
        _extract_key_levels,
        _build_trade_plan,
    )
    from apps.ict_engine.ict.mtf import run_mtf_analysis
    from apps.market.delta_service import is_crypto_symbol

    now_ist = datetime.now(IST)
    current_hour = now_ist.replace(minute=0, second=0, microsecond=0)

    if not force_regenerate:
        existing = HourlyPrediction.objects.filter(
            symbol=symbol,
            prediction_hour=current_hour,
        ).first()
        if existing:
            logger.info("Hourly prediction exists | %s | %s", symbol, current_hour)
            return existing

    logger.info("Generating hourly prediction | %s | %s", symbol, current_hour)

    timeframes = ["4H", "1H", "30m", "15m"]
    bars_map = {"4H": 100, "1H": 200, "30m": 300, "15m": 500}

    _is_crypto = is_crypto_symbol(symbol)
    tf_data = {}
    for tf in timeframes:
        if _is_crypto:
            df = _fetch_candles_crypto(symbol, tf, bars=bars_map[tf])
        else:
            df = _fetch_candles(user, symbol, tf, bars=bars_map[tf])
        if not df.empty:
            tf_data[tf] = df

    if len(tf_data) < 2:
        logger.error("Insufficient data for hourly prediction | %s", symbol)
        return None

    current_price = float(list(tf_data.values())[-1]["close"].iloc[-1])

    try:
        mtf = run_mtf_analysis(
            symbol=symbol,
            tf_data=tf_data,
            anchor_tf="1H",
            execution_tf="15m",
        )
    except Exception as e:
        logger.error("Hourly MTF failed | %s | %s", symbol, e)
        return None

    confluence = getattr(mtf, "confluence", None)
    bias = getattr(mtf, "primary_bias", None) or "neutral"
    if confluence is None:
        logger.error("Hourly MTF has no confluence object | %s", symbol)
        return None

    key_levels = _extract_key_levels(mtf.snapshots, current_price)

    if bias in ("long", "short"):
        trade_plan = _build_trade_plan(bias, current_price, key_levels, confluence.total, 0)
    else:
        trade_plan = {"bias": "neutral", "confidence": "low", "combined_score": 0}

    combined = getattr(confluence, "total", 0)
    confidence_pct = min(round(combined, 1), 95)

    ict_breakdown = {
        "kill_zone":       confluence.breakdown.get("kill_zone", 0),
        "order_block":     confluence.breakdown.get("order_block", 0),
        "bias_alignment":  confluence.breakdown.get("bias_alignment", 0),
        "fair_value_gap":  confluence.breakdown.get("fair_value_gap", 0),
        "liquidity_sweep": confluence.breakdown.get("liquidity_sweep", 0),
        "market_structure": confluence.breakdown.get("market_structure", 0),
        "total":           confluence.total,
    }

    summary = (
        f"{symbol} Hourly ({current_hour.strftime('%d %b %H:%M IST')}) | "
        f"{'BULLISH' if bias=='long' else 'BEARISH' if bias=='short' else 'NEUTRAL'} | "
        f"Conf: {confidence_pct:.0f}% | Score: {confluence.total:.0f}/100"
    )

    prediction, created = HourlyPrediction.objects.update_or_create(
        symbol=symbol,
        prediction_hour=current_hour,
        defaults={
            "bias":            "bullish" if bias == "long" else "bearish" if bias == "short" else "neutral",
            "confidence_pct":  confidence_pct,
            "confluence_score": confluence.total,
            "entry_zone_high": trade_plan.get("entry_zone_high"),
            "entry_zone_low":  trade_plan.get("entry_zone_low"),
            "stop_loss":       trade_plan.get("stop_loss"),
            "target_1":        trade_plan.get("target_1"),
            "target_2":        trade_plan.get("target_2"),
            "key_levels":      key_levels[:10],
            "ict_breakdown":   ict_breakdown,
            "trade_plan":      trade_plan,
            "summary":         summary,
        }
    )

    logger.info(
        "%s hourly prediction | %s | %s | bias=%s | conf=%.0f%%",
        "Created" if created else "Updated",
        symbol, current_hour, bias, confidence_pct,
    )
    return prediction
