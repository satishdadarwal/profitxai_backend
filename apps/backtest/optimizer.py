# apps/backtest/optimizer.py
#
# Strategy Parameter Optimizer
# Grid Search + Walk-Forward Validation
#
from __future__ import annotations
import itertools
import logging
import math
from typing import Any

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
#  Default param grids per strategy
# ─────────────────────────────────────────────────────────────
STRATEGY_PARAM_GRIDS = {
    "ict_mtf": {
        "min_confluence": [55, 65, 75],
        "min_rr":         [2.0, 3.0],
        "risk_pct":       [1.0, 1.5],
    },
    "ict_silver_bullet": {
        "min_confluence": [55, 65, 75],
        "min_rr":         [2.0, 3.0],
        "risk_pct":       [1.0, 1.5],
    },
    "multi_confirm_options": {
        "min_confidence": [60, 65, 70],
        "rr_ratio":       [1.5, 2.0, 2.5],
        "sl_pct":         [1.0, 1.5],
    },
    "multi_confirm_crypto": {
        "min_confidence": [60, 65, 70],
        "rr_ratio":       [2.0, 2.5, 3.0],
        "sl_pct":         [1.0, 1.5],
    },
    "ema_crossover": {
        "fast_ema": [5, 9, 12],
        "slow_ema": [21, 26, 34],
    },
    "default": {
        "rr_ratio":       [1.5, 2.0, 2.5],
        "sl_pct":         [1.0, 1.5],
        "min_confidence": [60, 65, 70],
    },
}


# ─────────────────────────────────────────────────────────────
#  Scoring Function
# ─────────────────────────────────────────────────────────────
def _score(results: dict, objective: str) -> float:
    """
    Backtest results ko ek score mein convert karo.
    Higher = better.
    """
    total    = results.get("total_trades", 0)
    net_pnl  = results.get("net_pnl", 0) or 0
    ret_pct  = results.get("total_return_pct", 0) or 0

    # Minimum trades check
    if total < 5:
        return -999.0

    # ✅ Negative PnL = immediately penalize
    if net_pnl <= 0:
        return round(ret_pct - 100, 4)  # Always negative

    sharpe   = results.get("sharpe_ratio", 0) or 0
    pf       = results.get("profit_factor", 0) or 0
    win_rate = results.get("win_rate", 0) or 0
    max_dd   = abs(results.get("max_drawdown", 100) or 100)

    # Clamp extremes
    sharpe   = max(min(sharpe, 5.0), -3.0)
    pf       = max(min(pf, 10.0), 0.0)
    win_rate = max(min(win_rate, 100.0), 0.0)
    max_dd   = max(min(max_dd, 100.0), 0.01)

    # DD penalty — exponential
    dd_penalty = math.exp(-max_dd / 20.0)

    if objective == "sharpe":
        return sharpe if net_pnl > 0 else sharpe - 10

    elif objective == "profit_factor":
        return pf if net_pnl > 0 else -pf

    elif objective == "min_drawdown":
        return dd_penalty * 100 if net_pnl > 0 else -dd_penalty * 100

    elif objective == "win_rate":
        return win_rate if net_pnl > 0 else -win_rate

    else:  # balanced
        score = (
            sharpe   * 30 +
            pf       * 25 +
            win_rate * 0.2 +
            dd_penalty * 25 +
            min(ret_pct, 30)  # Return bonus
        )
        return round(score, 4)


# ─────────────────────────────────────────────────────────────
#  Grid Generator
# ─────────────────────────────────────────────────────────────
def _auto_grid_from_algo(strategy_name: str) -> dict:
    """
    Strategy ke _DEFAULTS se auto grid generate karo.
    Numeric params ke liye 3 values: default*0.5, default, default*1.5
    """
    try:
        from apps.backtest.engine import get_algo
        algo = get_algo(strategy_name, {})
        defaults = getattr(algo, '_DEFAULTS', {}) or getattr(algo, 'params', {})
        grid = {}
        numeric_keys = ['min_confidence', 'min_confluence', 'rr_ratio',
                        'min_rr', 'sl_pct', 'risk_pct', 'fast_ema', 'slow_ema']
        for key in numeric_keys:
            if key in defaults:
                val = float(defaults[key])
                lo  = round(max(val * 0.7, 0.5), 2)
                hi  = round(val * 1.3, 2)
                mid = round(val, 2)
                grid[key] = sorted(set([lo, mid, hi]))
        return grid if grid else STRATEGY_PARAM_GRIDS["default"]
    except Exception:
        return STRATEGY_PARAM_GRIDS["default"]


