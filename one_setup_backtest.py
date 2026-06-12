#!/usr/bin/env python
"""
One Setup For Life – Backtest + Parameter Grid Search
=====================================================
Run: docker exec -e CELERY_WORKER_RUNNING=1 profitxai_backend-web-1 python -u /app/one_setup_backtest.py

NOTE on options P&L: results are in SPOT POINTS (underlying index move),
since historical option chain data is unavailable.  A rough premium
estimate uses delta ≈ 0.40: premium_pnl ≈ spot_pnl × 0.40.
"""

import os
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
os.environ["CELERY_WORKER_RUNNING"] = "1"

import django
django.setup()

import sys
import datetime as dt

import pandas as pd

from apps.strategies.models import Strategy
from apps.common.candle_service import fetch_candles_for_strategy
from apps.ict_engine.one_setup_strategy import OneSetupStrategy, OneSetupDirection

# ── Config ────────────────────────────────────────────────────────────────
OPTS_ID = "72143d8a-d97c-4ac9-93eb-073ff6b26d18"
PERP_ID = "0f08b58a-45da-4e2a-a461-a10c3d3201b6"

OPTIONS_SYMS = ["NIFTY", "BANKNIFTY", "SENSEX"]
CRYPTO_SYMS  = ["BTCUSD", "ETHUSD"]

# Grid: 3×3×2 = 18 combos per symbol → fast
GRID_SCORE = [40, 55, 70]
GRID_RR    = [1.5, 2.0, 2.5]
GRID_KZ    = [True, False]

# Scan every N 15m bars within a day (step=5 → every 75min for NSE)
SCAN_STEP_NSE    = 5
SCAN_STEP_CRYPTO = 8   # 24/7 days are long; scan every 2h

# ── Helpers ───────────────────────────────────────────────────────────────

def _to_df(candles):
    rows = []
    for c in candles:
        if hasattr(c, "open"):
            rows.append({"ts": c.timestamp,
                         "open": float(c.open), "high": float(c.high),
                         "low": float(c.low), "close": float(c.close),
                         "volume": float(c.volume)})
        else:
            rows.append({"ts": c.get("ts", 0),
                         "open": float(c.get("open", 0)), "high": float(c.get("high", 0)),
                         "low": float(c.get("low", 0)), "close": float(c.get("close", 0)),
                         "volume": float(c.get("volume", 0))})
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df.index = pd.to_datetime(df["ts"], unit="s", utc=True)
    return df.drop(columns=["ts"]).sort_index()


def _resample(df, rule):
    return df.resample(rule).agg(
        {"open": "first", "high": "max", "low": "min",
         "close": "last", "volume": "sum"}
    ).dropna()


def _dates(df):
    return sorted(set(df.index.date))


def _day_mask(df, date):
    return df[df.index.date == date]


# ── Single-combo backtest ─────────────────────────────────────────────────

