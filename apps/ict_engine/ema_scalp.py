# apps/ict_engine/ema_scalp.py
#
# EMA Scalp Strategy
# ─────────────────────────────────────────────────────────
# Setup : EMA Cross + Volume Confirm + RSI Filter
# RR    : 1:2 fixed (crypto/futures) | 1:1.5 (options — premium decay)
# TF    : 15M trend + 5M entry  (crypto/futures)
#         15M trend + 3M entry  (options — faster fill needed)
# Modes : crypto | futures | options
# ─────────────────────────────────────────────────────────

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# 🇮🇳 INDIAN OPTIONS CONFIG
# ─────────────────────────────────────────────
OPTION_LOT_SIZES = {
    "NIFTY":      65,
    "BANKNIFTY":  30,
    "FINNIFTY":   60,
    "MIDCPNIFTY": 120,
    "SENSEX":     10,
    "RELIANCE":   250,
    "TCS":        150,
    "INFY":       300,
    "HDFCBANK":   550,
    "ICICIBANK":  1375,
    "SBIN":       1500,
}

# ATM strike rounding step per index
STRIKE_STEP = {
    "NIFTY":      50,
    "BANKNIFTY":  100,
    "FINNIFTY":   50,
    "MIDCPNIFTY": 25,
    "SENSEX":     100,
}


def get_lot_size(symbol: str) -> int:
    base = symbol.upper().split("-")[0].split("_")[0]
    for key, size in OPTION_LOT_SIZES.items():
        if base.startswith(key):
            return size
    return 50


def get_strike_step(symbol: str) -> int:
    base = symbol.upper().split("-")[0].split("_")[0]
    for key, step in STRIKE_STEP.items():
        if base.startswith(key):
            return step
    return 50


# ─────────────────────────────────────────────
# 📊 SIGNAL DATACLASS
# ─────────────────────────────────────────────
class EMADirection(str, Enum):
    LONG  = "long"
    SHORT = "short"
    NONE  = "none"


@dataclass
class EMAScalpSignal:
    direction:    EMADirection
    symbol:       str
    entry_price:  float
    stop_loss:    float
    take_profit:  float        # TP1 — 1:2 RR
    take_profit2: float        # TP2 — 1:3 RR

    # EMA context
    fast_ema:  float
    slow_ema:  float
    trend_ema: float

    # Indicators
    rsi:          float
    volume_ratio: float
    atr:          float

    # Risk
    risk_points:   float
    reward_points: float
    rr_ratio:      float
    risk_amount:   float
    risk_pct:      float
    quantity:      float
    lot_size:      int

    # Options-specific
    option_type: str   = ""    # "CE" | "PE" | ""
    strike:      float = 0.0
    expiry:      str   = ""
    premium:     float = 0.0

    # Meta
    asset_type:       str   = "crypto"
    confluence_score: float = 0.0
    tags:  list = field(default_factory=list)
    notes: str  = ""

    def to_dict(self) -> dict:
        return {
            "direction":    self.direction.value,
            "symbol":       self.symbol,
            "entry_price":  round(self.entry_price,  4),
            "stop_loss":    round(self.stop_loss,     4),
            "take_profit":  round(self.take_profit,   4),
            "take_profit2": round(self.take_profit2,  4),
            "fast_ema":     round(self.fast_ema,      4),
            "slow_ema":     round(self.slow_ema,      4),
            "trend_ema":    round(self.trend_ema,     4),
            "rsi":          round(self.rsi,           2),
            "volume_ratio": round(self.volume_ratio,  2),
            "atr":          round(self.atr,           4),
            "risk_points":  round(self.risk_points,   4),
            "reward_points": round(self.reward_points, 4),
            "rr_ratio":     round(self.rr_ratio,      2),
            "risk_amount":  round(self.risk_amount,   0),
            "risk_pct":     round(self.risk_pct,      2),
            "quantity":     round(self.quantity,      4),
            "lot_size":     self.lot_size,
            "option_type":  self.option_type,
            "strike":       self.strike,
            "expiry":       self.expiry,
            "premium":      round(self.premium,       2),
            "asset_type":   self.asset_type,
            "confluence":   round(self.confluence_score, 1),
            "tags":         self.tags,
            "notes":        self.notes,
        }


# ─────────────────────────────────────────────
# 📐 INDICATOR HELPERS
# ─────────────────────────────────────────────
def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _rsi(series: pd.Series, period: int = 14) -> float:
    delta    = series.diff()
    gain     = delta.clip(lower=0)
    loss     = -delta.clip(upper=0)
    avg_gain = gain.ewm(span=period, adjust=False).mean()
    avg_loss = loss.ewm(span=period, adjust=False).mean()
    rs       = avg_gain / avg_loss.replace(0, np.nan)
    rsi_s    = 100 - (100 / (1 + rs))
    return float(rsi_s.iloc[-1]) if not rsi_s.empty else 50.0


