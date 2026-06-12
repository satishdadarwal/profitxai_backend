"""
ICT "One Setup For Life" Strategy
=====================================
Model: PM Range → Liquidity Sweep (Judas Swing) → CISD/MSS → FVG/OB Retracement Entry

Steps:
1. Prior Range  : previous day's high/low (D-resolution candle, same approach as
                  ORB Gap's prior-day reference).
2. Judas Swing  : in the current session price sweeps BEYOND one side of that range,
                  taking out liquidity (BSL above high, or SSL below low).
                  ssl_swept → bias = long; bsl_swept → bias = short.
3. CISD / MSS   : after the sweep, price must break market structure in the bias
                  direction on the 5m TF (detect_bos_choch + swing_indices).
                  HARD requirement — no MSS = no signal.
4. FVG / OB     : a Fair Value Gap (or Order Block fallback) must exist in the
                  displacement leg and current price must be retesting it.
                  HARD requirement per the model — entering without CISD+FVG/OB is
                  explicitly "a fundamental mistake".
5. Stop Loss    : beyond the swept liquidity extreme (sweep candle high/low).
6. Target       : nearest opposing intact BSL/SSL via detect_liquidity, fallback
                  to fixed RR (min_rr) if no qualifying level found.
7. Timing       : India killzones (INDIA_OPENING / INDIA_CLOSING) for NSE options;
                  London/NY killzones for crypto. Filter can be disabled via
                  killzone_filter=False.

Symbols: NIFTY/BANKNIFTY/SENSEX (options buyer) and BTCUSD/ETHUSD (crypto perps).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Optional

import pandas as pd

from .ict import BreakDirection, detect_bos_choch, swing_indices
from .ict.fvg import open_fvgs, FVGType
from .ict.order_block import active_order_blocks, OBType
from .ict.liquidity import detect_liquidity, LiqStatus
from .ict.killzone import get_killzone_context, KZName
from apps.backtest.algos.confluence_options import _atr, _strike_price, _dte

logger = logging.getLogger(__name__)

_OPTIONS_SYMBOLS = frozenset({"NIFTY", "BANKNIFTY", "SENSEX"})
_INDIA_KZ = {KZName.INDIA_OPENING, KZName.INDIA_CLOSING, KZName.INDIA_MID}
_CRYPTO_KZ = {KZName.LONDON_OPEN, KZName.NEW_YORK_OPEN, KZName.LONDON_NY_OVERLAP}


class OneSetupDirection(str, Enum):
    LONG = "long"
    SHORT = "short"
    NONE = "none"


@dataclass
class OneSetupSignal:
    direction: OneSetupDirection
    symbol: str
    entry_price: float
    stop_loss: float
    take_profit: float
    swept_level: float
    sweep_type: str          # "ssl_swept" | "bsl_swept"
    fvg_top: float
    fvg_bottom: float
    fvg_mid: float
    entry_zone_type: str     # "fvg" | "ob"
    mss_break_price: float
    target_liq_found: bool
    risk_points: float
    reward_points: float
    rr_ratio: float
    confluence_score: float
    tags: list = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "direction": self.direction.value,
            "symbol": self.symbol,
            "entry_price": round(self.entry_price, 4),
            "stop_loss": round(self.stop_loss, 4),
            "take_profit": round(self.take_profit, 4),
            "swept_level": round(self.swept_level, 4),
            "sweep_type": self.sweep_type,
            "fvg_zone": {
                "top": round(self.fvg_top, 4),
                "bottom": round(self.fvg_bottom, 4),
                "mid": round(self.fvg_mid, 4),
            },
            "entry_zone_type": self.entry_zone_type,
            "mss_break_price": round(self.mss_break_price, 4),
            "target_liq_found": self.target_liq_found,
            "risk_points": round(self.risk_points, 4),
            "reward_points": round(self.reward_points, 4),
            "rr_ratio": round(self.rr_ratio, 2),
            "confluence": round(self.confluence_score, 1),
            "tags": self.tags,
            "notes": self.notes,
        }


class OneSetupStrategy:
    """
    PM-range sweep + CISD/MSS + FVG/OB retracement.
    Instrument-agnostic: spot/crypto SL/TP values; caller converts to
    option premiums for NSE options symbols.
    """

    def __init__(
        self,
        min_rr: float = 2.0,
        min_score: float = 50.0,
        killzone_filter: bool = True,
    ):
        self.min_rr = min_rr
        self.min_score = min_score
        self.killzone_filter = killzone_filter

    # ------------------------------------------------------------------
    # Step 1: Prior range
    # ------------------------------------------------------------------
    def _get_prior_range(self, df_daily: pd.DataFrame) -> Optional[dict]:
        if df_daily is None or len(df_daily) < 2:
            return None
        prev = df_daily.iloc[-2]
        high = float(prev["high"])
        low = float(prev["low"])
        if high <= low:
            return None
        return {"high": high, "low": low, "mid": (high + low) / 2.0}

    # ------------------------------------------------------------------
    # Step 2: Sweep detection (Judas Swing)
    # ------------------------------------------------------------------
    def _detect_sweep(
        self, df_15m: pd.DataFrame, prior_range: dict
    ) -> tuple:
        """
        Returns (sweep_type, swept_level, sweep_ts, sweep_candle_extreme) or
        (None, None, None, None).

        Searches the last 80 bars (one full NSE session + buffer) for the most
        recent bar that wicked beyond the prior range.  BSL sweep (high > prior
        high) → bearish bias; SSL sweep (low < prior low) → bullish bias.
        """
        if df_15m is None or len(df_15m) < 3:
            return None, None, None, None

        prior_high = prior_range["high"]
        prior_low = prior_range["low"]
        window = df_15m.iloc[-80:]

        for i in range(len(window) - 1, -1, -1):
            row = window.iloc[i]
            h = float(row["high"])
            l = float(row["low"])

            if h > prior_high:
                return "bsl_swept", prior_high, window.index[i], h
            if l < prior_low:
                return "ssl_swept", prior_low, window.index[i], l

        return None, None, None, None

    # ------------------------------------------------------------------
    # Step 3: MSS / CISD confirmation on 5m
    # ------------------------------------------------------------------
    def _confirm_mss(
        self, df_5m: pd.DataFrame, direction: str, after_ts: pd.Timestamp
    ) -> tuple:
        """
        Returns (confirmed: bool, mss_break_price: float).
        Uses detect_bos_choch on 5m bars that came AFTER the sweep timestamp.
        """
        df_after = df_5m[df_5m.index > after_ts]
        if len(df_after) < 8:
            return False, 0.0

        sh_idx, sl_idx = swing_indices(
            df_after, method="fractal", left_bars=2, right_bars=2
        )
        breaks = detect_bos_choch(df_after, sh_idx, sl_idx)
        if not breaks:
            return False, 0.0

        want = BreakDirection.BULLISH if direction == "long" else BreakDirection.BEARISH
        for b in reversed(breaks):
            if b.direction == want:
                return True, float(b.break_close)
        return False, 0.0

    # ------------------------------------------------------------------
    # Step 4: FVG / OB retracement zone
    # ------------------------------------------------------------------
    def _find_entry_fvg_ob(
        self,
        df: pd.DataFrame,
        direction: str,
        current_price: float,
        atr: float,
    ) -> Optional[dict]:
        """
        Looks for an open/partial FVG (or active OB as fallback) in `df` that
        current price is currently retesting.  Returns None if neither found or
        price is not in the zone.
        """
        if len(df) < 5:
            return None

        buf = atr * 0.35
        want_fvg = FVGType.BULLISH if direction == "long" else FVGType.BEARISH

        # -- FVG first --
        fvgs = open_fvgs(df)
        candidates = [f for f in fvgs if f.fvg_type == want_fvg]
        if candidates:
            nearest = min(candidates, key=lambda f: abs(f.mid - current_price))
            if (nearest.bottom - buf) <= current_price <= (nearest.top + buf):
                return {
                    "top": nearest.top,
                    "bottom": nearest.bottom,
                    "mid": nearest.mid,
                    "type": "fvg",
                    "significant": nearest.is_significant,
                }

        # -- OB fallback --
        sh_idx, sl_idx = swing_indices(df, method="fractal", left_bars=2, right_bars=2)
        obs = active_order_blocks(df, sh_idx, sl_idx)
        want_ob = OBType.BULLISH if direction == "long" else OBType.BEARISH
        ob_candidates = [ob for ob in obs if ob.ob_type == want_ob]
        if ob_candidates:
            nearest_ob = min(ob_candidates, key=lambda ob: abs(ob.mid - current_price))
            if (nearest_ob.bottom - buf) <= current_price <= (nearest_ob.top + buf):
                return {
                    "top": nearest_ob.top,
                    "bottom": nearest_ob.bottom,
                    "mid": nearest_ob.mid,
                    "type": "ob",
                    "significant": nearest_ob.is_institutional,
                }

        return None

    # ------------------------------------------------------------------
    # Step 6: Target liquidity
    # ------------------------------------------------------------------
    def _find_target_liquidity(
        self,
        df: pd.DataFrame,
        direction: str,
        entry_price: float,
        risk: float,
    ) -> tuple:
        """Returns (target_price, liq_found: bool)."""
        try:
            sh_idx, sl_idx = swing_indices(
                df, method="fractal", left_bars=2, right_bars=2
            )
            liq_map = detect_liquidity(df, sh_idx, sl_idx)
            min_dist = risk * self.min_rr * 0.8

            if direction == "long":
                intact = [
                    l for l in liq_map.bsl_levels
                    if l.status == LiqStatus.INTACT and l.price > entry_price + min_dist
                ]
                if intact:
                    return min(intact, key=lambda l: l.price).price, True
            else:
                intact = [
                    l for l in liq_map.ssl_levels
                    if l.status == LiqStatus.INTACT and l.price < entry_price - min_dist
                ]
                if intact:
                    return max(intact, key=lambda l: l.price).price, False
        except Exception:
            pass

        # RR-based fallback
        tp = (
            entry_price + risk * self.min_rr
            if direction == "long"
            else entry_price - risk * self.min_rr
        )
        return tp, False

    # ------------------------------------------------------------------
    # Killzone check
    # ------------------------------------------------------------------
    def _in_killzone(self, ts: pd.Timestamp, symbol: str) -> bool:
        if not self.killzone_filter:
            return True
        try:
            ctx = get_killzone_context(ts)
            active_names = {z.name for z in ctx.active_zones}
            want = _INDIA_KZ if symbol.upper() in _OPTIONS_SYMBOLS else _CRYPTO_KZ
            return bool(active_names & want)
        except Exception:
            return True

    # ------------------------------------------------------------------
    # Main entrypoint
    # ------------------------------------------------------------------
    def analyze(
        self,
        symbol: str,
        df_daily: pd.DataFrame,
        df_15m: pd.DataFrame,
        df_5m: pd.DataFrame,
    ) -> Optional[OneSetupSignal]:
        if df_daily is None or df_15m is None or df_5m is None:
            return None
        if df_daily.empty or len(df_15m) < 20 or len(df_5m) < 20:
            return None

        # Step 1
        prior_range = self._get_prior_range(df_daily)
        if prior_range is None:
            return None

        # Step 2
        sweep_type, swept_level, sweep_ts, sweep_extreme = self._detect_sweep(
            df_15m, prior_range
        )
        if sweep_type is None:
            return None

        direction = "long" if sweep_type == "ssl_swept" else "short"

        # Killzone filter
        current_ts = df_15m.index[-1]
        if not self._in_killzone(current_ts, symbol):
            logger.debug("[OneSetup] %s: outside killzone, skip", symbol)
            return None

        # Step 3: MSS on 5m after sweep
        mss_ok, mss_price = self._confirm_mss(df_5m, direction, sweep_ts)
        if not mss_ok:
            logger.debug("[OneSetup] %s: MSS not confirmed after sweep", symbol)
            return None

        # Displacement slice: from sweep candle onwards on 15m
        disp_df = df_15m[df_15m.index >= sweep_ts]
        if len(disp_df) < 3:
            return None

        current_price = float(df_15m.iloc[-1]["close"])
        candles_list = [
            {"high": float(r.high), "low": float(r.low), "close": float(r.close)}
            for _, r in df_15m.iterrows()
        ]
        atr_val = _atr(candles_list, period=14)
        if not atr_val or atr_val <= 0:
            return None

        # Step 4: FVG / OB — HARD requirement
        zone = self._find_entry_fvg_ob(disp_df, direction, current_price, atr_val)
        if zone is None:
            logger.debug(
                "[OneSetup] %s: no FVG/OB retracement zone found — signal not qualified",
                symbol,
            )
            return None

        entry = current_price
        zone_type = zone["type"]

        # Step 5: SL beyond sweep extreme
        buf = atr_val * 0.25
        if direction == "long":
            sl = sweep_extreme - buf   # sweep extreme is the candle's low
            if sl >= entry:
                return None
            risk = entry - sl
        else:
            sl = sweep_extreme + buf   # sweep extreme is the candle's high
            if sl <= entry:
                return None
            risk = sl - entry

        if risk <= 0:
            return None

        # Step 6: Target
        tp, liq_found = self._find_target_liquidity(df_15m, direction, entry, risk)
        if direction == "long" and tp <= entry:
            tp = entry + risk * self.min_rr
        if direction == "short" and tp >= entry:
            tp = entry - risk * self.min_rr

        reward = abs(tp - entry)
        rr = reward / risk if risk > 0 else 0.0
        if rr < self.min_rr * 0.8:
            return None

        # Scoring
        score = 45.0
        score += 25.0 if zone_type == "fvg" else 12.0   # FVG > OB
        if zone.get("significant"):
            score += 8.0
        if rr >= self.min_rr:
            score += 10.0
        if liq_found:
            score += 8.0
        if self._in_killzone(current_ts, symbol):
            score += 5.0
        score = min(score, 100.0)

        if score < self.min_score:
            return None

        tags = ["one_setup", direction, sweep_type, zone_type]
        if liq_found:
            tags.append("liq_target")

        notes = (
            f"{sweep_type} @ {swept_level:.2f} → MSS {direction} break @ {mss_price:.2f} "
            f"→ {zone_type.upper()} retest [{zone['bottom']:.2f}-{zone['top']:.2f}]"
        )

        return OneSetupSignal(
            direction=OneSetupDirection(direction),
            symbol=symbol,
            entry_price=entry,
            stop_loss=sl,
            take_profit=tp,
            swept_level=swept_level,
            sweep_type=sweep_type,
            fvg_top=zone["top"],
            fvg_bottom=zone["bottom"],
            fvg_mid=zone["mid"],
            entry_zone_type=zone_type,
            mss_break_price=mss_price,
            target_liq_found=liq_found,
            risk_points=risk,
            reward_points=reward,
            rr_ratio=rr,
            confluence_score=score,
            tags=tags,
            notes=notes,
        )


# ---------------------------------------------------------------------------
# Cycle helpers
# ---------------------------------------------------------------------------

def _null_one_setup_signal(symbol: str) -> dict:
    return {
        "signal_type": "hold",
        "symbol": symbol,
        "price": Decimal("0"),
        "reason": "No OneSetup signal",
        "metadata": {},
        "result": "skipped",
        "order": None,
    }


def _execute_one_setup_options(
    strategy, symbol: str, sig: OneSetupSignal
) -> dict:
    """Options execution wrapper — mirrors _execute_hourly_macro_options."""
    try:
        from apps.orders.models import Order as _Order
        from django.db.models import Q
        _user = getattr(strategy, "user", None)
        _qs = _Order.objects.filter(
            Q(symbol_display__icontains=symbol) | Q(asset__symbol__icontains=symbol),
            status__in=["open", "pending"],
        )
        if _user:
            _qs = _qs.filter(user=_user)
        if _qs.exists():
            logger.info("[OneSetup] Duplicate skip: open position exists for %s", symbol)
            return _null_one_setup_signal(symbol)
    except Exception as e:
        logger.warning("[OneSetup] Duplicate check failed | %s", e)

    option_type = "CE" if sig.direction == OneSetupDirection.LONG else "PE"
    spot = sig.entry_price
    strike = _strike_price(spot, symbol, option_type, otm_shift=0)
    dte = _dte(symbol)

    option_premium = None
    abs_delta = None
    try:
        from apps.options.black_scholes import compute_greeks
        T = max(dte / 365, 0.001)
        bs_type = "call" if option_type == "CE" else "put"
        g = compute_greeks(spot, strike, T, 0.065, 0.15, bs_type)
        abs_delta = abs(g["delta"])
        theta = g["theta"]
        if not (0.25 <= abs_delta <= 0.65):
            logger.debug(
                "[OneSetup] %s delta=%.3f out of range | %s", option_type, abs_delta, symbol
            )
            return _null_one_setup_signal(symbol)
        if theta < -15:
            logger.debug(
                "[OneSetup] %s theta=%.2f too high decay | %s", option_type, theta, symbol
            )
            return _null_one_setup_signal(symbol)
        option_premium = round(g["price"], 2)
    except Exception:
        pass

    # Convert spot SL/TP to option premium levels via delta approximation
    if option_premium and abs_delta:
        if option_type == "CE":
            spot_risk   = sig.entry_price - sig.stop_loss
            spot_reward = sig.take_profit - sig.entry_price
        else:
            spot_risk   = sig.stop_loss - sig.entry_price
            spot_reward = sig.entry_price - sig.take_profit
        premium_sl = round(max(option_premium - abs_delta * spot_risk, 1.0), 2)
        premium_tp = round(option_premium + abs_delta * spot_reward, 2)
    else:
        premium_sl = round(option_premium * 0.70, 2) if option_premium else None
        premium_tp = round(option_premium * 1.60, 2) if option_premium else None

    entry_price_dec = Decimal(str(option_premium)) if option_premium else Decimal(str(spot))

    logger.info(
        "✅ OneSetup Options signal | %s | %s | dir=%s | spot=%.2f | "
        "premium=%.2f | SL=%.2f | TP=%.2f | RR=%.2f | score=%.1f | "
        "sweep=%s | zone=%s [%.2f-%.2f] | strike=%d | DTE=%d",
        symbol, option_type, sig.direction.value, spot,
        option_premium or 0.0, premium_sl or 0.0, premium_tp or 0.0,
        sig.rr_ratio, sig.confluence_score,
        sig.sweep_type, sig.entry_zone_type, sig.fvg_bottom, sig.fvg_top,
        strike, dte,
    )

    sig_meta = sig.to_dict()
    sig_meta.update({
        "option_type": option_type,
        "strike": strike,
        "dte": dte,
        "entry_premium": option_premium,
        "spot_sl": sig.stop_loss,
        "spot_tp": sig.take_profit,
        "stop_loss":   premium_sl  if premium_sl  else sig.stop_loss,
        "take_profit": premium_tp  if premium_tp  else sig.take_profit,
        "setup_type": f"OneSetup_{sig.sweep_type}_{option_type}_{symbol}",
    })

    return {
        "signal_type": "buy",
        "symbol": symbol,
        "price": entry_price_dec,
        "reason": f"OneSetup {sig.sweep_type} {option_type} | {sig.notes}",
        "metadata": sig_meta,
        "result": "executed",
        "order": None,
    }


def execute_one_setup_cycle(strategy, symbol: str) -> dict:
    """
    Live/paper cycle entrypoint for the One Setup For Life strategy.

    Fetches D + 5m candles; resamples 5m→15m for sweep/FVG detection and
    uses 5m directly for MSS confirmation.  Options symbols (NIFTY/BANKNIFTY/
    SENSEX) go through _execute_one_setup_options for Greeks gating and
    premium-level SL/TP conversion.  Crypto perps (BTCUSD/ETHUSD) return
    buy/sell with spot-based SL/TP directly.
    """
    from apps.common.candle_service import fetch_candles_for_strategy

    def _to_df(candles: list) -> pd.DataFrame:
        rows = []
        for c in candles:
            if hasattr(c, "open"):
                rows.append({
                    "ts": c.timestamp,
                    "open": float(c.open), "high": float(c.high),
                    "low": float(c.low), "close": float(c.close),
                    "volume": float(c.volume),
                })
            else:
                rows.append({
                    "ts": c.get("ts", 0),
                    "open": float(c.get("open", 0)), "high": float(c.get("high", 0)),
                    "low": float(c.get("low", 0)), "close": float(c.get("close", 0)),
                    "volume": float(c.get("volume", 0)),
                })
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        df.index = pd.to_datetime(df["ts"], unit="s", utc=True)
        return df.drop(columns=["ts"]).sort_index()

    # Fetch candles
    try:
        daily_raw = fetch_candles_for_strategy(strategy, symbol, "D", bars=10) or []
        ltf_raw   = fetch_candles_for_strategy(strategy, symbol, "5", bars=500) or []
    except TypeError:
        try:
            daily_raw = fetch_candles_for_strategy(strategy, symbol, "D") or []
            ltf_raw   = fetch_candles_for_strategy(strategy, symbol, "5") or []
        except Exception as e:
            logger.error("OneSetup candle fetch error | symbol=%s | err=%s", symbol, e)
            return _null_one_setup_signal(symbol)
    except Exception as e:
        logger.error("OneSetup candle fetch error | symbol=%s | err=%s", symbol, e)
        return _null_one_setup_signal(symbol)

    df_daily = _to_df(daily_raw)
    df_5m    = _to_df(ltf_raw)

    if df_daily.empty or len(df_5m) < 20:
        return _null_one_setup_signal(symbol)

    # Resample 5m → 15m for sweep + FVG detection
    df_15m = df_5m.resample("15min").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    }).dropna()

    if len(df_15m) < 10:
        return _null_one_setup_signal(symbol)

    strat = OneSetupStrategy(
        min_rr=float(strategy.parameters.get("min_rr", 2.0)),
        min_score=float(strategy.parameters.get("min_score", 50.0)),
        killzone_filter=bool(strategy.parameters.get("killzone_filter", True)),
    )

    try:
        sig = strat.analyze(
            symbol=symbol,
            df_daily=df_daily,
            df_15m=df_15m,
            df_5m=df_5m,
        )
    except Exception as e:
        logger.error(
            "OneSetup analyze error | symbol=%s | err=%s", symbol, e, exc_info=True
        )
        return _null_one_setup_signal(symbol)

    if sig is None:
        return _null_one_setup_signal(symbol)

    # Route by instrument type
    if symbol.upper() in _OPTIONS_SYMBOLS:
        return _execute_one_setup_options(strategy, symbol, sig)

    # --- Crypto perp path (BTCUSD / ETHUSD) ---
    try:
        from apps.orders.models import Order as _Order
        from django.db.models import Q
        clean_symbol = symbol.replace("USD", "").replace("-USDT", "").strip()
        _user = getattr(strategy, "user", None)
        _qs = _Order.objects.filter(
            Q(symbol_display__icontains=clean_symbol) | Q(asset__symbol__icontains=clean_symbol),
            status__in=["open", "pending"],
        )
        if _user:
            _qs = _qs.filter(user=_user)
        if _qs.exists():
            logger.info("[OneSetup] Duplicate skip: open position for %s", symbol)
            return _null_one_setup_signal(symbol)
    except Exception as e:
        logger.warning("[OneSetup] Duplicate check failed | %s", e)

    side = "buy" if sig.direction == OneSetupDirection.LONG else "sell"

    logger.info(
        "✅ OneSetup Crypto signal | %s | dir=%s | sweep=%s | zone=%s | "
        "entry=%.4f | SL=%.4f | TP=%.4f | RR=%.2f | score=%.1f",
        symbol, sig.direction.value, sig.sweep_type, sig.entry_zone_type,
        sig.entry_price, sig.stop_loss, sig.take_profit,
        sig.rr_ratio, sig.confluence_score,
    )

    sig_meta = sig.to_dict()
    sig_meta.update({
        "setup_type": f"OneSetup_{sig.sweep_type}_{symbol}",
    })

    return {
        "signal_type": side,
        "symbol": symbol,
        "price": Decimal(str(sig.entry_price)),
        "reason": f"OneSetup {sig.sweep_type} {sig.direction.value} | {sig.notes}",
        "metadata": sig_meta,
        "result": "executed",
        "order": None,
    }