def generate_grid(strategy_name: str, param_ranges: dict) -> list[dict]:
    """
    Param ranges se all combinations generate karo.
    param_ranges override karta hai default grid ko.
    Agar strategy STRATEGY_PARAM_GRIDS mein nahi hai toh auto-detect karo.
    """
    if strategy_name in STRATEGY_PARAM_GRIDS:
        base_grid = STRATEGY_PARAM_GRIDS[strategy_name].copy()
    else:
        # ✅ Auto-detect from strategy _DEFAULTS
        base_grid = _auto_grid_from_algo(strategy_name)

    # User override apply karo
    for key, val in param_ranges.items():
        if isinstance(val, list):
            base_grid[key] = val
        elif isinstance(val, dict):
            # {min, max, step} format
            mn  = val.get("min", 1)
            mx  = val.get("max", 5)
            stp = val.get("step", 0.5)
            vals = []
            v = mn
            while v <= mx + 1e-9:
                vals.append(round(v, 4))
                v += stp
            base_grid[key] = vals

    keys   = list(base_grid.keys())
    values = list(base_grid.values())
    combos = list(itertools.product(*values))

    return [dict(zip(keys, combo)) for combo in combos]


# ─────────────────────────────────────────────────────────────
#  Walk-Forward Split
# ─────────────────────────────────────────────────────────────
def walk_forward_split(candles: list, train_ratio: float = 0.7):
    """
    Candles ko train/test mein split karo.
    Returns: (train_candles, test_candles)
    """
    split = int(len(candles) * train_ratio)
    return candles[:split], candles[split:]


# ─────────────────────────────────────────────────────────────
#  Single Run
# ─────────────────────────────────────────────────────────────
# ICT strategies jo run_backtest_ict use karte hain
_ICT_STRATEGIES = {"ict_mtf", "ict_silver_bullet"}


def _candles_to_date_range(candles: list):
    """Candles se start/end date nikalo."""
    import datetime
    timestamps = []
    for c in candles:
        ts = c.timestamp if hasattr(c, "timestamp") else c.get("ts", 0)
        timestamps.append(ts)
    if not timestamps:
        return None, None
    mn = min(timestamps)
    mx = max(timestamps)
    start = datetime.datetime.utcfromtimestamp(mn).strftime("%Y-%m-%d")
    end   = datetime.datetime.utcfromtimestamp(mx).strftime("%Y-%m-%d")
    return start, end


