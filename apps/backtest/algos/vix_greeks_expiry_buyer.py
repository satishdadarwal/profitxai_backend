"""
VIX Greeks Expiry Buyer Strategy
=================================
Author: ProfitXAI
Time Window: 1:00 PM - 3:00 PM IST (NY Open Killzone equivalent for India)
Instruments: NIFTY, BANKNIFTY, SENSEX options (CE/PE buyer)
Expiry: DTE <= 3 (expiry week only)
Target Win Rate: 70%+
RR: 1:3

Entry Logic (ALL conditions must pass):
1. Time: 1 PM - 3 PM IST only
2. VIX Filter:
   - VIX < 14: Buy options (low fear = trending market)
   - VIX 14-18: Cautious — need higher confluence (score >= 75)
   - VIX > 18: Skip (too expensive premium + uncertain direction)
3. ICT Structure:
   - Liquidity sweep detected (BSL/SSL)
   - MSS (Market Structure Shift) confirmed
   - FVG present (Fair Value Gap)
4. Greeks Filter:
   - Delta: 0.35-0.55 (near ATM, not too OTM)
   - Theta: > -8/day (acceptable decay)
   - Gamma: > 0.003 (enough sensitivity)
5. Options Chain Confluence:
   - PCR divergence from trend
   - Max Pain alignment
   - OI wall as target
6. Momentum:
   - RSI: 30-45 for PE buy, 55-70 for CE buy
   - VWAP: Price below for PE, above for CE
   - EMA 9 cross on 5-min

SL: 30% of premium (Greeks-adjusted)
TP: 90% of premium (1:3 RR on premium basis)
"""

import math
import logging
from typing import Optional
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────
VIX_BUY_MAX      = 18.0   # VIX above this → skip
VIX_LOW_THRESH   = 14.0   # VIX below this → strong buy signal
VIX_MID_THRESH   = 18.0   # VIX 14-18 → need higher score
DTE_MAX          = 3      # Only trade expiry week
MIN_SCORE_NORMAL = 65     # Min confluence score (VIX < 14)
MIN_SCORE_HIGH   = 75     # Min confluence score (VIX 14-18)
DELTA_MIN        = 0.30   # Min delta for CE (or abs for PE)
DELTA_MAX        = 0.55   # Max delta
THETA_MAX        = -10.0  # Max theta decay per day (negative)
GAMMA_MIN        = 0.002  # Min gamma
SL_PCT           = 30.0   # SL = 30% of premium
TP_PCT           = 90.0   # TP = 90% of premium (1:3 RR)
RSI_PE_MIN       = 25
RSI_PE_MAX       = 48
RSI_CE_MIN       = 52
RSI_CE_MAX       = 75


@dataclass
class VixGreeksSignal:
    signal: str           # 'buy_ce', 'buy_pe', 'hold'
    score: float          # 0-100
    option_type: str      # 'CE' or 'PE'
    symbol: str
    spot: float
    vix: float
    dte: int
    delta: float
    theta: float
    gamma: float
    sl_pct: float
    tp_pct: float
    rr: float
    reasons: list = field(default_factory=list)
    reject_reasons: list = field(default_factory=list)


def _ema(values: list, period: int) -> float:
    """Simple EMA calculation."""
    if len(values) < period:
        return values[-1] if values else 0
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema


def _rsi(closes: list, period: int = 14) -> float:
    """RSI calculation."""
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 2)


def _vwap(candles: list) -> float:
    """VWAP from candles list."""
    if not candles:
        return 0
    tp_vol = sum(
        ((c.get('high', 0) + c.get('low', 0) + c.get('close', 0)) / 3) * c.get('volume', 1)
        for c in candles
    )
    vol = sum(c.get('volume', 1) for c in candles)
    return tp_vol / vol if vol > 0 else 0


