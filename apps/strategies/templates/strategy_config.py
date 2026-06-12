# apps/strategies/templates/strategy_config.py

STRATEGY_TEMPLATES = {
    "multi_confirm_options": {
        "instrument_type": "options",
        "risk_config": {
            "trader_type": "buyer",
            "qty": 1,
            "atr_sl_mult": 1.0,
            "atr_tp_mult": 3.0,
        },
        "parameters": {"capital": 100000, "min_confidence": 65},
        "allowed_plans": ["elite", "pro"],
        "is_global": True,
    },
    "multi_confirm_crypto": {
        "instrument_type": "perp",
        "risk_config": {"qty": 1, "atr_sl_mult": 1.0, "atr_tp_mult": 3.0},
        "parameters": {"capital": 100000, "min_confidence": 65},
        "allowed_plans": ["elite", "pro"],
        "is_global": True,
    },
    "ict_mtf": {
        "instrument_type": "perp",
        "risk_config": {"qty": 1, "atr_sl_mult": 1.0, "atr_tp_mult": 3.0},
        "parameters": {"capital": 100000, "min_confluence": 62, "min_rr": 2.0, "risk_pct": 1.0},
        "allowed_plans": ["elite"],
        "is_global": True,
    },
    "ict_silver_bullet": {
        "instrument_type": "options",
        "risk_config": {"trader_type": "buyer", "qty": 1, "atr_sl_mult": 1.0, "atr_tp_mult": 3.0},
        "parameters": {"capital": 100000, "min_rr": 3.0},
        "allowed_plans": ["elite", "pro"],
        "is_global": True,
    },
    "ema_crossover": {
        "instrument_type": "equity",
        "risk_config": {"qty": 1, "sl_pct": 0.5, "target_pct": 1.5},
        "parameters": {"capital": 100000, "fast_ema": 9, "slow_ema": 21},
        "allowed_plans": ["pro", "elite", "free"],
        "is_global": False,
    },
    "ema_scalp": {
        "instrument_type": "equity",
        "risk_config": {"qty": 1, "sl_pct": 0.5, "target_pct": 1.5},
        "parameters": {"capital": 100000},
        "allowed_plans": ["pro", "elite"],
        "is_global": False,
    },
    "nse_option_seller": {
        "instrument_type": "options",
        "risk_config": {"trader_type": "seller", "qty": 1, "atr_sl_mult": 1.5, "atr_tp_mult": 1.0},
        "parameters": {"capital": 100000},
        "allowed_plans": ["elite"],
        "is_global": True,
    },
}


STRATEGY_TEMPLATES["confluence_options"] = {
    "instrument_type": "options",
    "risk_config": {
        "trader_type": "buyer",
        "qty": 1,
        "atr_sl_mult": 1.0,
        "atr_tp_mult": 3.0,
    },
    "parameters": {
        "capital": 100000,
        "min_confidence": 65,
        "min_sb_score": 65,
        "min_mc_score": 60,
        "rsi_bull_min": 52,
        "rsi_bear_max": 48,
    },
    "allowed_plans": ["elite"],
    "is_global": True,
}
STRATEGY_TEMPLATES["orb_gap_options"] = {
    "instrument_type": "options",
    "risk_config": {
        "trader_type": "buyer",
        "qty": 1,
        "max_pyramid_lots": 2,
    },
    "parameters": {
        "capital": 15000,
        "min_rr": 3.0,
        "atr_sl_mult": 1.0,
    },
    "allowed_plans": ["elite"],
    "is_global": True,
}
STRATEGY_TEMPLATES["hourly_macro_scalp"] = {
    "instrument_type": "perp",
    "risk_config": {
        "trader_type": "scalper",
        "qty": 1,
    },
    "parameters": {
        "min_rr": 2.0,
        "atr_sl_mult": 0.5,
        "min_score": 50.0,
        "zone_buffer_atr_mult": 0.25,
    },
    "allowed_plans": ["elite"],
    "is_global": True,
}


def apply_template(strategy):
    """Strategy save hone pe template se missing fields fill karo."""
    template = STRATEGY_TEMPLATES.get(strategy.algo_name)
    if not template:
        return strategy

    if not strategy.instrument_type:
        strategy.instrument_type = template["instrument_type"]

    if not strategy.risk_config:
        strategy.risk_config = {}
    for k, v in template["risk_config"].items():
        if k not in strategy.risk_config:
            strategy.risk_config[k] = v

    if not strategy.parameters:
        strategy.parameters = {}
    for k, v in template["parameters"].items():
        if k not in strategy.parameters:
            strategy.parameters[k] = v

    if not strategy.allowed_plans:
        strategy.allowed_plans = template.get("allowed_plans", [])

    return strategy
