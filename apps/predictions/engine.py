# apps/predictions/engine.py
# Main prediction engine - ICT/SMC + Global Cues combined

import logging
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# CANDLE FETCHER (Fyers — equity only)
# ─────────────────────────────────────────────
def _fetch_candles(strategy_user, symbol: str, timeframe: str, bars: int = 500) -> pd.DataFrame:
    """Fetch candles from Fyers and return as DataFrame. Equity symbols only."""
    try:
        from apps.brokers.models import BrokerAccount
        from fyers_apiv3 import fyersModel
        from apps.brokers.symbol_mapper import normalize_for_fyers
        import datetime as _dt

        account = BrokerAccount.objects.filter(
            user=strategy_user, broker="fyers",
            is_active=True, is_verified=True,
        ).first()

        if not account:
            return pd.DataFrame()

        fyers = fyersModel.FyersModel(
            client_id=account.app_id,
            token=account.access_token,
            log_path="", is_async=False,
        )

        fyers_sym = normalize_for_fyers(symbol)
        tf_map = {"1W": "D", "1D": "D", "4H": "120", "1H": "60",
                  "30m": "30", "15m": "15", "5m": "5", "1m": "1"}
        resolution = tf_map.get(timeframe, "D")

        end = _dt.date.today()
        days_map = {"1W": 730, "1D": 365, "4H": 180, "1H": 60,
                    "30m": 30, "15m": 15, "5m": 7, "1m": 3}
        days = days_map.get(timeframe, 120)
        start = end - _dt.timedelta(days=days)

        data = fyers.history(data={
            "symbol": fyers_sym,
            "resolution": resolution,
            "date_format": "1",
            "range_from": start.strftime("%Y-%m-%d"),
            "range_to": end.strftime("%Y-%m-%d"),
            "cont_flag": "1",
        })

        if data.get("s") != "ok" or not data.get("candles"):
            return pd.DataFrame()

        candles = data["candles"][-bars:]
        df = pd.DataFrame(candles, columns=["ts", "open", "high", "low", "close", "volume"])
        df.index = pd.to_datetime(df["ts"], unit="s", utc=True)
        df = df.drop(columns=["ts"])
        return df

    except Exception as e:
        logger.error("Candle fetch failed | symbol=%s | tf=%s | %s", symbol, timeframe, e)
        return pd.DataFrame()


# ─────────────────────────────────────────────
# CANDLE FETCHER (Delta Exchange — crypto only)
# ─────────────────────────────────────────────
def _fetch_candles_crypto(symbol: str, timeframe: str, bars: int = 500) -> pd.DataFrame:
    """Fetch crypto OHLCV from Delta Exchange using existing delta_service."""
    try:
        from apps.market.delta_service import fetch_delta_candles

        # engine timeframe → delta_service timeframe
        tf_map = {
            "1W": "D",    # Delta has no weekly; use daily
            "1D": "D",
            "4H": "4H",
            "1H": "1H",
            "30m": "30",
            "15m": "15",
            "5m":  "5",
            "1m":  "1",
        }
        delta_tf = tf_map.get(timeframe, "D")

        result = fetch_delta_candles(symbol, timeframe=delta_tf, limit=bars)

        if "error" in result or not result.get("candles"):
            logger.warning(
                "Delta candles empty | symbol=%s | tf=%s | reason=%s",
                symbol, timeframe, result.get("error", "no candles")
            )
            return pd.DataFrame()

        df = pd.DataFrame(result["candles"])   # columns: ts, open, high, low, close, volume
        df.index = pd.to_datetime(df["ts"], unit="s", utc=True)
        df = df.drop(columns=["ts"])
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna()
        return df

    except Exception as e:
        logger.error("Crypto candle fetch failed | symbol=%s | tf=%s | %s", symbol, timeframe, e)
        return pd.DataFrame()