def _atr(candles: list, period: int = 14) -> float:
    """ATR calculation."""
    if len(candles) < 2:
        return 0
    trs = []
    for i in range(1, len(candles)):
        h = candles[i].get('high', 0)
        l = candles[i].get('low', 0)
        pc = candles[i-1].get('close', 0)
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-period:]) / min(len(trs), period)


def _detect_liquidity_sweep(candles_5m: list, lookback: int = 20) -> dict:
    """
    ICT Liquidity sweep detection.
    BSL (Buy Side Liquidity) sweep = price wicks above recent high then closes below
    SSL (Sell Side Liquidity) sweep = price wicks below recent low then closes above
    """
    if len(candles_5m) < lookback + 2:
        return {'swept': False, 'type': None, 'level': 0}

    recent = candles_5m[-lookback-1:-1]
    last = candles_5m[-1]

    recent_high = max(c.get('high', 0) for c in recent)
    recent_low = min(c.get('low', float('inf')) for c in recent)

    last_high = last.get('high', 0)
    last_low = last.get('low', 0)
    last_close = last.get('close', 0)

    # BSL sweep: wick above recent high but close below
    if last_high > recent_high and last_close < recent_high:
        return {'swept': True, 'type': 'BSL', 'level': recent_high}

    # SSL sweep: wick below recent low but close above
    if last_low < recent_low and last_close > recent_low:
        return {'swept': True, 'type': 'SSL', 'level': recent_low}

    return {'swept': False, 'type': None, 'level': 0}


def _detect_mss(candles_5m: list) -> dict:
    """
    Market Structure Shift detection.
    Bearish MSS: breaks below recent swing low (after bullish structure)
    Bullish MSS: breaks above recent swing high (after bearish structure)
    """
    if len(candles_5m) < 10:
        return {'mss': False, 'direction': None}

    recent = candles_5m[-10:]
    closes = [c.get('close', 0) for c in recent]
    highs = [c.get('high', 0) for c in recent]
    lows = [c.get('low', float('inf')) for c in recent]

    last_close = closes[-1]
    prev_swing_high = max(highs[:-2])
    prev_swing_low = min(lows[:-2])

    if last_close > prev_swing_high:
        return {'mss': True, 'direction': 'bullish', 'level': prev_swing_high}
    elif last_close < prev_swing_low:
        return {'mss': True, 'direction': 'bearish', 'level': prev_swing_low}

    return {'mss': False, 'direction': None}


def _detect_fvg(candles_5m: list) -> dict:
    """
    Fair Value Gap detection (3-candle pattern).
    Bullish FVG: candle[i-2].high < candle[i].low (gap up)
    Bearish FVG: candle[i-2].low > candle[i].high (gap down)
    """
    if len(candles_5m) < 3:
        return {'fvg': False, 'type': None, 'upper': 0, 'lower': 0}

    c1 = candles_5m[-3]
    c3 = candles_5m[-1]

    c1_high = c1.get('high', 0)
    c1_low = c1.get('low', 0)
    c3_high = c3.get('high', 0)
    c3_low = c3.get('low', 0)

    # Bullish FVG
    if c1_high < c3_low:
        return {'fvg': True, 'type': 'bullish', 'upper': c3_low, 'lower': c1_high}

    # Bearish FVG
    if c1_low > c3_high:
        return {'fvg': True, 'type': 'bearish', 'upper': c1_low, 'lower': c3_high}

    return {'fvg': False, 'type': None, 'upper': 0, 'lower': 0}