def run_single(
    strategy_name: str,
    params: dict,
    candles: list,
    initial_capital: float = 100_000,
    fee_rate: float = 0.001,
    symbol: str = "UNKNOWN",
) -> dict:
    """
    Ek param combination pe backtest run karo.
    ICT strategies ke liye run_backtest_ict use karo.
    Returns results dict.
    """
    import pandas as pd

    if not candles:
        return {"error": "No candles", "total_trades": 0}

    # ── ICT strategies — dedicated runner ──────────────────
    if strategy_name in _ICT_STRATEGIES:
        try:
            from apps.strategies.ict_integration import run_backtest_ict

            start_date, end_date = _candles_to_date_range(candles)
            if not start_date:
                return {"error": "No date range", "total_trades": 0}

            class _FakeStrategy:
                def __init__(self):
                    self.symbol     = symbol
                    self.mode       = "paper"
                    self.user       = None
                    self.parameters = {**params, "capital": initial_capital}

            fake = _FakeStrategy()

            # Timeframe guess from candle spacing
            if len(candles) > 1:
                ts0 = candles[0].timestamp if hasattr(candles[0], "timestamp") else candles[0].get("ts", 0)
                ts1 = candles[1].timestamp if hasattr(candles[1], "timestamp") else candles[1].get("ts", 0)
                tf_min = max(1, (ts1 - ts0) // 60)
                tf_str = f"{tf_min}m"
            else:
                tf_str = "15m"

            results = run_backtest_ict(fake, start_date, end_date, timeframe=tf_str)
            return results

        except Exception as e:
            logger.warning("run_single ICT error | %s | %s | %s", strategy_name, params, e)
            return {"error": str(e), "total_trades": 0}

    # ── Generic engine ─────────────────────────────────────
    from apps.backtest.engine import BacktestEngine, get_algo

    try:
        algo = get_algo(strategy_name, params)
    except KeyError:
        return {"error": f"Strategy '{strategy_name}' not found", "total_trades": 0}

    try:
        df = pd.DataFrame([
            {
                "time":   c.timestamp if hasattr(c, "timestamp") else c.get("ts", 0),
                "open":   c.open if hasattr(c, "open") else c.get("open", 0),
                "high":   c.high if hasattr(c, "high") else c.get("high", 0),
                "low":    c.low if hasattr(c, "low") else c.get("low", 0),
                "close":  c.close if hasattr(c, "close") else c.get("close", 0),
                "volume": c.volume if hasattr(c, "volume") else c.get("volume", 0),
            }
            for c in candles
        ])
        df["time"] = pd.to_datetime(df["time"], unit="s")
        df = df.set_index("time").sort_index()

        engine = BacktestEngine(
            df=df,
            strategy=algo,
            initial_capital=initial_capital,
            fee_rate=fee_rate,
            symbol=symbol,
        )
        result = engine.run()
        return result.to_dict()

    except Exception as e:
        logger.warning("run_single error | strategy=%s | params=%s | %s", strategy_name, params, e)
        return {"error": str(e), "total_trades": 0}


# ─────────────────────────────────────────────────────────────
#  Main Optimizer
# ─────────────────────────────────────────────────────────────
def run_optimizer(
    optimizer_run_id: str,
    candles: list,
    strategy_name: str,
    param_ranges: dict,
    objective: str = "balanced",
    train_ratio: float = 0.7,
    initial_capital: float = 100_000,
    fee_rate: float = 0.001,
    symbol: str = "UNKNOWN",
    progress_callback=None,
) -> dict:
    """
    Full optimizer run.
    Returns: {best_params, best_score, best_train, best_test, all_results}
    """
    from apps.backtest.models import OptimizerRun

    grid = generate_grid(strategy_name, param_ranges)
    total = len(grid)

    logger.info("Optimizer start | strategy=%s | combinations=%d | objective=%s",
                strategy_name, total, objective)

    # DB update
    OptimizerRun.objects.filter(id=optimizer_run_id).update(
        total_combinations=total,
        status="running",
    )

    # Walk-forward split
    train_candles, test_candles = walk_forward_split(candles, train_ratio)

    if len(train_candles) < 50:
        return {"error": "Insufficient train data", "total_trades": 0}
    if len(test_candles) < 20:
        return {"error": "Insufficient test data", "total_trades": 0}

    logger.info("Train bars=%d | Test bars=%d", len(train_candles), len(test_candles))

    all_results = []
    best_score  = -999.0
    best_params = None
    best_train  = None
    best_test   = None

    for idx, params in enumerate(grid):
        # Train pe run karo
        train_result = run_single(
            strategy_name, params, train_candles,
            initial_capital, fee_rate, symbol
        )
        train_score = _score(train_result, objective)

        # Test pe validate karo (overfitting check)
        test_result = run_single(
            strategy_name, params, test_candles,
            initial_capital, fee_rate, symbol
        )
        test_score = _score(test_result, objective)

        # Combined score — test pe bhi achha hona chahiye
        combined = train_score * 0.5 + test_score * 0.5

        all_results.append({
            "params":       params,
            "train_score":  round(train_score, 3),
            "test_score":   round(test_score, 3),
            "combined":     round(combined, 3),
            "train_trades": train_result.get("total_trades", 0),
            "test_trades":  test_result.get("total_trades", 0),
            "train_pnl":    train_result.get("net_pnl", 0),
            "test_pnl":     test_result.get("net_pnl", 0),
            "train_winrate": train_result.get("win_rate", 0),
            "test_winrate": test_result.get("win_rate", 0),
            "train_sharpe": train_result.get("sharpe_ratio", 0),
            "test_sharpe":  test_result.get("sharpe_ratio", 0),
            "train_dd":     train_result.get("max_drawdown", 0),
            "test_dd":      test_result.get("max_drawdown", 0),
            "train_pf":     train_result.get("profit_factor", 0),
            "test_pf":      test_result.get("profit_factor", 0),
        })

        if combined > best_score:
            best_score  = combined
            best_params = params
            best_train  = train_result
            best_test   = test_result

        # Progress update har 10 combinations pe
        if idx % 10 == 0 or idx == total - 1:
            progress = int((idx + 1) / total * 100)
            OptimizerRun.objects.filter(id=optimizer_run_id).update(
                completed_combinations=idx + 1,
                progress=progress,
            )
            if progress_callback:
                progress_callback(progress)
            logger.debug("Optimizer progress %d/%d | best_score=%.3f", idx+1, total, best_score)

    # Sort by combined score
    all_results.sort(key=lambda x: x["combined"], reverse=True)
    top_results = all_results[:10]

    # Robustness check — train aur test dono positive?
    robust = (
        best_train and best_test and
        best_train.get("net_pnl", 0) > 0 and
        best_test.get("net_pnl", 0) > 0
    )

    return {
        "best_params":      best_params,
        "best_score":       round(best_score, 3),
        "robust":           robust,
        "total_combinations": total,
        "objective":        objective,
        "train_bars":       len(train_candles),
        "test_bars":        len(test_candles),
        "best_train":       {
            "trades":   best_train.get("total_trades", 0) if best_train else 0,
            "pnl":      best_train.get("net_pnl", 0) if best_train else 0,
            "win_rate": best_train.get("win_rate", 0) if best_train else 0,
            "sharpe":   best_train.get("sharpe_ratio", 0) if best_train else 0,
            "max_dd":   best_train.get("max_drawdown", 0) if best_train else 0,
            "pf":       best_train.get("profit_factor", 0) if best_train else 0,
        },
        "best_test": {
            "trades":   best_test.get("total_trades", 0) if best_test else 0,
            "pnl":      best_test.get("net_pnl", 0) if best_test else 0,
            "win_rate": best_test.get("win_rate", 0) if best_test else 0,
            "sharpe":   best_test.get("sharpe_ratio", 0) if best_test else 0,
            "max_dd":   best_test.get("max_drawdown", 0) if best_test else 0,
            "pf":       best_test.get("profit_factor", 0) if best_test else 0,
        },
        "top_results": top_results,
    }