def _atr(df: pd.DataFrame, period: int = 14) -> float:
    high  = df["high"]
    low   = df["low"]
    close = df["close"]
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)
    return float(tr.ewm(span=period, adjust=False).mean().iloc[-1])


def _volume_ratio(df: pd.DataFrame, period: int = 20) -> float:
    if len(df) < period + 1:
        return 1.0
    avg_vol = float(df["volume"].iloc[-period - 1:-1].mean())
    cur_vol = float(df["volume"].iloc[-1])
    return round(cur_vol / avg_vol, 2) if avg_vol > 0 else 1.0


def _vwap(df: pd.DataFrame) -> float:
    typical = (df["high"] + df["low"] + df["close"]) / 3
    vwap    = (typical * df["volume"]).cumsum() / df["volume"].cumsum()
    return float(vwap.iloc[-1])


# ─────────────────────────────────────────────
# 🎯 MAIN STRATEGY CLASS
# ─────────────────────────────────────────────
class EMAScalpStrategy:
    """
    EMA Scalp — Crypto, Futures & Indian Options.

    Signal flow:
      1. EMA cross     : fast(9) x slow(21) on 5M in last `cross_lookback` bars
      2. Trend filter  : price vs EMA 50 on 15M  (optional, default ON)
      3. RSI filter    : not overbought/oversold
      4. Volume filter : current vs avg            (optional)
      5. VWAP confirm  : options only              (optional)
      6. SL / TP       : ATR-based for crypto/futures
                         Premium % for options
    """

    def __init__(
        self,
        account_balance:    float = 100_000,
        risk_per_trade_pct: float = 1.0,
        fast_ema_period:    int   = 9,
        slow_ema_period:    int   = 21,
        trend_ema_period:   int   = 50,
        rsi_period:         int   = 14,
        rsi_overbought:     float = 65.0,
        rsi_oversold:       float = 35.0,
        atr_period:         int   = 14,
        atr_sl_multiplier:  float = 1.5,
        rr_ratio:           float = 2.0,
        min_volume_ratio:   float = 1.0,
        cross_lookback:     int   = 3,
        use_trend_filter:   bool  = True,
        use_vwap_filter:    bool  = True,
        asset_type:         str   = "crypto",
    ):
        self.account_balance    = account_balance
        self.risk_per_trade_pct = risk_per_trade_pct
        self.fast_period        = fast_ema_period
        self.slow_period        = slow_ema_period
        self.trend_period       = trend_ema_period
        self.rsi_period         = rsi_period
        self.rsi_ob             = rsi_overbought
        self.rsi_os             = rsi_oversold
        self.atr_period         = atr_period
        self.atr_mult           = atr_sl_multiplier
        self.rr                 = min(rr_ratio, 1.5) if asset_type == "options" else rr_ratio
        self.min_vol_ratio      = min_volume_ratio
        self.cross_lookback     = cross_lookback
        self.use_trend_filter   = use_trend_filter
        self.use_vwap_filter    = use_vwap_filter
        self.asset_type         = asset_type

    # ── EMA Cross — last N bars ───────────────────────────────
    def _detect_cross(
        self, df: pd.DataFrame, lookback: Optional[int] = None
    ) -> Optional[str]:
        lb = lookback or self.cross_lookback
        if len(df) < self.slow_period + lb + 2:
            return None
        fast = _ema(df["close"], self.fast_period)
        slow = _ema(df["close"], self.slow_period)
        diff = fast - slow
        for i in range(len(diff) - lb, len(diff)):
            prev = float(diff.iloc[i - 1])
            curr = float(diff.iloc[i])
            if prev < 0 and curr > 0:
                return "bullish"
            elif prev > 0 and curr < 0:
                return "bearish"
        return None

    # ── Trend filter ──────────────────────────────────────────
    def _get_trend(self, df_15m: pd.DataFrame) -> str:
        if len(df_15m) < self.trend_period:
            return "neutral"
        ema_val = float(_ema(df_15m["close"], self.trend_period).iloc[-1])
        price   = float(df_15m["close"].iloc[-1])
        buf     = ema_val * 0.001
        if price > ema_val + buf:
            return "bullish"
        elif price < ema_val - buf:
            return "bearish"
        return "neutral"

    # ── VWAP filter ───────────────────────────────────────────
    def _vwap_aligned(self, df: pd.DataFrame, direction: str) -> bool:
        try:
            vwap_val = _vwap(df)
            price    = float(df["close"].iloc[-1])
            return price > vwap_val if direction == "bullish" else price < vwap_val
        except Exception:
            return True

    # ── Position sizing ───────────────────────────────────────
    def _size_position(
        self, entry: float, stop: float, symbol: str = ""
    ) -> tuple[float, float, int]:
        risk_amount   = self.account_balance * (self.risk_per_trade_pct / 100.0)
        risk_per_unit = abs(entry - stop)
        if risk_per_unit == 0:
            return 0.0, 0.0, 1

        lot_size = get_lot_size(symbol) if self.asset_type == "options" else 1

        if self.asset_type == "options":
            risk_per_lot = entry * lot_size      # max loss = full premium per lot
            if risk_per_lot <= 0:
                return 0.0, 0.0, lot_size
            lots        = max(1, int(risk_amount / risk_per_lot))
            actual_risk = lots * risk_per_lot
            return float(lots), round(actual_risk, 2), lot_size
        else:
            qty = risk_amount / risk_per_unit
            return round(qty, 4), round(risk_amount, 2), 1

    # ── Main analyze ──────────────────────────────────────────
    def analyze(
        self,
        symbol:        str,
        df_15m:        pd.DataFrame,
        df_5m:         pd.DataFrame,
        option_symbol: str   = "",
        strike:        float = 0.0,
        expiry:        str   = "",
        premium:       float = 0.0,
    ) -> Optional[EMAScalpSignal]:

        min_bars = max(self.slow_period, self.trend_period) + self.cross_lookback + 2
        if len(df_5m) < min_bars or len(df_15m) < self.trend_period:
            logger.debug("[%s] EMA Scalp: Insufficient data", symbol)
            return None

        # ── Step 1: EMA cross (check first — fastest reject) ──
        cross = self._detect_cross(df_5m)
        if cross is None:
            logger.debug("[%s] EMA Scalp: No cross in last %d bars",
                         symbol, self.cross_lookback)
            return None

        # ── Step 2: Trend filter ──────────────────────────────
        trend = self._get_trend(df_15m)
        logger.info("[%s] EMA Scalp | cross=%s trend=%s asset=%s",
                    symbol, cross, trend, self.asset_type)

        if self.use_trend_filter:
            if trend == "neutral":
                logger.debug("[%s] Trend neutral, skip", symbol)
                return None
            if cross != trend:
                logger.debug("[%s] Cross/trend mismatch (%s vs %s), skip",
                             symbol, cross, trend)
                return None

        # ── Step 3: RSI ───────────────────────────────────────
        rsi_val = _rsi(df_5m["close"], self.rsi_period)
        if cross == "bullish" and rsi_val > self.rsi_ob:
            logger.debug("[%s] RSI overbought (%.1f)", symbol, rsi_val)
            return None
        if cross == "bearish" and rsi_val < self.rsi_os:
            logger.debug("[%s] RSI oversold (%.1f)", symbol, rsi_val)
            return None

        # ── Step 4: Volume ────────────────────────────────────
        vol_ratio = _volume_ratio(df_5m)
        if vol_ratio < self.min_vol_ratio:
            logger.debug("[%s] Low volume (%.2fx)", symbol, vol_ratio)
            return None

        # ── Step 5: VWAP (options only) ───────────────────────
        if self.asset_type == "options" and self.use_vwap_filter:
            if not self._vwap_aligned(df_5m, cross):
                logger.debug("[%s] VWAP misaligned, skip", symbol)
                return None

        # ── Step 6: SL / TP ───────────────────────────────────
        atr_val       = _atr(df_5m, self.atr_period)
        current_price = float(df_5m["close"].iloc[-1])

        if self.asset_type == "options":
            entry_price  = premium if premium > 0 else current_price * 0.01
            stop_loss    = round(entry_price * 0.60, 2)   # lose 40% of premium → exit
            take_profit  = round(entry_price * (1 + self.rr * 0.40), 2)
            take_profit2 = round(entry_price * (1 + self.rr * 0.70), 2)
        else:
            entry_price = current_price
            if cross == "bullish":
                stop_loss    = entry_price - atr_val * self.atr_mult
                take_profit  = entry_price + abs(entry_price - stop_loss) * self.rr
                take_profit2 = entry_price + abs(entry_price - stop_loss) * (self.rr + 1)
            else:
                stop_loss    = entry_price + atr_val * self.atr_mult
                take_profit  = entry_price - abs(entry_price - stop_loss) * self.rr
                take_profit2 = entry_price - abs(entry_price - stop_loss) * (self.rr + 1)

        risk_points   = abs(entry_price - stop_loss)
        reward_points = abs(take_profit - entry_price)

        if risk_points < entry_price * 0.0005:
            logger.debug("[%s] Risk too tight (%.4f)", symbol, risk_points)
            return None

        # ── Step 7: Position size ─────────────────────────────
        tgt_sym = option_symbol or symbol
        qty, risk_amount, lot_size = self._size_position(entry_price, stop_loss, tgt_sym)
        if qty == 0:
            return None

        # ── Step 8: EMA values ────────────────────────────────
        fast_ema_val  = float(_ema(df_5m["close"],  self.fast_period).iloc[-1])
        slow_ema_val  = float(_ema(df_5m["close"],  self.slow_period).iloc[-1])
        trend_ema_val = float(_ema(df_15m["close"], self.trend_period).iloc[-1])

        # ── Step 9: Confluence score ──────────────────────────
        score = 50.0
        if cross == "bullish" and 45 < rsi_val < 65:
            score += 15
        elif cross == "bearish" and 35 < rsi_val < 55:
            score += 15
        if vol_ratio >= 1.5:
            score += 20
        elif vol_ratio >= 1.2:
            score += 10
        ema_gap = abs(fast_ema_val - slow_ema_val) / slow_ema_val * 100
        if ema_gap > 0.1:
            score += 10
        dist = abs(current_price - trend_ema_val) / trend_ema_val * 100
        if dist < 1.0:
            score += 5
        if cross == trend:
            score += 5
        if self.asset_type == "options" and self._vwap_aligned(df_5m, cross):
            score += 10
        score = min(round(score, 1), 100.0)

        # ── Tags & notes ──────────────────────────────────────
        option_type = ("CE" if cross == "bullish" else "PE") \
                      if self.asset_type == "options" else ""
        tags = [cross.upper(), f"EMA{self.fast_period}x{self.slow_period}",
                f"RSI_{int(rsi_val)}", self.asset_type.upper()]
        if vol_ratio >= 1.2:
            tags.append("HIGH_VOL")
        if cross == trend:
            tags.append("TREND_ALIGNED")
        if option_type:
            tags.append(option_type)

        notes = (f"EMA {self.fast_period}x{self.slow_period} {cross} | "
                 f"RSI={rsi_val:.1f} | Vol={vol_ratio:.1f}x | {self.asset_type}")
        if option_type:
            notes += f" | {option_type} strike={strike}"

        signal = EMAScalpSignal(
            direction     = EMADirection.LONG if cross == "bullish" else EMADirection.SHORT,
            symbol        = option_symbol or symbol,
            entry_price   = entry_price,
            stop_loss     = stop_loss,
            take_profit   = take_profit,
            take_profit2  = take_profit2,
            fast_ema      = fast_ema_val,
            slow_ema      = slow_ema_val,
            trend_ema     = trend_ema_val,
            rsi           = rsi_val,
            volume_ratio  = vol_ratio,
            atr           = atr_val,
            risk_points   = risk_points,
            reward_points = reward_points,
            rr_ratio      = self.rr,
            risk_amount   = risk_amount,
            risk_pct      = self.risk_per_trade_pct,
            quantity      = qty,
            lot_size      = lot_size,
            option_type   = option_type,
            strike        = strike,
            expiry        = expiry,
            premium       = premium,
            asset_type    = self.asset_type,
            confluence_score = score,
            tags          = tags,
            notes         = notes,
        )

        logger.info(
            "[%s] EMA SCALP SIGNAL | %s %s | Entry=%.4f SL=%.4f TP1=%.4f TP2=%.4f"
            " | RR=1:%.1f | RSI=%.1f | Vol=%.2f | Score=%.1f",
            symbol, signal.direction.value.upper(), self.asset_type,
            entry_price, stop_loss, take_profit, take_profit2,
            self.rr, rsi_val, vol_ratio, score,
        )
        return signal