# ─────────────────────────────────────────────
# KEY LEVELS EXTRACTOR
# ─────────────────────────────────────────────
def _extract_key_levels(snapshots: dict, current_price: float) -> list:
    """Extract important price levels from MTF analysis."""
    levels = []

    for tf, snap in snapshots.items():
        weight = snap.weight

        # Order Blocks
        for ob in snap.order_blocks[-5:]:
            from apps.ict_engine.ict.order_block import OBStatus
            if ob.status in {OBStatus.PRISTINE, OBStatus.TESTED}:
                levels.append({
                    "type": f"OB_{ob.ob_type.value.upper()}",
                    "timeframe": tf,
                    "high": round(ob.top, 2),
                    "low": round(ob.bottom, 2),
                    "mid": round(ob.mid, 2),
                    "weight": weight,
                    "distance_pct": round(abs(ob.mid - current_price) / current_price * 100, 2),
                })

        # FVGs
        for fvg in snap.fvgs[-5:]:
            from apps.ict_engine.ict.fvg import FVGStatus
            if fvg.status in {FVGStatus.OPEN, FVGStatus.PARTIAL}:
                levels.append({
                    "type": f"FVG_{fvg.fvg_type.value.upper()}",
                    "timeframe": tf,
                    "high": round(fvg.top, 2),
                    "low": round(fvg.bottom, 2),
                    "mid": round(fvg.mid, 2),
                    "weight": weight,
                    "distance_pct": round(abs(fvg.mid - current_price) / current_price * 100, 2),
                })

        # Liquidity Levels
        intact_levels = [
            l for l in (snap.liquidity.bsl_levels + snap.liquidity.ssl_levels)
            if l.status.value == "intact"
        ]
        for liq in intact_levels[:5]:
            levels.append({
                "type": f"LIQ_{liq.liq_type.value}",
                "timeframe": tf,
                "high": round(liq.price + 5, 2),
                "low": round(liq.price - 5, 2),
                "mid": round(liq.price, 2),
                "weight": weight,
                "distance_pct": round(abs(liq.price - current_price) / current_price * 100, 2),
            })

    levels.sort(key=lambda x: x["distance_pct"])
    return levels[:20]


# ─────────────────────────────────────────────
# TRADE PLAN BUILDER
# ─────────────────────────────────────────────
def _build_trade_plan(
    bias: str,
    current_price: float,
    key_levels: list,
    confluence_score: float,
    global_score: float,
) -> dict:
    """Build next day trade plan from analysis."""

    bullish_levels = [l for l in key_levels if "BULLISH" in l["type"] or "SSL" in l["type"]]
    bearish_levels = [l for l in key_levels if "BEARISH" in l["type"] or "BSL" in l["type"]]

    if bias == "long":
        support_levels = [l for l in bullish_levels if l["mid"] < current_price]
        entry_level = support_levels[0] if support_levels else None

        entry_high = entry_level["high"] if entry_level else round(current_price * 0.998, 2)
        entry_low  = entry_level["low"]  if entry_level else round(current_price * 0.995, 2)
        stop_loss  = round(entry_low * 0.997, 2)

        risk = entry_high - stop_loss
        target_1 = round(entry_high + risk * 1.5, 2)
        target_2 = round(entry_high + risk * 2.5, 2)
        target_3 = round(entry_high + risk * 4.0, 2)

    else:  # short / bearish
        resistance_levels = [l for l in bearish_levels if l["mid"] > current_price]
        entry_level = resistance_levels[0] if resistance_levels else None

        entry_high = entry_level["high"] if entry_level else round(current_price * 1.005, 2)
        entry_low  = entry_level["low"]  if entry_level else round(current_price * 1.002, 2)
        stop_loss  = round(entry_high * 1.003, 2)

        risk = stop_loss - entry_low
        target_1 = round(entry_low - risk * 1.5, 2)
        target_2 = round(entry_low - risk * 2.5, 2)
        target_3 = round(entry_low - risk * 4.0, 2)

    combined = (confluence_score * 0.6) + ((global_score + 100) / 2 * 0.4)
    confidence = "high" if combined >= 70 else "medium" if combined >= 45 else "low"

    return {
        "bias": bias,
        "entry_zone_high": entry_high,
        "entry_zone_low": entry_low,
        "stop_loss": stop_loss,
        "target_1": target_1,
        "target_2": target_2,
        "target_3": target_3,
        "risk_reward": round((target_2 - entry_high) / (entry_high - stop_loss), 2)
            if bias == "long" and entry_high != stop_loss else
            round((entry_low - target_2) / (stop_loss - entry_low), 2)
            if stop_loss != entry_low else 0,
        "confidence": confidence,
        "combined_score": round(combined, 1),
    }