def run_backtest(symbol, df_5m, df_15m, df_daily, is_nse, params):
    strat_obj = OneSetupStrategy(
        min_rr=params["min_rr"],
        min_score=params["min_score"],
        killzone_filter=params["killzone_filter"],
    )

    dates = _dates(df_15m)
    if len(dates) < 2:
        return []

    trades = []
    step = SCAN_STEP_NSE if is_nse else SCAN_STEP_CRYPTO

    for di in range(1, len(dates)):
        date_today = dates[di]
        date_prev  = dates[di - 1]

        today_15m = _day_mask(df_15m, date_today)
        today_5m  = _day_mask(df_5m,  date_today)

        if len(today_15m) < 4 or len(today_5m) < 10:
            continue

        # Build daily slice used to derive prior range.
        # _get_prior_range uses iloc[-2], so we need:
        #   iloc[-2] = date_prev full bar  (= prior range)
        #   iloc[-1] = date_today partial bar
        prev_daily_row = _day_mask(df_daily, date_prev)
        if prev_daily_row.empty:
            continue

        entered_today = False

        # Scan at every `step` 15m bars from bar index 4 onwards
        scan_indices = range(4, len(today_15m), step)
        if not scan_indices:
            scan_indices = [len(today_15m) - 1]

        for j in scan_indices:
            if entered_today:
                break

            ts_cutoff = today_15m.index[j]

            # Cumulative slices up to this bar (capped for speed)
            cum_15m = df_15m[df_15m.index <= ts_cutoff].iloc[-120:]
            cum_5m  = df_5m[df_5m.index <= ts_cutoff].iloc[-600:]

            # Daily slice: prev full day + today partial
            today_partial = cum_5m[cum_5m.index.date == date_today]
            if today_partial.empty:
                today_daily_row = prev_daily_row.copy()
            else:
                today_daily_row = _resample(today_partial, "D")
            df_daily_slice = pd.concat([prev_daily_row, today_daily_row]).iloc[-4:]

            try:
                sig = strat_obj.analyze(
                    symbol=symbol,
                    df_daily=df_daily_slice,
                    df_15m=cum_15m,
                    df_5m=cum_5m,
                )
            except Exception:
                continue

            if sig is None:
                continue

            # Signal found – simulate on remaining 5m bars of the day
            entry     = sig.entry_price
            sl        = sig.stop_loss
            tp        = sig.take_profit
            direction = sig.direction.value

            rem_5m = today_5m[today_5m.index > ts_cutoff]
            pnl         = 0.0
            exit_reason = "EOD"

            for _, row in rem_5m.iterrows():
                h = float(row["high"])
                l = float(row["low"])
                if direction == "long":
                    if l <= sl:
                        pnl = sl - entry;  exit_reason = "SL";  break
                    if h >= tp:
                        pnl = tp - entry;  exit_reason = "TP";  break
                else:
                    if h >= sl:
                        pnl = entry - sl;  exit_reason = "SL";  break
                    if l <= tp:
                        pnl = entry - tp;  exit_reason = "TP";  break
            else:
                if not rem_5m.empty:
                    last_c = float(rem_5m.iloc[-1]["close"])
                    pnl = (last_c - entry) if direction == "long" else (entry - last_c)

            trades.append({
                "date": str(date_today),
                "direction": direction,
                "sweep_type": sig.sweep_type,
                "zone_type": sig.entry_zone_type,
                "score": sig.confluence_score,
                "entry": round(entry, 2),
                "sl": round(sl, 2),
                "tp": round(tp, 2),
                "exit_reason": exit_reason,
                "pnl": round(pnl, 2),
                "win": pnl > 0,
            })
            entered_today = True

    return trades


def summarize(trades):
    if not trades:
        return {"n": 0, "wins": 0, "wr": 0.0, "tot_pnl": 0.0, "avg_pnl": 0.0}
    n = len(trades)
    wins = sum(1 for t in trades if t["win"])
    tot  = sum(t["pnl"] for t in trades)
    return {
        "n": n, "wins": wins,
        "wr": round(wins / n * 100, 1),
        "tot_pnl": round(tot, 2),
        "avg_pnl": round(tot / n, 2),
    }


# ── Main ─────────────────────────────────────────────────────────────────

print("=" * 68)
print("  One Setup For Life – Backtest + Grid Search")
print("=" * 68)

strat_opts = Strategy.objects.get(id=OPTS_ID)
strat_perp = Strategy.objects.get(id=PERP_ID)

all_best = {}     # symbol -> best params dict