# ─────────────────────────────────────────────
# 🇮🇳 OPTIONS CONTRACT RESOLVER
# ─────────────────────────────────────────────
def _resolve_option_contract(
    strategy, underlying: str, df_15m: pd.DataFrame, df_5m: pd.DataFrame
) -> tuple[str, float, str, float]:
    """
    Auto-select ATM option contract.
    Returns: (fyers_symbol, strike, expiry, premium)
    """
    import datetime
    from django.utils import timezone

    spot_price = float(df_15m["close"].iloc[-1])

    # Determine direction
    fast = _ema(df_5m["close"], 9)
    slow = _ema(df_5m["close"], 21)
    diff = fast - slow
    direction = "bullish"
    for i in range(len(diff) - 3, len(diff)):
        prev, curr = float(diff.iloc[i - 1]), float(diff.iloc[i])
        if prev < 0 < curr:
            direction = "bullish"
            break
        elif prev > 0 > curr:
            direction = "bearish"
            break

    option_type = "CE" if direction == "bullish" else "PE"
    base_sym    = underlying.upper().replace("NSE:", "").replace("-INDEX", "")
    step        = get_strike_step(base_sym)
    atm_strike  = round(spot_price / step) * step

    # Expiry — allow override via parameters
    expiry = strategy.parameters.get("expiry", "")
    if not expiry:
        today      = timezone.now().date()
        days_ahead = (3 - today.weekday()) % 7 or 7   # next Thursday
        expiry_dt  = today + datetime.timedelta(days=days_ahead)
        expiry     = expiry_dt.strftime("%d%b%y").upper()   # e.g. "01MAY25"

    fyers_sym = f"NSE:{base_sym}{expiry}{int(atm_strike)}{option_type}"

    # Try live LTP from Fyers
    premium = 0.0
    try:
        from fyers_apiv3 import fyersModel
        from apps.brokers.models import BrokerAccount

        account = BrokerAccount.objects.filter(
            user=strategy.user, broker="fyers",
            is_active=True, is_verified=True,
        ).first()

        if account:
            fyers = fyersModel.FyersModel(
                client_id=account.app_id, token=account.access_token,
                log_path="", is_async=False,
            )
            quote = fyers.quotes(data={"symbols": fyers_sym})
            if quote.get("s") == "ok":
                premium = float(quote["d"][0]["v"].get("lp", 0))
    except Exception as e:
        logger.warning("Option LTP fetch failed | %s | %s", fyers_sym, e)

    if premium <= 0:
        premium = round(spot_price * 0.01, 2)   # 1% of spot fallback

    logger.info("Option resolved | %s | strike=%s | expiry=%s | ltp=%.2f",
                fyers_sym, atm_strike, expiry, premium)
    return fyers_sym, float(atm_strike), expiry, premium