# ─────────────────────────────────────────────
# SUMMARY GENERATOR
# ─────────────────────────────────────────────
def _generate_summary(
    symbol: str,
    bias: str,
    confluence: object,
    global_score: float,
    news_sentiment: float,
    trade_plan: dict,
    next_date: date,
) -> str:
    direction = "BULLISH" if bias == "long" else "BEARISH"
    conf_str = trade_plan["confidence"].upper()

    summary = f"""
{symbol} — Next Day Prediction ({next_date.strftime('%d %b %Y')})

BIAS: {direction} | CONFIDENCE: {conf_str} | SCORE: {trade_plan['combined_score']:.0f}/100

ICT/SMC Analysis:
- Confluence Score: {confluence.total:.0f}/100
- Bias Alignment: {confluence.breakdown.get('bias_alignment', 0):.0f}
- Market Structure: {confluence.breakdown.get('market_structure', 0):.0f}
- Order Block: {confluence.breakdown.get('order_block', 0):.0f}
- Fair Value Gap: {confluence.breakdown.get('fair_value_gap', 0):.0f}
- Liquidity Sweep: {confluence.breakdown.get('liquidity_sweep', 0):.0f}
- Kill Zone: {confluence.breakdown.get('kill_zone', 0):.0f}

Global Cues: {'+' if global_score > 0 else ''}{global_score:.0f}/100
News Sentiment: {'+' if news_sentiment > 0 else ''}{news_sentiment:.2f}

Trade Plan:
- Entry Zone: {trade_plan['entry_zone_low']} - {trade_plan['entry_zone_high']}
- Stop Loss: {trade_plan['stop_loss']}
- Target 1: {trade_plan['target_1']} (1.5R)
- Target 2: {trade_plan['target_2']} (2.5R)
- Target 3: {trade_plan['target_3']} (4.0R)
- Risk:Reward: 1:{trade_plan['risk_reward']}
""".strip()

    return summary