for symbol in OPTIONS_SYMS + CRYPTO_SYMS:
    is_nse  = symbol in OPTIONS_SYMS
    strat   = strat_opts if is_nse else strat_perp

    print(f"\n{'─'*68}")
    print(f"  {symbol}  ({'NSE options – spot-point P&L' if is_nse else 'Crypto perp'})")
    print(f"{'─'*68}")
    sys.stdout.flush()

    raw    = fetch_candles_for_strategy(strat, symbol, "5", bars=2000) or []
    df_5m  = _to_df(raw)
    if df_5m.empty or len(df_5m) < 50:
        print(f"  ✗ No data, skipping")
        continue

    df_15m   = _resample(df_5m,  "15min")
    df_daily = _resample(df_5m,  "D")
    dates    = _dates(df_15m)

    print(f"  Data : {len(df_5m)} 5m bars | {len(df_15m)} 15m bars | {len(dates)} days")
    print(f"  Range: {dates[0]} → {dates[-1]}")
    sys.stdout.flush()

    # Build grid
    combos = [
        {"min_score": ms, "min_rr": rr, "killzone_filter": kz}
        for ms in GRID_SCORE for rr in GRID_RR for kz in GRID_KZ
    ]
    print(f"  Running {len(combos)} combos × {len(dates)-1} trading days ...")
    sys.stdout.flush()

    results = []
    for params in combos:
        trades = run_backtest(symbol, df_5m, df_15m, df_daily, is_nse, params)
        s      = summarize(trades)
        results.append({**params, **s, "trades": trades})

    total_trades = sum(r["n"] for r in results)
    qualified    = [r for r in results if r["n"] >= 3]

    print(f"\n  Total trades across all combos: {total_trades}")

    if not qualified:
        print(f"  ⚠  WARNING: no combo produced ≥3 trades.")
        print(f"     FVG/OB hard filter may be too strict for {len(dates)}-day window.")
        print(f"     Suggestion: run in production and collect more data before tuning.")
        # still show the best available
        qualified = sorted(results, key=lambda r: r["tot_pnl"], reverse=True)[:3]
        if all(r["n"] == 0 for r in qualified):
            print(f"     All combos returned 0 trades – skipping output.")
            continue

    # Top 5 by total PnL
    by_pnl = sorted(qualified, key=lambda r: r["tot_pnl"], reverse=True)[:5]
    by_wr  = sorted(qualified, key=lambda r: (r["wr"], r["tot_pnl"]), reverse=True)[:5]

    hdr = f"  {'score':>5} {'rr':>4} {'kz':>5} | {'n':>4} {'wr%':>6} {'tot_pnl':>9} {'avg_pnl':>8}"
    sep = "  " + "─" * 52

    print(f"\n  TOP 5 by total PnL (spot pts):")
    print(hdr); print(sep)
    for r in by_pnl:
        print(f"  {r['min_score']:>5} {r['min_rr']:>4} {str(r['killzone_filter']):>5} | "
              f"{r['n']:>4} {r['wr']:>6} {r['tot_pnl']:>9.1f} {r['avg_pnl']:>8.2f}")

    print(f"\n  TOP 5 by win rate (min 3 trades):")
    print(hdr); print(sep)
    for r in by_wr:
        print(f"  {r['min_score']:>5} {r['min_rr']:>4} {str(r['killzone_filter']):>5} | "
              f"{r['n']:>4} {r['wr']:>6} {r['tot_pnl']:>9.1f} {r['avg_pnl']:>8.2f}")

    # Sample trades from best combo
    if by_pnl:
        best = by_pnl[0]
        print(f"\n  Sample trades (best combo  score={best['min_score']} rr={best['min_rr']} kz={best['killzone_filter']}):")
        for t in best["trades"][:8]:
            tag = "✓" if t["win"] else "✗"
            print(f"    {tag} {t['date']}  {t['direction']:5s}  {t['sweep_type']:11s} "
                  f"zone={t['zone_type']:3s}  exit={t['exit_reason']:3s}  pnl={t['pnl']:+.1f}")

    # Store best for recommendations
    if by_pnl:
        all_best[symbol] = by_pnl[0]

    sys.stdout.flush()


# ── Recommendations ───────────────────────────────────────────────────────

print(f"\n{'='*68}")
print("  RECOMMENDATIONS")
print(f"{'='*68}")

options_best_params = None
crypto_best_params  = None

for symbol, best in all_best.items():
    is_nse = symbol in OPTIONS_SYMS
    est_premium_avg = round(best["avg_pnl"] * 0.40, 1) if is_nse else None
    print(f"\n  {symbol}:")
    print(f"    min_score={best['min_score']}  min_rr={best['min_rr']}  "
          f"killzone_filter={best['killzone_filter']}")
    print(f"    Trades={best['n']}  WinRate={best['wr']}%  "
          f"TotalPnL={best['tot_pnl']:.1f}pts  AvgPnL={best['avg_pnl']:.1f}pts")
    if est_premium_avg is not None:
        print(f"    ≈ Option premium avg ΔP/L ~ {est_premium_avg} Rs/lot (delta≈0.40 estimate)")
    if is_nse and options_best_params is None:
        options_best_params = {
            "min_rr": best["min_rr"],
            "min_score": best["min_score"],
            "killzone_filter": best["killzone_filter"],
        }
    if not is_nse and crypto_best_params is None:
        crypto_best_params = {
            "min_rr": best["min_rr"],
            "min_score": best["min_score"],
            "killzone_filter": best["killzone_filter"],
        }

# If we have best params, apply them
if options_best_params or crypto_best_params:
    print(f"\n{'─'*68}")
    print("  APPLYING RECOMMENDED PARAMS TO DB + TEMPLATE")
    print(f"{'─'*68}")

    if options_best_params:
        opts_strat = Strategy.objects.get(id=OPTS_ID)
        opts_strat.parameters = {**opts_strat.parameters, **options_best_params}
        opts_strat.save(update_fields=["parameters", "updated_at"])
        print(f"  Options strategy updated: {options_best_params}")

    if crypto_best_params:
        perp_strat = Strategy.objects.get(id=PERP_ID)
        perp_strat.parameters = {**perp_strat.parameters, **crypto_best_params}
        perp_strat.save(update_fields=["parameters", "updated_at"])
        print(f"  Crypto strategy updated:  {crypto_best_params}")

print(f"\n{'='*68}")
print("  DONE")
print(f"{'='*68}\n")