# ─────────────────────────────────────────────
# 🔄 LIVE CYCLE
# ─────────────────────────────────────────────
def execute_ema_scalp_cycle(strategy, symbol: str) -> dict:
    import pandas as pd
    from apps.common.candle_service import fetch_candles_for_strategy

    asset_type = strategy.parameters.get("asset_type", "crypto")
    htf_tf     = strategy.parameters.get("htf", "15")
    ltf_tf     = strategy.parameters.get("ltf", "3" if asset_type == "options" else "5")

    try:
        htf_raw = fetch_candles_for_strategy(strategy, symbol, htf_tf) or []
        ltf_raw = fetch_candles_for_strategy(strategy, symbol, ltf_tf) or []
    except Exception as e:
        logger.error("EMA Scalp candle fetch | symbol=%s | err=%s", symbol, e)
        return _null_ema_signal(symbol)

    if not htf_raw or not ltf_raw:
        logger.warning("EMA Scalp: No candles | %s htf=%d ltf=%d",
                       symbol, len(htf_raw), len(ltf_raw))
        return _null_ema_signal(symbol)

    def _to_df(candles):
        rows = []
        for c in candles:
            if hasattr(c, "open"):
                rows.append({"ts": c.timestamp, "open": float(c.open),
                             "high": float(c.high), "low": float(c.low),
                             "close": float(c.close), "volume": float(c.volume)})
            else:
                rows.append({"ts": c.get("ts", 0), "open": float(c.get("open", 0)),
                             "high": float(c.get("high", 0)), "low": float(c.get("low", 0)),
                             "close": float(c.get("close", 0)), "volume": float(c.get("volume", 0))})
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        df.index = pd.to_datetime(df["ts"], unit="s", utc=True)
        return df.drop(columns=["ts"])

    df_15m = _to_df(htf_raw)
    df_5m  = _to_df(ltf_raw)

    if df_15m.empty or df_5m.empty:
        return _null_ema_signal(symbol)

    # Options: resolve contract
    option_symbol = strike = expiry = premium = ""
    strike = premium = 0.0
    if asset_type == "options":
        try:
            option_symbol, strike, expiry, premium = _resolve_option_contract(
                strategy, symbol, df_15m, df_5m)
        except Exception as e:
            logger.error("Option resolve failed | %s | %s", symbol, e)
            return _null_ema_signal(symbol)

    # Build strategy instance
    from apps.risk.manager import get_user_capital
    ema = EMAScalpStrategy(
        account_balance    = get_user_capital(strategy.user, strategy.mode),
        risk_per_trade_pct = float(strategy.parameters.get("risk_pct", 1.0)),
        fast_ema_period    = int(strategy.parameters.get("fast_ema", 9)),
        slow_ema_period    = int(strategy.parameters.get("slow_ema", 21)),
        trend_ema_period   = int(strategy.parameters.get("trend_ema", 50)),
        rsi_period         = int(strategy.parameters.get("rsi_period", 14)),
        rsi_overbought     = float(strategy.parameters.get("rsi_ob", 65.0)),
        rsi_oversold       = float(strategy.parameters.get("rsi_os", 35.0)),
        atr_period         = int(strategy.parameters.get("atr_period", 14)),
        atr_sl_multiplier  = float(strategy.parameters.get("atr_mult", 1.5)),
        rr_ratio           = float(strategy.parameters.get("rr", 2.0)),
        min_volume_ratio   = float(strategy.parameters.get("min_vol", 1.0)),
        cross_lookback     = int(strategy.parameters.get("cross_lookback", 3)),
        use_trend_filter   = bool(strategy.parameters.get("trend_filter", True)),
        use_vwap_filter    = bool(strategy.parameters.get("vwap_filter", True)),
        asset_type         = asset_type,
    )

    try:
        sig = ema.analyze(symbol=symbol, df_15m=df_15m, df_5m=df_5m,
                          option_symbol=option_symbol, strike=strike,
                          expiry=expiry, premium=premium)
    except Exception as e:
        logger.error("EMA analyze error | %s | %s", symbol, e, exc_info=True)
        return _null_ema_signal(symbol)

    if sig is None:
        return _null_ema_signal(symbol)

    # Open paper trade — guarded against duplicates
    try:
        from apps.paper_trading.services import open_trade
        from apps.orders.models import Order as _Order

        # Guard: skip if an open paper position already exists for this user+symbol.
        _already_open = _Order.objects.filter(
            user=strategy.user,
            mode=_Order.Mode.PAPER,
            status__in=[_Order.Status.OPEN, _Order.Status.PARTIAL],
            symbol_display__iexact=sig.symbol,
        ).exists()

        if _already_open:
            logger.info(
                "⏭ EMA Scalp paper trade skipped — position already open | "
                "symbol=%s | strategy=%s",
                sig.symbol, strategy.id,
            )
        else:
            qty_str = str(int(sig.quantity * sig.lot_size)) \
                      if asset_type == "options" else str(sig.quantity)

            trade_data = {
                "symbol":       sig.symbol,
                "asset_type":   asset_type,
                "side":         sig.direction.value,
                "entry_price":  str(sig.entry_price),
                "stop_loss":    str(sig.stop_loss),
                "target_price": str(sig.take_profit),
                "quantity":     qty_str,
                "lot_size":     sig.lot_size,
                "leverage":     int(strategy.parameters.get("leverage", 1)),
                "setup_type":   "ema_scalp",
                "strategy_id":  str(strategy.id),
                "display_name": f"EMA Scalp {sig.symbol}",
                "option_type":  sig.option_type,
                "strike_price": str(sig.strike) if sig.strike else "",
            }
            trade = open_trade(strategy.user, trade_data)
            # Link to strategy so _handle_ict_signal's guard can find it next cycle.
            if trade and getattr(strategy, "id", None):
                trade.strategy_id = strategy.id
                trade.save(update_fields=["strategy_id", "updated_at"])
            logger.info("EMA Scalp trade opened | %s | id=%s | %s @ %.4f",
                        sig.symbol, trade.id, sig.direction.value, sig.entry_price)
    except Exception as e:
        logger.error("open_trade failed | %s | %s", symbol, e, exc_info=True)

    # Save signal + WS push
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer
        from apps.strategies.models import StrategySignal

        StrategySignal.objects.create(
            strategy=strategy, signal_type=sig.direction.value,
            symbol=sig.symbol, price=Decimal(str(sig.entry_price)),
            reason=sig.notes, metadata=sig.to_dict(), result="executed",
        )
        layer = get_channel_layer()
        if layer:
            async_to_sync(layer.group_send)(f"user_{strategy.user_id}", {
                "type": "new_signal", "direction": sig.direction.value,
                "symbol": sig.symbol, "entry": sig.entry_price,
                "sl": sig.stop_loss, "target1": sig.take_profit,
                "target2": sig.take_profit2, "confidence": sig.confluence_score,
                "reason": sig.notes, "strategy_id": str(strategy.id),
                "algo": "ema_scalp", "rr": sig.rr_ratio, "tags": sig.tags,
                "risk_inr": sig.risk_amount, "asset_type": sig.asset_type,
                "option_type": sig.option_type,
            })
    except Exception as e:
        logger.warning("WS push failed | %s", e)

    return {
        "signal_type": sig.direction.value,
        "symbol":      sig.symbol,
        "price":       Decimal(str(sig.entry_price)),
        "reason":      sig.notes,
        "metadata":    sig.to_dict(),
        "result":      "executed",
        "order":       None,
    }