# ─────────────────────────────────────────────
# MAIN PREDICTION ENGINE
# ─────────────────────────────────────────────
def generate_prediction(
    symbol: str,
    user,
    news_api_key: Optional[str] = None,
    force_regenerate: bool = False,
) -> Optional[object]:
    """
    Generate next day prediction for a symbol.
    Combines ICT/SMC MTF analysis with global cues.
    Routes candle fetching: crypto → Delta Exchange, equity → Fyers.
    """
    from apps.predictions.models import DailyPrediction, GlobalCueSnapshot
    from apps.ict_engine.ict.mtf import run_mtf_analysis
    from apps.market.delta_service import is_crypto_symbol

    # Next trading day
    today = date.today()
    weekday = today.weekday()
    if weekday == 4:    # Friday
        next_day = today + timedelta(days=3)
    elif weekday == 5:  # Saturday
        next_day = today + timedelta(days=2)
    else:
        next_day = today + timedelta(days=1)

    # Check if already generated
    if not force_regenerate:
        existing = DailyPrediction.objects.filter(
            symbol=symbol, prediction_date=next_day
        ).first()
        if existing:
            logger.info("Prediction already exists for %s %s", symbol, next_day)
            return existing

    logger.info("Generating prediction | symbol=%s | date=%s", symbol, next_day)

    # ── Step 1: Fetch multi-timeframe candles ──────────────
    timeframes = ["1W", "1D", "4H", "1H", "30m", "15m"]
    bars_map   = {"1W": 100, "1D": 300, "4H": 200, "1H": 300, "30m": 300, "15m": 500}

    # ✅ Route: crypto → Delta Exchange, equity → Fyers
    _is_crypto = is_crypto_symbol(symbol)
    logger.info("Symbol routing | %s | crypto=%s", symbol, _is_crypto)

    tf_data = {}
    for tf in timeframes:
        if _is_crypto:
            df = _fetch_candles_crypto(symbol, tf, bars=bars_map[tf])
        else:
            df = _fetch_candles(user, symbol, tf, bars=bars_map[tf])

        if not df.empty:
            tf_data[tf] = df
            logger.info("Fetched %s | %d bars", tf, len(df))
        else:
            logger.warning("No data for %s %s", symbol, tf)

    if len(tf_data) < 2:
        logger.error("Insufficient timeframe data for %s", symbol)
        return None

    current_price = float(list(tf_data.values())[-1]["close"].iloc[-1])

    # ── Step 2: Run MTF ICT Analysis ──────────────────────
    try:
        mtf = run_mtf_analysis(
            symbol=symbol,
            tf_data=tf_data,
            anchor_tf="1D",
            execution_tf="1H",
        )
    except Exception as e:
        logger.error("MTF analysis failed | %s | %s", symbol, e)
        return None

    confluence = mtf.confluence
    bias = mtf.primary_bias or "neutral"

    # ── Step 3: Extract key levels ─────────────────────────
    key_levels = _extract_key_levels(mtf.snapshots, current_price)

    # ── Step 4: Fetch global cues ──────────────────────────
    from apps.predictions.global_cues import fetch_all_global_cues

    try:
        cues = fetch_all_global_cues(news_api_key)
        global_score   = cues["global_score"]
        news_sentiment = cues["news_sentiment"]
        news_list      = cues["news"]
        markets_data   = cues["markets"]
        fii_dii        = cues["fii_dii"]
    except Exception as e:
        logger.warning("Global cues fetch failed: %s", e)
        global_score   = 0.0
        news_sentiment = 0.0
        news_list      = []
        markets_data   = {}
        fii_dii        = {}

    # ── Step 5: Adjust bias with global cues ──────────────
    if abs(global_score) > 60:
        if global_score > 60 and bias == "short":
            logger.info("Global cues overriding ICT bearish bias to neutral")
            bias = "neutral"
        elif global_score < -60 and bias == "long":
            logger.info("Global cues overriding ICT bullish bias to neutral")
            bias = "neutral"

    # ── Step 6: Build trade plan ───────────────────────────
    if bias in ("long", "short"):
        trade_plan = _build_trade_plan(
            bias, current_price, key_levels,
            confluence.total, global_score
        )
    else:
        trade_plan = {"bias": "neutral", "confidence": "low", "combined_score": 0}

    final_score = trade_plan.get("combined_score", 0)
    confidence  = trade_plan.get("confidence", "low")

    # ── Step 7: Generate summary ───────────────────────────
    summary = _generate_summary(
        symbol, bias, confluence,
        global_score, news_sentiment,
        trade_plan, next_day,
    )

    # ── Step 8: Save to DB ─────────────────────────────────
    GlobalCueSnapshot.objects.update_or_create(
        date=today,
        defaults={
            "sp500_close":     markets_data.get("sp500", {}).get("close") if markets_data.get("sp500") else None,
            "sp500_chg_pct":   markets_data.get("sp500", {}).get("chg_pct") if markets_data.get("sp500") else None,
            "dow_close":       markets_data.get("dow", {}).get("close") if markets_data.get("dow") else None,
            "dow_chg_pct":     markets_data.get("dow", {}).get("chg_pct") if markets_data.get("dow") else None,
            "nasdaq_close":    markets_data.get("nasdaq", {}).get("close") if markets_data.get("nasdaq") else None,
            "nasdaq_chg_pct":  markets_data.get("nasdaq", {}).get("chg_pct") if markets_data.get("nasdaq") else None,
            "nikkei_close":    markets_data.get("nikkei", {}).get("close") if markets_data.get("nikkei") else None,
            "nikkei_chg_pct":  markets_data.get("nikkei", {}).get("chg_pct") if markets_data.get("nikkei") else None,
            "crude_oil":       markets_data.get("crude_oil", {}).get("close") if markets_data.get("crude_oil") else None,
            "crude_chg_pct":   markets_data.get("crude_oil", {}).get("chg_pct") if markets_data.get("crude_oil") else None,
            "gold":            markets_data.get("gold", {}).get("close") if markets_data.get("gold") else None,
            "gold_chg_pct":    markets_data.get("gold", {}).get("chg_pct") if markets_data.get("gold") else None,
            "vix_us":          markets_data.get("vix_us", {}).get("close") if markets_data.get("vix_us") else None,
            "vix_india":       markets_data.get("vix_india", {}).get("close") if markets_data.get("vix_india") else None,
            "dxy":             markets_data.get("dxy", {}).get("close") if markets_data.get("dxy") else None,
            "dxy_chg_pct":     markets_data.get("dxy", {}).get("chg_pct") if markets_data.get("dxy") else None,
            "fii_net":         fii_dii.get("fii_net"),
            "dii_net":         fii_dii.get("dii_net"),
            "global_score":    global_score,
            "raw_data":        cues if isinstance(cues, dict) else {},
        }
    )

    prediction, created = DailyPrediction.objects.update_or_create(
        symbol=symbol,
        prediction_date=next_day,
        defaults={
            "bias":             "bullish" if bias == "long" else "bearish" if bias == "short" else "neutral",
            "confidence":       confidence,
            "confluence_score": confluence.total,
            "entry_zone_high":  trade_plan.get("entry_zone_high"),
            "entry_zone_low":   trade_plan.get("entry_zone_low"),
            "stop_loss":        trade_plan.get("stop_loss"),
            "target_1":         trade_plan.get("target_1"),
            "target_2":         trade_plan.get("target_2"),
            "target_3":         trade_plan.get("target_3"),
            "global_score":     global_score,
            "global_cues":      {
                "markets": markets_data,
                "fii_dii": fii_dii,
            },
            "news_sentiment":   news_sentiment,
            "news_summary":     summary,
            "top_news":         news_list[:5],
            "mtf_analysis":     {
                "aligned":       mtf.aligned,
                "primary_bias":  mtf.primary_bias,
                "confluence":    {
                    "total":      confluence.total,
                    "breakdown":  confluence.breakdown,
                    "direction":  confluence.direction,
                    "confidence": confluence.confidence,
                },
                "timeframes_analyzed": list(mtf.snapshots.keys()),
            },
            "key_levels":       key_levels,
            "final_score":      final_score,
            "summary":          summary,
            "trade_plan":       trade_plan,
        }
    )

    action = "Created" if created else "Updated"
    logger.info(
        "%s prediction | %s | %s | bias=%s | score=%.0f",
        action, symbol, next_day, bias, final_score
    )

    return prediction