def generate_signal(
    symbol: str,
    spot: float,
    candles_5m: list,
    candles_15m: list,
    vix: float,
    dte: int,
    pcr: float,
    max_pain: float,
    call_wall: float,
    put_wall: float,
    ce_delta: float,
    pe_delta: float,
    theta: float,
    gamma: float,
    iv_rank: Optional[float] = None,
    parameters: Optional[dict] = None,
) -> VixGreeksSignal:
    """
    Main signal generation function.
    Returns VixGreeksSignal with buy_ce / buy_pe / hold.
    """
    params = parameters or {}
    min_score = params.get('min_score', MIN_SCORE_NORMAL)
    reasons = []
    rejects = []
    score = 0

    # ── 1. Time Window Check (1 PM - 3 PM IST) ───────────────────
    from django.utils import timezone
    import pytz
    ist = pytz.timezone('Asia/Kolkata')
    now_ist = timezone.now().astimezone(ist)
    hour = now_ist.hour
    minute = now_ist.minute
    time_decimal = hour + minute / 60

    if not (13.0 <= time_decimal <= 15.0):
        return VixGreeksSignal(
            signal='hold', score=0, option_type='', symbol=symbol,
            spot=spot, vix=vix, dte=dte, delta=0, theta=theta,
            gamma=gamma, sl_pct=SL_PCT, tp_pct=TP_PCT, rr=3.0,
            reject_reasons=[f'Outside killzone window (1PM-3PM IST). Current: {hour}:{minute:02d}']
        )

    # ── 2. DTE Filter ──────────────────────────────────────────────
    if dte > DTE_MAX:
        return VixGreeksSignal(
            signal='hold', score=0, option_type='', symbol=symbol,
            spot=spot, vix=vix, dte=dte, delta=0, theta=theta,
            gamma=gamma, sl_pct=SL_PCT, tp_pct=TP_PCT, rr=3.0,
            reject_reasons=[f'DTE={dte} > {DTE_MAX} — not expiry week']
        )

    # ── Expiry Day Check (Post Sep 2025 SEBI rules) ───────────────
    weekday = now_ist.weekday()  # 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri
    # NSE: NIFTY weekly=Tuesday(1), BANKNIFTY/FINNIFTY monthly only (DTE<=2)
    # BSE: SENSEX weekly=Thursday(3)
    _sym = symbol.upper()
    _WEEKLY = {'NIFTY': 1, 'SENSEX': 3}
    _MONTHLY_ONLY = {'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY'}
    if _sym in _WEEKLY:
        if weekday != _WEEKLY[_sym]:
            return VixGreeksSignal(
                signal='hold', score=0, option_type='', symbol=symbol,
                spot=spot, vix=vix, dte=dte, delta=0, theta=theta,
                gamma=gamma, sl_pct=SL_PCT, tp_pct=TP_PCT, rr=3.0,
                reject_reasons=[f'Not expiry day for {symbol} (need weekday={_WEEKLY[_sym]}, today={weekday})']
            )
    elif _sym in _MONTHLY_ONLY and dte > 2:
        return VixGreeksSignal(
            signal='hold', score=0, option_type='', symbol=symbol,
            spot=spot, vix=vix, dte=dte, delta=0, theta=theta,
            gamma=gamma, sl_pct=SL_PCT, tp_pct=TP_PCT, rr=3.0,
            reject_reasons=[f'{symbol} monthly only — DTE={dte} > 2, too early']
        )

    reasons.append(f'DTE={dte} ✅')
    score += 5

    # ── 3. VIX Filter ──────────────────────────────────────────────
    if vix > VIX_BUY_MAX:
        return VixGreeksSignal(
            signal='hold', score=0, option_type='', symbol=symbol,
            spot=spot, vix=vix, dte=dte, delta=0, theta=theta,
            gamma=gamma, sl_pct=SL_PCT, tp_pct=TP_PCT, rr=3.0,
            reject_reasons=[f'VIX={vix:.1f} > {VIX_BUY_MAX} — too expensive']
        )

    if vix < VIX_LOW_THRESH:
        score += 20
        reasons.append(f'VIX={vix:.1f} < {VIX_LOW_THRESH} — low fear ✅ (+20)')
        min_score = MIN_SCORE_NORMAL
    else:
        score += 8
        reasons.append(f'VIX={vix:.1f} moderate — need higher score (+8)')
        min_score = MIN_SCORE_HIGH

    # ── 4. ICT Structure ───────────────────────────────────────────
    sweep = _detect_liquidity_sweep(candles_5m)
    mss = _detect_mss(candles_5m)
    fvg = _detect_fvg(candles_5m)

    if sweep['swept']:
        score += 20
        reasons.append(f'Liquidity sweep {sweep["type"]} @ {sweep["level"]:.0f} ✅ (+20)')
    else:
        # Sweep mandatory for ICT setup — without sweep signal not valid
        return VixGreeksSignal(
            signal='hold', score=score, option_type='', symbol=symbol,
            spot=spot, vix=vix, dte=dte, delta=0, theta=theta,
            gamma=gamma, sl_pct=SL_PCT, tp_pct=TP_PCT, rr=3.0,
            reasons=reasons, reject_reasons=['No liquidity sweep — ICT setup invalid']
        )

    if mss['mss']:
        score += 20
        reasons.append(f'MSS {mss["direction"]} confirmed ✅ (+20)')
    else:
        rejects.append('No MSS confirmed (-)')

    if fvg['fvg']:
        score += 10
        reasons.append(f'FVG {fvg["type"]} present ✅ (+10)')

    # ── 5. Determine Direction ─────────────────────────────────────
    # Priority: MSS > Sweep > PCR
    if mss['mss']:
        direction = mss['direction']  # 'bullish' or 'bearish'
    elif sweep['swept']:
        # BSL sweep → bearish reversal (price came down after sweeping highs)
        # SSL sweep → bullish reversal
        direction = 'bearish' if sweep['type'] == 'BSL' else 'bullish'
    else:
        direction = 'neutral'

    if direction == 'neutral':
        return VixGreeksSignal(
            signal='hold', score=score, option_type='', symbol=symbol,
            spot=spot, vix=vix, dte=dte, delta=0, theta=theta,
            gamma=gamma, sl_pct=SL_PCT, tp_pct=TP_PCT, rr=3.0,
            reasons=reasons, reject_reasons=rejects + ['No clear direction']
        )

    option_type = 'CE' if direction == 'bullish' else 'PE'

    # ── 5b. ICT Premium/Discount Zone Filter ──────────────────────
    # CE (bullish) → spot must be in discount zone (below equilibrium)
    # PE (bearish) → spot must be in premium zone (above equilibrium)
    try:
        import datetime as _dt_pd
        _last_ts_vg = candles_5m[-1].get('ts') or candles_5m[-1].get('timestamp') or candles_5m[-1].get('t') if candles_5m else None
        if _last_ts_vg:
            _today_d_vg = _dt_pd.datetime.fromtimestamp(float(_last_ts_vg), tz=_dt_pd.timezone.utc).date()
            _yest_d_vg = _today_d_vg - _dt_pd.timedelta(days=1)
            _prev_vg = [c for c in candles_5m if _dt_pd.datetime.fromtimestamp(float(c.get('ts') or c.get('timestamp') or c.get('t') or 0), tz=_dt_pd.timezone.utc).date() == _yest_d_vg]
        else:
            _prev_vg = []
        if _prev_vg and len(_prev_vg) > 5:
            _ph_vg = max(c.get('high', 0) for c in _prev_vg)
            _pl_vg = min(c.get('low', float('inf')) for c in _prev_vg)
            logger.debug("VixGreeks PD Zone using PDH=%.2f PDL=%.2f", _ph_vg, _pl_vg)
        else:
            # Fallback: approx yesterday session (5m NSE session ≈ 75 bars)
            _n_vg = len(candles_5m)
            _fb_vg = candles_5m[max(0, _n_vg - 150):max(0, _n_vg - 75)] or candles_5m[-75:]
            _ph_vg = max(c.get('high', 0) for c in _fb_vg)
            _pl_vg = min(c.get('low', float('inf')) for c in _fb_vg)
            logger.debug("VixGreeks PD Zone fallback session range")
        _range_vg = _ph_vg - _pl_vg
        if _range_vg > 0:
            _eq_vg = _pl_vg + (_range_vg * 0.5)
            if option_type == 'CE' and spot > _eq_vg:
                logger.info(
                    "VixGreeks CE: spot %.2f in PREMIUM zone (eq=%.2f), skip LONG",
                    spot, _eq_vg
                )
                return VixGreeksSignal(
                    signal='hold', score=score, option_type=option_type, symbol=symbol,
                    spot=spot, vix=vix, dte=dte, delta=0, theta=theta,
                    gamma=gamma, sl_pct=SL_PCT, tp_pct=TP_PCT, rr=3.0,
                    reasons=reasons,
                    reject_reasons=rejects + [f'PD Zone: spot {spot:.2f} in PREMIUM (eq={_eq_vg:.2f}), need DISCOUNT for CE']
                )
            elif option_type == 'PE' and spot < _eq_vg:
                logger.info(
                    "VixGreeks PE: spot %.2f in DISCOUNT zone (eq=%.2f), skip SHORT",
                    spot, _eq_vg
                )
                return VixGreeksSignal(
                    signal='hold', score=score, option_type=option_type, symbol=symbol,
                    spot=spot, vix=vix, dte=dte, delta=0, theta=theta,
                    gamma=gamma, sl_pct=SL_PCT, tp_pct=TP_PCT, rr=3.0,
                    reasons=reasons,
                    reject_reasons=rejects + [f'PD Zone: spot {spot:.2f} in DISCOUNT (eq={_eq_vg:.2f}), need PREMIUM for PE']
                )
            logger.info(
                "VixGreeks PD Zone ✅ | spot=%.2f eq=%.2f | %s",
                spot, _eq_vg, 'DISCOUNT' if option_type == 'CE' else 'PREMIUM'
            )
    except Exception as _pd_e:
        logger.debug("VixGreeks PD Zone check skipped: %s", _pd_e)

    # ── 6. Greeks Filter ───────────────────────────────────────────
    delta = ce_delta if option_type == 'CE' else abs(pe_delta)

    if not (DELTA_MIN <= delta <= DELTA_MAX):
        rejects.append(f'Delta={delta:.3f} outside {DELTA_MIN}-{DELTA_MAX}')
        score -= 10
    else:
        score += 15
        reasons.append(f'Delta={delta:.3f} in range ✅ (+15)')

    if theta < THETA_MAX:
        rejects.append(f'Theta={theta:.2f} < {THETA_MAX} — too much decay')
        score -= 10
    else:
        score += 10
        reasons.append(f'Theta={theta:.2f} acceptable ✅ (+10)')

    if gamma < GAMMA_MIN:
        rejects.append(f'Gamma={gamma:.4f} < {GAMMA_MIN} — low sensitivity')
    else:
        score += 5
        reasons.append(f'Gamma={gamma:.4f} good ✅ (+5)')

    # ── 7. Options Chain Confluence ────────────────────────────────
    # PCR
    if pcr and pcr > 0:
        if direction == 'bullish' and pcr < 0.85:
            score += 10
            reasons.append(f'PCR={pcr:.2f} supports bullish ✅ (+10)')
        elif direction == 'bearish' and pcr > 1.2:
            score += 10
            reasons.append(f'PCR={pcr:.2f} supports bearish ✅ (+10)')
        else:
            rejects.append(f'PCR={pcr:.2f} neutral/against direction')

    # Max Pain
    if max_pain and spot:
        mp_diff_pct = abs(spot - max_pain) / spot * 100
        if mp_diff_pct < 0.3:
            # Very near max pain — market may pin here, avoid
            score -= 5
            rejects.append(f'Near max pain {max_pain:.0f} — pinning risk (-5)')
        else:
            score += 5
            reasons.append(f'Away from max pain {max_pain:.0f} ✅ (+5)')

    # OI Walls as targets
    if direction == 'bullish' and call_wall:
        target_dist = (call_wall - spot) / spot * 100
        if 0.3 < target_dist < 1.5:
            score += 10
            reasons.append(f'Call wall {call_wall:.0f} as target ({target_dist:.1f}% away) ✅ (+10)')
    elif direction == 'bearish' and put_wall:
        target_dist = (spot - put_wall) / spot * 100
        if 0.3 < target_dist < 1.5:
            score += 10
            reasons.append(f'Put wall {put_wall:.0f} as target ({target_dist:.1f}% away) ✅ (+10)')

    # ── 8. Momentum Indicators ─────────────────────────────────────
    if len(candles_5m) >= 15:
        closes = [c.get('close', 0) for c in candles_5m]
        rsi = _rsi(closes)
        vwap = _vwap(candles_5m)
        ema9 = _ema(closes, 9)
        ema21 = _ema(closes, 21)

        # RSI check
        if direction == 'bearish' and RSI_PE_MIN <= rsi <= RSI_PE_MAX:
            score += 15
            reasons.append(f'RSI={rsi:.1f} in bearish zone ✅ (+15)')
        elif direction == 'bullish' and RSI_CE_MIN <= rsi <= RSI_CE_MAX:
            score += 15
            reasons.append(f'RSI={rsi:.1f} in bullish zone ✅ (+15)')
        else:
            rejects.append(f'RSI={rsi:.1f} not aligned with {direction}')
            score -= 5

        # VWAP
        if direction == 'bullish' and spot > vwap:
            score += 8
            reasons.append(f'Price above VWAP {vwap:.0f} ✅ (+8)')
        elif direction == 'bearish' and spot < vwap:
            score += 8
            reasons.append(f'Price below VWAP {vwap:.0f} ✅ (+8)')
        else:
            rejects.append(f'Price vs VWAP not aligned')

        # EMA cross
        if direction == 'bullish' and ema9 > ema21:
            score += 5
            reasons.append(f'EMA9 > EMA21 bullish ✅ (+5)')
        elif direction == 'bearish' and ema9 < ema21:
            score += 5
            reasons.append(f'EMA9 < EMA21 bearish ✅ (+5)')

    # ── 9. IV Rank (bonus) ─────────────────────────────────────────
    if iv_rank is not None:
        if iv_rank < 30:
            score += 10
            reasons.append(f'IV Rank={iv_rank:.0f} low — cheap options ✅ (+10)')
        elif iv_rank > 70:
            score -= 10
            rejects.append(f'IV Rank={iv_rank:.0f} high — expensive options (-10)')

    # ── 10. Final Decision ─────────────────────────────────────────
    logger.info(
        "VixGreeks | %s | %s | score=%.0f | min=%.0f | vix=%.1f | dte=%d | delta=%.3f | reasons=%s | rejects=%s",
        symbol, direction, score, min_score, vix, dte, delta, reasons, rejects
    )

    if score >= min_score:
        signal = f'buy_{option_type.lower()}'
        # Dynamic SL/TP based on DTE
        sl_pct = SL_PCT + (dte * 2)   # More room closer to expiry
        tp_pct = sl_pct * 3            # 1:3 RR always
        rr = 3.0

        return VixGreeksSignal(
            signal=signal, score=round(score, 1), option_type=option_type,
            symbol=symbol, spot=spot, vix=vix, dte=dte,
            delta=round(delta, 3), theta=round(theta, 2), gamma=round(gamma, 4),
            sl_pct=round(sl_pct, 1), tp_pct=round(tp_pct, 1), rr=rr,
            reasons=reasons, reject_reasons=rejects
        )
    else:
        return VixGreeksSignal(
            signal='hold', score=round(score, 1), option_type=option_type,
            symbol=symbol, spot=spot, vix=vix, dte=dte,
            delta=round(delta, 3), theta=round(theta, 2), gamma=round(gamma, 4),
            sl_pct=SL_PCT, tp_pct=TP_PCT, rr=3.0,
            reasons=reasons,
            reject_reasons=rejects + [f'Score {score:.0f} < min {min_score}']
        )
