# apps/options/options_prediction.py
# Orchestrates: fetch chain → compute metrics → derive signal → save prediction

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def generate_options_prediction(symbol_name: str = "NIFTY", user=None) -> Optional[object]:
    """
    Full pipeline:
    1. Fetch option chain from nse_fetcher
    2. Compute PCR, Max Pain, OI Walls, IV Rank
    3. Derive signal
    4. Save OptionChainSnapshot + OptionsPrediction
    5. Return OptionsPrediction
    """
    from apps.options.models import OptionSymbol, OptionChainSnapshot, OptionsPrediction, IVHistory
    from apps.options.nse_fetcher import fetch_nse_option_chain
    from apps.options.signal_engine import (
        compute_pcr, compute_max_pain, find_oi_walls,
        compute_iv_rank, derive_signal
    )
    from datetime import date

    try:
        sym_obj = OptionSymbol.objects.get(name=symbol_name.upper(), is_active=True)
    except OptionSymbol.DoesNotExist:
        logger.error("OptionSymbol not found: %s", symbol_name)
        return None

    try:
        chain_result = fetch_nse_option_chain(symbol=symbol_name, user=user)
    except Exception as e:
        logger.error("Option chain fetch failed | %s | %s", symbol_name, e)
        return None

    spot = chain_result.get("spot")
    chain_data = chain_result.get("chain", [])
    expiries = chain_result.get("expiries", [])
    expiry_str = expiries[0] if expiries else str(date.today())

    try:
        from datetime import datetime
        expiry_date = datetime.strptime(expiry_str, "%d-%b-%Y").date()
    except Exception:
        expiry_date = date.today()

    pcr_data = compute_pcr(chain_data)
    max_pain = compute_max_pain(chain_data)
    oi_walls = find_oi_walls(chain_data)

    atm_strike = int(round(spot / sym_obj.strike_step) * sym_obj.strike_step)
    atm_row = next((r for r in chain_data if r["strike"] == atm_strike), None)
    atm_ce_iv = atm_row["CE"].get("iv") if atm_row else None
    atm_pe_iv = atm_row["PE"].get("iv") if atm_row else None

    vix = None
    try:
        from apps.predictions.models import GlobalCueSnapshot
        latest_cue = GlobalCueSnapshot.objects.first()
        if latest_cue:
            vix = latest_cue.vix_india
    except Exception:
        pass

    current_iv = atm_ce_iv or 0
    iv_rank = compute_iv_rank(sym_obj, current_iv) if current_iv else None

    if current_iv:
        IVHistory.objects.update_or_create(
            symbol=sym_obj,
            date=date.today(),
            defaults={"atm_iv": current_iv, "iv_rank": iv_rank},
        )

    snapshot = OptionChainSnapshot.objects.create(
        symbol=sym_obj,
        expiry=expiry_date,
        spot=spot,
        pcr_oi=pcr_data["pcr_oi"],
        pcr_volume=pcr_data["pcr_volume"],
        max_pain=max_pain,
        atm_strike=atm_strike,
        atm_ce_iv=atm_ce_iv,
        atm_pe_iv=atm_pe_iv,
        vix=vix,
        call_wall=oi_walls["call_wall"],
        put_wall=oi_walls["put_wall"],
        chain_data=chain_data,
    )

    snapshot._iv_rank = iv_rank
    signal = derive_signal(snapshot)

    straddle_cost = 0
    if atm_row:
        straddle_cost = (atm_row["CE"].get("ltp", 0) + atm_row["PE"].get("ltp", 0))
    range_low = round(spot - straddle_cost, 2)
    range_high = round(spot + straddle_cost, 2)

    prediction = OptionsPrediction.objects.create(
        symbol=sym_obj,
        snapshot=snapshot,
        expiry=expiry_date,
        direction=signal["direction"],
        confidence_pct=signal["confidence_pct"],
        signal_score=signal["signal_score"],
        expected_range_low=range_low,
        expected_range_high=range_high,
        max_pain=max_pain,
        call_wall=oi_walls["call_wall"],
        put_wall=oi_walls["put_wall"],
        breakeven_pts=round(straddle_cost, 2),
        up_prob=signal["up_prob"],
        flat_prob=signal["flat_prob"],
        down_prob=signal["down_prob"],
        suggested_strategy=signal["suggested_strategy"],
        strategy_legs=signal["strategy_legs"],
        pcr_oi=pcr_data["pcr_oi"],
        iv_rank=iv_rank,
        signal_factors=signal["signal_factors"],
    )

    logger.info(
        "Options prediction created | %s | %s | dir=%s | conf=%.0f%%",
        symbol_name, expiry_date, signal["direction"], signal["confidence_pct"],
    )
    return prediction