# ─────────────────────────────────────────────
# 📈 BACKTEST
# ─────────────────────────────────────────────
def run_ema_scalp_backtest(strategy, from_date: str, to_date: str) -> dict:
    import numpy as np

    asset_type = strategy.parameters.get("asset_type", "crypto")
    htf_tf     = strategy.parameters.get("htf", "15")
    ltf_tf     = strategy.parameters.get("ltf", "5")

    def _fetch(symbol, resolution):
        from apps.common.candle_service import fetch_candles_range
        try:
            return fetch_candles_range(symbol, resolution, from_date, to_date) or []
        except Exception as e:
            logger.error("Backtest fetch error: %s", e)
            return []

    candles_15m = _fetch(strategy.symbol, htf_tf)
    candles_5m  = _fetch(strategy.symbol, ltf_tf)

    if len(candles_5m) < 100:
        raise RuntimeError(f"Insufficient data: {len(candles_5m)} bars")

    def _to_df(candles):
        rows = []
        for c in candles:
            if hasattr(c, "open"):
                rows.append({"ts": c.timestamp, "open": float(c.open),
                             "high": float(c.high), "low": float(c.low),
                             "close": float(c.close), "volume": float(c.volume)})
            else:
                rows.append({"ts": c.get("ts", 0), "open": float(c.get("open", 0)),
                             "high": float(c.get("high", 0)), "low": float(c.get("low", 0)),
                             "close": float(c.get("close", 0)), "volume": float(c.get("volume", 0))})
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        df.index = pd.to_datetime(df["ts"], unit="s", utc=True)
        return df.drop(columns=["ts"])

    df_15m = _to_df(candles_15m)
    df_5m  = _to_df(candles_5m)

    ema_s = EMAScalpStrategy(
        account_balance    = float(strategy.parameters.get("capital", 100_000)),
        risk_per_trade_pct = float(strategy.parameters.get("risk_pct", 1.0)),
        fast_ema_period    = int(strategy.parameters.get("fast_ema", 9)),
        slow_ema_period    = int(strategy.parameters.get("slow_ema", 21)),
        trend_ema_period   = int(strategy.parameters.get("trend_ema", 50)),
        rsi_overbought     = float(strategy.parameters.get("rsi_ob", 65.0)),
        rsi_oversold       = float(strategy.parameters.get("rsi_os", 35.0)),
        atr_sl_multiplier  = float(strategy.parameters.get("atr_mult", 1.5)),
        rr_ratio           = float(strategy.parameters.get("rr", 2.0)),
        min_volume_ratio   = float(strategy.parameters.get("min_vol", 1.0)),
        cross_lookback     = int(strategy.parameters.get("cross_lookback", 3)),
        use_trend_filter   = bool(strategy.parameters.get("trend_filter", True)),
        asset_type         = asset_type,
    )

    capital    = ema_s.account_balance
    balance    = capital
    trades     = []
    open_trade = None
    warmup     = max(ema_s.slow_period, ema_s.trend_period) + 5

    for i in range(warmup, len(df_5m)):
        w5    = df_5m.iloc[:i + 1]
        last_ts = w5.index[-1]

        # Exit check
        if open_trade:
            bh = float(df_5m["high"].iloc[i])
            bl = float(df_5m["low"].iloc[i])
            closed, exit_px, reason = False, 0.0, ""
            d = open_trade["direction"]
            if d == "long":
                if bl <= open_trade["sl"]:
                    exit_px, reason, closed = open_trade["sl"], "SL", True
                elif bh >= open_trade["tp2"]:
                    exit_px, reason, closed = open_trade["tp2"], "TP2", True
                elif bh >= open_trade["tp"]:
                    exit_px, reason, closed = open_trade["tp"], "TP1", True
            else:
                if bh >= open_trade["sl"]:
                    exit_px, reason, closed = open_trade["sl"], "SL", True
                elif bl <= open_trade["tp2"]:
                    exit_px, reason, closed = open_trade["tp2"], "TP2", True
                elif bl <= open_trade["tp"]:
                    exit_px, reason, closed = open_trade["tp"], "TP1", True

            if closed:
                pnl = (exit_px - open_trade["entry"]) * open_trade["qty"] \
                      if d == "long" else \
                      (open_trade["entry"] - exit_px) * open_trade["qty"]
                if asset_type == "options":
                    pnl *= open_trade["lot_size"]
                balance += pnl
                trades.append({
                    "entry_ts": open_trade["entry_ts"], "exit_ts": str(last_ts),
                    "side": d, "entry_price": round(open_trade["entry"], 4),
                    "exit_price": round(exit_px, 4), "qty": open_trade["qty"],
                    "lot_size": open_trade.get("lot_size", 1),
                    "pnl": round(pnl, 2), "balance": round(balance, 2),
                    "reason": reason, "tags": open_trade["tags"],
                })
                open_trade = None

        if open_trade:
            continue

        df15s = df_15m[df_15m.index <= last_ts]
        if len(df15s) < ema_s.trend_period:
            continue

        sim_premium = round(float(w5["close"].iloc[-1]) * 0.01, 2) \
                      if asset_type == "options" else 0.0

        try:
            sig = ema_s.analyze(strategy.symbol, df15s, w5, premium=sim_premium)
        except Exception as e:
            logger.debug("BT analyze error: %s", e)
            continue

        if sig is None:
            continue

        rpu = abs(sig.entry_price - sig.stop_loss)
        if rpu <= 0:
            continue
        ra  = balance * (ema_s.risk_per_trade_pct / 100)
        qty = max(1, int(ra / (sig.entry_price * sig.lot_size))) \
              if asset_type == "options" else ra / rpu

        open_trade = {
            "direction": sig.direction.value, "entry": sig.entry_price,
            "sl": sig.stop_loss, "tp": sig.take_profit, "tp2": sig.take_profit2,
            "qty": qty, "lot_size": sig.lot_size,
            "entry_ts": str(last_ts), "tags": sig.tags,
        }

    # Stats
    total  = len(trades)
    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    net    = round(balance - capital, 2)
    aw     = round(sum(w["pnl"] for w in wins)   / len(wins),   2) if wins   else 0
    al     = round(sum(l["pnl"] for l in losses) / len(losses), 2) if losses else 0
    gp     = sum(w["pnl"] for w in wins)
    gl     = abs(sum(l["pnl"] for l in losses))
    pf     = round(gp / gl, 2) if gl else 0.0

    peak = bal = capital
    max_dd = 0.0
    for t in trades:
        bal  += t["pnl"]
        peak  = max(peak, bal)
        max_dd = max(max_dd, (peak - bal) / peak * 100)
    max_dd = round(max_dd, 2)

    pnls = [t["pnl"] for t in trades]
    sharpe = sortino = 0.0
    if len(pnls) > 1:
        rets    = [p / capital for p in pnls]
        avg_r   = float(np.mean(rets))
        std_r   = float(np.std(rets))
        sharpe  = round(avg_r / std_r * np.sqrt(252), 2) if std_r else 0.0
        neg_r   = [r for r in rets if r < 0]
        dstd    = float(np.std(neg_r)) if neg_r else 0.0
        sortino = round(avg_r / dstd * np.sqrt(252), 2) if dstd else 0.0

    wr   = len(wins) / total if total else 0
    exp  = round((wr * aw) + ((1 - wr) * al), 2)
    cal  = round(net / capital * 100 / max_dd, 2) if max_dd else 0.0

    return {
        "strategy_name":   strategy.name,
        "algo_name":       "ema_scalp",
        "symbol":          strategy.symbol,
        "asset_type":      asset_type,
        "from_date":       from_date,
        "to_date":         to_date,
        "timeframe":       f"{ltf_tf}m",
        "total_candles":   len(df_5m),
        "total_trades":    total,
        "win_trades":      len(wins),
        "loss_trades":     len(losses),
        "win_rate":        round(wr * 100, 1),
        "initial_capital": capital,
        "final_balance":   round(balance, 2),
        "net_pnl":         net,
        "return_pct":      round(net / capital * 100, 2),
        "avg_win":         aw,
        "avg_loss":        al,
        "profit_factor":   pf,
        "max_drawdown":    max_dd,
        "sharpe_ratio":    sharpe,
        "sortino_ratio":   sortino,
        "calmar_ratio":    cal,
        "expectancy":      exp,
        "equity_curve":    [{"ts": t["exit_ts"], "equity": t["balance"]} for t in trades],
        "trades":          trades[-100:],
    }


# ─────────────────────────────────────────────
# 🔇 NULL SIGNAL
# ─────────────────────────────────────────────
def _null_ema_signal(symbol: str) -> dict:
    return {
        "signal_type": "hold",
        "symbol":      symbol,
        "price":       Decimal("0"),
        "reason":      "No EMA Scalp setup",
        "metadata":    {},
        "result":      "skipped",
        "order":       None,
    }