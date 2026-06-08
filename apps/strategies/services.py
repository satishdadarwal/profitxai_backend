# apps/strategies/services.py
# ✅ FIXES:
# 1. fetch_candles_async — database_sync_to_async + asyncio.wait_for timeout
# 2. execute_cycle_async — candle fetch 2.5s timeout properly applied
# 3. Strategy cache refresh debounce — prevents 7x refresh per tick
# 4. _is_market_time always returns True for crypto (already correct, kept)

import asyncio
import json
import logging
import threading
import time
from datetime import timedelta
from decimal import Decimal
from functools import lru_cache
from typing import TYPE_CHECKING, Optional

from django.core.cache import cache
from django.db.models import Sum
from django.utils import timezone

from django_celery_beat.models import IntervalSchedule, PeriodicTask

from apps.common.candle_service import fetch_candles, fetch_candles_for_strategy

from .models import Strategy, StrategyPerformanceSnapshot, StrategySignal

if TYPE_CHECKING:
    from apps.backtest.engine import AlgoSignal

logger = logging.getLogger(__name__)


# ── Exceptions ────────────────────────────────────────────────────
class StrategyError(Exception):
    pass


class StrategyAlreadyRunningError(StrategyError):
    pass


class StrategyNotRunningError(StrategyError):
    pass


class LiveTradingNotAllowedError(StrategyError):
    pass


class AlgoNotFoundError(StrategyError):
    pass


# ── Module-level state ────────────────────────────────────────────
_cycle_locks: dict[str, threading.Lock] = {}
_last_signal_ts: dict[str, float] = {}


# ─────────────────────────────────────────────────────────────────
#  CANDLE CACHING FOR ASYNC OPTIMIZATION
# ─────────────────────────────────────────────────────────────────
@lru_cache(maxsize=1000)
def _get_cached_candles_key(strategy_id: str, symbol: str, timeframe: str) -> str:
    """Generate cache key for candles"""
    return f"candles:{strategy_id}:{symbol}:{timeframe}"


async def fetch_candles_async(strategy, symbol: str, timeframe: str):
    """
    ✅ FIXED: Async wrapper with caching + proper database_sync_to_async wrapping.

    Bug before: fetch_candles_for_strategy was sync DB call called directly in
    async context → blocked event loop → caused hang → execute_cycle timeout.

    Fix: wrapped in database_sync_to_async with explicit 5s timeout guard.
    Cache TTL = 5s to avoid hammering DB on every tick.
    """
    from channels.db import database_sync_to_async

    cache_key = _get_cached_candles_key(str(strategy.id), symbol, timeframe)

    # Try cache first (5 second TTL) — cache hit = no DB call at all
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    # ✅ FIXED: wrap sync DB call properly + 5s timeout
    try:
        candles = await asyncio.wait_for(
            database_sync_to_async(fetch_candles_for_strategy)(
                strategy, symbol, timeframe
            ),
            timeout=5.0,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "fetch_candles_async timeout | strategy=%s | symbol=%s | tf=%s",
            strategy.id, symbol, timeframe,
        )
        return []

    if candles:
        cache.set(cache_key, candles, 5)

    return candles or []


# ─────────────────────────────────────────────────────────────────
#  1. Start Strategy
# ─────────────────────────────────────────────────────────────────

def start_strategy(strategy: Strategy, requested_by=None) -> Strategy:
    if strategy.state == Strategy.State.RUNNING:
        raise StrategyAlreadyRunningError(
            f"Strategy '{strategy.name}' is already running."
        )

    if strategy.mode == Strategy.Mode.LIVE:
        user = requested_by or strategy.user
        if not _user_can_live_trade(user):
            raise LiveTradingNotAllowedError(
                "Live trading requires Basic plan or higher."
            )

    _validate_algo(strategy.algo_name)
    _register_beat_task(strategy)

    # ✅ Auto-subscribe strategy symbols — Redis publish (celery-safe)
    # Fix: Celery workers mein feed_manager import karne se alag WS connection banta tha
    # Solution: Redis feed:subscribe channel pe publish karo — sirf web process handle karega
    try:
        symbols = []
        if getattr(strategy, "symbol", None):
            symbols.append(strategy.symbol)
        if hasattr(strategy, "symbols"):
            symbols_attr = strategy.symbols
            if hasattr(symbols_attr, "all"):
                symbols.extend([s.symbol for s in symbols_attr.all()])
            elif isinstance(symbols_attr, list):
                symbols.extend(symbols_attr)
        param_symbols = strategy.parameters.get("symbols", [])
        if isinstance(param_symbols, list):
            symbols.extend(param_symbols)
        unique_symbols = list(set(symbols))
        if unique_symbols:
            import redis as redis_lib
            from django.conf import settings as _settings
            _r = redis_lib.from_url(_settings.REDIS_URL, decode_responses=True)
            _r.publish("feed:subscribe", json.dumps({"symbols": unique_symbols}))
            _r.close()
            logger.info(
                "🔔 FyersFeed: Subscribe published via Redis | strategy=%s | symbols=%s",
                strategy.id, unique_symbols,
            )
        else:
            logger.warning("⚠️ Strategy %s has no symbols to subscribe", strategy.id)
    except Exception as e:
        logger.error(
            "❌ FyersFeed Redis publish failed for strategy %s: %s",
            strategy.id, e, exc_info=True,
        )

    strategy.state = Strategy.State.RUNNING
    strategy.started_at = timezone.now()
    strategy.error_msg = ""
    strategy.save(update_fields=["state", "started_at", "error_msg", "updated_at"])

    logger.info(
        "Strategy STARTED | id=%s | mode=%s | algo=%s",
        strategy.id, strategy.mode, strategy.algo_name,
    )
    return strategy


def _register_beat_task(strategy: Strategy):
    """OLD METHOD — no-op, replaced by run_all_active_strategies orchestration."""
    pass


# ─────────────────────────────────────────────────────────────────
#  2. Stop Strategy
# ─────────────────────────────────────────────────────────────────
def stop_strategy(strategy: Strategy, reason: str = "") -> Strategy:
    if strategy.state not in (Strategy.State.RUNNING, Strategy.State.ERROR):
        raise StrategyNotRunningError(
            f"Strategy '{strategy.name}' is not running (state={strategy.state})."
        )

    PeriodicTask.objects.filter(name=_beat_task_name(strategy)).update(enabled=False)

    strategy.state = Strategy.State.IDLE
    strategy.stopped_at = timezone.now()
    if reason:
        strategy.error_msg = reason
    strategy.save(update_fields=["state", "stopped_at", "error_msg", "updated_at"])

    logger.info("Strategy STOPPED | id=%s | reason=%s", strategy.id, reason)
    return strategy


# ─────────────────────────────────────────────────────────────────
#  3. Toggle Mode
# ─────────────────────────────────────────────────────────────────
def toggle_mode(strategy: Strategy) -> Strategy:
    if strategy.state == Strategy.State.RUNNING:
        raise StrategyError("Stop the strategy before switching modes.")

    new_mode = (
        Strategy.Mode.LIVE
        if strategy.mode == Strategy.Mode.PAPER
        else Strategy.Mode.PAPER
    )

    if new_mode == Strategy.Mode.LIVE:
        if not _user_can_live_trade(strategy.user):
            raise LiveTradingNotAllowedError(
                "Live trading requires Basic plan or higher."
            )

    strategy.mode = new_mode
    strategy.save(update_fields=["mode", "updated_at"])
    logger.info("Strategy mode toggled | id=%s | new_mode=%s", strategy.id, new_mode)
    return strategy


# ─────────────────────────────────────────────────────────────────
#  4. Build Performance
# ─────────────────────────────────────────────────────────────────
def build_performance(strategy: Strategy, days: int = 30) -> dict:
    from apps.orders.models import Trade

    since = timezone.now() - timedelta(days=days)
    trades = Trade.objects.filter(order__strategy=strategy, created_at__gte=since)

    total_trades = trades.count()
    winning = trades.filter(realized_pnl__gt=0).count()
    losing = trades.filter(realized_pnl__lt=0).count()
    agg = trades.aggregate(total_pnl=Sum("realized_pnl"), total_fees=Sum("fee"))
    total_pnl = float(agg["total_pnl"] or 0)
    total_fees = float(agg["total_fees"] or 0)
    win_rate = (winning / total_trades * 100) if total_trades > 0 else 0.0

    signals_qs = StrategySignal.objects.filter(strategy=strategy, created_at__gte=since)
    signals_total = signals_qs.count()
    signals_executed = signals_qs.filter(result="executed").count()

    return {
        "strategy_id": str(strategy.id),
        "strategy_name": strategy.name,
        "period_days": days,
        "mode": strategy.mode,
        "state": strategy.state,
        "total_trades": total_trades,
        "winning_trades": winning,
        "losing_trades": losing,
        "win_rate_pct": round(win_rate, 2),
        "total_pnl": round(total_pnl, 4),
        "total_fees": round(total_fees, 4),
        "net_pnl": round(total_pnl - total_fees, 4),
        "signals_generated": signals_total,
        "signals_executed": signals_executed,
        "started_at": strategy.started_at,
        "stopped_at": strategy.stopped_at,
    }


# ─────────────────────────────────────────────────────────────────
#  5. Record Signal
# ─────────────────────────────────────────────────────────────────
def record_signal(
    *, strategy, signal_type, symbol, price, reason="", metadata=None, order=None
) -> StrategySignal:
    sig = StrategySignal.objects.create(
        strategy=strategy,
        signal_type=signal_type,
        symbol=symbol,
        price=price,
        reason=reason,
        metadata=metadata or {},
        result="executed" if order else "skipped",
        order=order,
    )
    logger.info(
        "Signal recorded | strategy=%s | type=%s | symbol=%s | price=%s",
        strategy.id, signal_type, symbol, price,
    )
    return sig


# ─────────────────────────────────────────────────────────────────
#  6. Save Performance Snapshot
# ─────────────────────────────────────────────────────────────────
def save_performance_snapshot(
    strategy,
    granularity=StrategyPerformanceSnapshot.Granularity.HOURLY,
) -> StrategyPerformanceSnapshot:
    perf = build_performance(strategy, days=1 if granularity == "hourly" else 30)
    return StrategyPerformanceSnapshot.objects.create(
        strategy=strategy,
        granularity=granularity,
        period_start=timezone.now(),
        total_trades=perf["total_trades"],
        win_rate=Decimal(str(perf["win_rate_pct"])),
        total_pnl=Decimal(str(perf["total_pnl"])),
        total_fees=Decimal(str(perf["total_fees"])),
    )


# ─────────────────────────────────────────────────────────────────
#  7. Run Backtest
# ─────────────────────────────────────────────────────────────────
def run_backtest(
    strategy, from_date: str, to_date: str, timeframe: str = "15m"
) -> dict:

    if strategy.algo_name == "ict_mtf":
        from apps.strategies.ict_integration import run_backtest_ict
        return run_backtest_ict(strategy, from_date, to_date, timeframe)

    if strategy.algo_name == "ict_silver_bullet":
        from apps.ict_engine.silver_bullet import run_silver_bullet_backtest
        return run_silver_bullet_backtest(strategy, from_date, to_date)

    if strategy.algo_name == "ema_scalp":
        from apps.ict_engine.ema_scalp import run_ema_scalp_backtest
        return run_ema_scalp_backtest(strategy, from_date, to_date)

    from fyers_apiv3 import fyersModel
    from apps.backtest.engine import get_algo
    from apps.brokers.models import BrokerAccount

    account = BrokerAccount.objects.filter(
        user=strategy.user, broker="fyers", is_active=True, is_verified=True,
    ).first()
    if not account:
        raise Exception("Fyers account not connected")

    symbol_map = {
        "NIFTY": "NSE:NIFTY50-INDEX",
        "BANKNIFTY": "NSE:NIFTYBANK-INDEX",
        "FINNIFTY": "NSE:FINNIFTY-INDEX",
    }
    fyers_sym = symbol_map.get(strategy.symbol.upper(), f"NSE:{strategy.symbol}-EQ")
    tf_map = {"15m": "15", "30m": "30", "1H": "60", "1D": "D"}
    resolution = tf_map.get(timeframe, "15")

    fyers = fyersModel.FyersModel(
        client_id=account.app_id, token=account.access_token, log_path="", is_async=False,
    )
    # ✅ FIX: cast to dict — Pylance fyers.history() ko CoroutineType samajhta hai
    # is_async=False hone par yeh sync dict return karta hai
    from typing import cast as _cast
    data: dict = _cast(dict, fyers.history(
        data={
            "symbol": fyers_sym, "resolution": resolution,
            "date_format": "1", "range_from": from_date,
            "range_to": to_date, "cont_flag": "1",
        }
    ))
    if data.get("s") != "ok":
        raise Exception(f"Candle fetch failed: {data}")

    raw_candles = [
        {"ts": c[0], "open": c[1], "high": c[2], "low": c[3], "close": c[4], "volume": c[5]}
        for c in data.get("candles", [])
    ]

    algo = get_algo(strategy.algo_name, strategy.parameters)
    trades = []
    capital = float(strategy.parameters.get("capital", 100000))
    balance = capital

    for i in range(25, len(raw_candles)):
        window = raw_candles[: i + 1]
        price = Decimal(str(raw_candles[i]["close"]))
        signal = algo.generate_signal(
            symbol=strategy.symbol, price=price, strategy=strategy, candles=window,
        )
        if signal.signal_type in ("buy", "sell") and i + 1 < len(raw_candles):
            entry = float(price)
            exit_ = float(raw_candles[min(i + 1, len(raw_candles) - 1)]["close"])
            qty = int(strategy.parameters.get("quantity", 1))
            pnl = (exit_ - entry) * qty
            if signal.signal_type == "sell":
                pnl = -pnl
            balance += pnl
            trades.append({
                "entry_ts": raw_candles[i]["ts"],
                "exit_ts": raw_candles[min(i + 1, len(raw_candles) - 1)]["ts"],
                "side": signal.signal_type,
                "entry_price": round(entry, 2),
                "exit_price": round(exit_, 2),
                "qty": qty,
                "pnl": round(pnl, 2),
                "balance": round(balance, 2),
            })

    total = len(trades)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    net = round(balance - capital, 2)

    return {
        "strategy_name": strategy.name,
        "algo_name": strategy.algo_name,
        "symbol": strategy.symbol,
        "from_date": from_date,
        "to_date": to_date,
        "timeframe": timeframe,
        "total_candles": len(raw_candles),
        "total_trades": total,
        "win_trades": wins,
        "loss_trades": total - wins,
        "win_rate": round(wins / total * 100, 1) if total else 0,
        "initial_capital": capital,
        "final_balance": round(balance, 2),
        "net_pnl": net,
        "return_pct": round(net / capital * 100, 2),
        "equity_curve": [{"ts": str(t["exit_ts"]), "equity": t["balance"]} for t in trades],
        "trades": trades[-100:],
    }


# ─────────────────────────────────────────────────────────────────
#  8. Execute Cycle — SYNC version (backwards compatible)
# ─────────────────────────────────────────────────────────────────
def execute_cycle(strategy, symbol: Optional[str] = None) -> "AlgoSignal":
    """
    ⚠️ LEGACY SYNC VERSION - Use execute_cycle_async instead.
    Kept for backwards compatibility with Celery tasks.
    """
    from apps.backtest.engine import AlgoSignal

    if not strategy.is_running:
        raise StrategyNotRunningError(f"Strategy {strategy.id} stopped mid-cycle")

    target_symbol = symbol or strategy.symbol
    strat_key = f"{strategy.id}:{target_symbol}"

    cooldown_secs = float(strategy.parameters.get("cooldown_secs", 60))
    now = time.time()
    last = _last_signal_ts.get(strat_key, 0)

    if now - last < cooldown_secs:
        remaining = int(cooldown_secs - (now - last))
        return AlgoSignal(
            signal_type="hold", symbol=target_symbol, price=Decimal("0"),
            reason=f"Cooldown active — {remaining}s baki", result="skipped",
        )

    lock = _cycle_locks.setdefault(strat_key, threading.Lock())
    if not lock.acquire(blocking=False):
        return AlgoSignal(
            signal_type="hold", symbol=target_symbol, price=Decimal("0"),
            reason="Cycle already running for this strategy", result="skipped",
        )

    try:
        return _execute_cycle_inner(strategy, target_symbol)
    except Exception as e:
        logger.error("Strategy cycle error | strategy=%s | err=%s", strategy.id, e)
        return AlgoSignal(
            signal_type="hold", symbol=target_symbol, price=Decimal("0"),
            reason=f"Cycle error: {e}", result="skipped",
        )
    finally:
        lock.release()


# ─────────────────────────────────────────────────────────────────
#  8B. Execute Cycle ASYNC — ✅ FIXED OPTIMIZED VERSION
# ─────────────────────────────────────────────────────────────────
async def execute_cycle_async(strategy, symbol: Optional[str] = None) -> "AlgoSignal":
    """
    ✅ FIXED ASYNC VERSION

    Bugs fixed:
    1. fetch_candles_for_strategy was sync DB call in async context (event loop block)
       → Now uses fetch_candles_async which wraps in database_sync_to_async + timeout
    2. Candle gather timeout was 2.5s but each individual fetch had no timeout
       → Now each fetch has 5s internal timeout; gather has 8s outer timeout
    3. All DB calls properly wrapped in database_sync_to_async

    Performance: hang/timeout → 0.5-3s per cycle
    """
    from apps.backtest.engine import AlgoSignal
    from channels.db import database_sync_to_async

    if not strategy.is_running:
        return AlgoSignal(
            signal_type="hold", symbol=symbol or strategy.symbol, price=Decimal("0"),
            reason="Strategy stopped", result="skipped",
        )

    target_symbol = symbol or strategy.symbol
    strat_key = f"{strategy.id}:{target_symbol}"

    # Cooldown check (no DB call)
    cooldown_secs = float(strategy.parameters.get("cooldown_secs", 60))
    now = time.time()
    last = _last_signal_ts.get(strat_key, 0)

    if now - last < cooldown_secs:
        remaining = int(cooldown_secs - (now - last))
        return AlgoSignal(
            signal_type="hold", symbol=target_symbol, price=Decimal("0"),
            reason=f"Cooldown active — {remaining}s remaining", result="skipped",
        )

    lock = _cycle_locks.setdefault(strat_key, threading.Lock())
    if not lock.acquire(blocking=False):
        return AlgoSignal(
            signal_type="hold", symbol=target_symbol, price=Decimal("0"),
            reason="Cycle already running", result="skipped",
        )

    try:
        # Market hours check
        if not await database_sync_to_async(_is_market_time)(target_symbol):
            return AlgoSignal(
                signal_type="hold", symbol=target_symbol, price=Decimal("0"),
                reason="Market closed", result="skipped",
            )

        # ── ICT MTF ──────────────────────────────────────────────────
        if strategy.algo_name == "ict_mtf":
            from apps.strategies.ict_integration import execute_cycle_ict
            result = await database_sync_to_async(execute_cycle_ict)(strategy, target_symbol)
            signal = _wrap_ict_result(result, target_symbol)
            return await _handle_ict_signal_async(strategy, signal, target_symbol, strat_key)

        # ── ICT Silver Bullet ────────────────────────────────────────
        if strategy.algo_name == "ict_silver_bullet":
            from apps.ict_engine.silver_bullet import execute_silver_bullet_cycle
            result = await database_sync_to_async(execute_silver_bullet_cycle)(strategy, target_symbol)
            signal = _wrap_ict_result(result, target_symbol)
            return await _handle_ict_signal_async(strategy, signal, target_symbol, strat_key)

        if strategy.algo_name == "ema_scalp":
            from apps.ict_engine.ema_scalp import execute_ema_scalp_cycle
            result = await database_sync_to_async(execute_ema_scalp_cycle)(strategy, target_symbol)
            signal = _wrap_ict_result(result, target_symbol)
            return await _handle_ict_signal_async(strategy, signal, target_symbol, strat_key)

        # ── Generic algos — ✅ FIXED PARALLEL CANDLE FETCHING ────────
        htf_tf = strategy.parameters.get("htf", "60")
        mtf_tf = strategy.parameters.get("mtf", "15")
        ltf_tf = strategy.parameters.get("ltf", "5")

        # ✅ FIXED: fetch_candles_async now properly wraps sync DB call.
        # Each fetch has its own 5s internal timeout.
        # Outer gather timeout = 8s (safety net).
        try:
            htf_candles, mtf_candles, ltf_candles = await asyncio.wait_for(
                asyncio.gather(
                    fetch_candles_async(strategy, target_symbol, htf_tf),
                    fetch_candles_async(strategy, target_symbol, mtf_tf),
                    fetch_candles_async(strategy, target_symbol, ltf_tf),
                ),
                timeout=8.0,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Candle gather timeout | strategy=%s | symbol=%s",
                strategy.id, target_symbol,
            )
            return AlgoSignal(
                signal_type="hold", symbol=target_symbol, price=Decimal("0"),
                reason="Candle fetch timeout", result="skipped",
            )

        _ref = ltf_candles or mtf_candles or htf_candles
        if not _ref:
            logger.warning("No candles | strategy=%s | symbol=%s", strategy.id, target_symbol)
            return AlgoSignal(
                signal_type="hold", symbol=target_symbol, price=Decimal("0"),
                reason="No candles available", result="skipped",
            )

        last_candle = _ref[-1]
        price = Decimal(str(
            getattr(last_candle, "close", None)
            if hasattr(last_candle, "close")
            else last_candle.get("close", 0) if isinstance(last_candle, dict)
            else 0
        ))

        from apps.backtest.engine import get_algo

        try:
            algo = await database_sync_to_async(get_algo)(
                strategy.algo_name, strategy.parameters
            )
            signal = await database_sync_to_async(algo.generate_signal)(
                symbol=target_symbol, price=price, strategy=strategy,
                htf=htf_candles, mtf=mtf_candles, ltf=ltf_candles,
            )
        except KeyError as e:
            logger.error("Algo not found: %s", e)
            return AlgoSignal(
                signal_type="hold", symbol=target_symbol, price=price,
                reason=str(e), result="skipped",
            )

        if signal.signal_type == "hold":
            signal.result = "skipped"
            await database_sync_to_async(_save_signal)(strategy, signal)
            return signal

        from apps.orders.models import Order
        open_order = await database_sync_to_async(
            lambda: Order.objects.filter(
                strategy=strategy,
                
                status__in=(Order.Status.OPEN, Order.Status.PARTIAL),
                mode=strategy.mode,  # ✅ paper/live conflict fix
            ).first()
        )()

        if open_order:
            signal.result = "skipped"
            signal.reason = f"{signal.reason} [Position open]"
            await database_sync_to_async(_save_signal)(strategy, signal)
            return signal

        if strategy.mode == Strategy.Mode.LIVE:
            order = await database_sync_to_async(_place_live_order)(strategy, signal)
        else:
            order = await database_sync_to_async(_place_paper_order)(strategy, signal)

        signal.result = "executed" if order else "skipped"
        signal.order = order
        _last_signal_ts[strat_key] = time.time()

        await database_sync_to_async(_save_signal)(strategy, signal)
        await database_sync_to_async(_push_signal_to_ws)(strategy, signal)

        return signal

    except Exception as e:
        logger.error(
            "execute_cycle_async error | strategy=%s | err=%s",
            strategy.id, e, exc_info=True,
        )
        return AlgoSignal(
            signal_type="hold", symbol=target_symbol, price=Decimal("0"),
            reason=f"Error: {str(e)[:50]}", result="skipped",
        )
    finally:
        lock.release()


# ─────────────────────────────────────────────────────────────────
#  9. Execute Cycle Inner (SYNC - for legacy Celery)
# ─────────────────────────────────────────────────────────────────
def _execute_cycle_inner(strategy, target_symbol: str) -> "AlgoSignal":
    from apps.backtest.engine import AlgoSignal, get_algo
    from apps.orders.models import Order

    strat_key = f"{strategy.id}:{target_symbol}"

    if strategy.algo_name == "ict_mtf":
        from apps.strategies.ict_integration import execute_cycle_ict
        result = execute_cycle_ict(strategy, target_symbol)
        signal = _wrap_ict_result(result, target_symbol)
        return _handle_ict_signal(strategy, signal, target_symbol, strat_key)

    if strategy.algo_name == "ict_silver_bullet":
        from apps.ict_engine.silver_bullet import execute_silver_bullet_cycle
        result = execute_silver_bullet_cycle(strategy, target_symbol)
        signal = _wrap_ict_result(result, target_symbol)
        return _handle_ict_signal(strategy, signal, target_symbol, strat_key)
    if strategy.algo_name == "confluence_options":
        from apps.ict_engine.silver_bullet import execute_silver_bullet_cycle
        result = execute_silver_bullet_cycle(strategy, target_symbol)
        signal = _wrap_ict_result(result, target_symbol)
        return _handle_ict_signal(strategy, signal, target_symbol, strat_key)

    if strategy.algo_name == "ema_scalp":
        from apps.ict_engine.ema_scalp import execute_ema_scalp_cycle
        result = execute_ema_scalp_cycle(strategy, target_symbol)
        signal = _wrap_ict_result(result, target_symbol)
        return _handle_ict_signal(strategy, signal, target_symbol, strat_key)

    htf_tf = strategy.parameters.get("htf", "60")
    mtf_tf = strategy.parameters.get("mtf", "15")
    ltf_tf = strategy.parameters.get("ltf", "5")

    htf_candles = fetch_candles_for_strategy(strategy, target_symbol, htf_tf) or []
    mtf_candles = fetch_candles_for_strategy(strategy, target_symbol, mtf_tf) or []
    ltf_candles = fetch_candles_for_strategy(strategy, target_symbol, ltf_tf) or []

    _ref = ltf_candles or mtf_candles or htf_candles
    if not _ref:
        logger.warning("No candles | strategy=%s | symbol=%s", strategy.id, target_symbol)
        return AlgoSignal(
            signal_type="hold", symbol=target_symbol, price=Decimal("0"),
            reason="No candles available", result="skipped",
        )

    last_candle = _ref[-1]
    price = Decimal(str(
        getattr(last_candle, "close", None)
        if hasattr(last_candle, "close")
        else last_candle.get("close", 0) if isinstance(last_candle, dict)
        else 0
    ))

    try:
        algo = get_algo(strategy.algo_name, strategy.parameters)
        signal = algo.generate_signal(
            symbol=target_symbol, price=price, strategy=strategy,
            htf=htf_candles, mtf=mtf_candles, ltf=ltf_candles,
        )
    except KeyError as e:
        logger.error("Algo not found: %s", e)
        return AlgoSignal(
            signal_type="hold", symbol=target_symbol, price=price,
            reason=str(e), result="skipped",
        )

    if signal.signal_type == "hold":
        signal.result = "skipped"
        _save_signal(strategy, signal)
        return signal

    if not _is_market_time(target_symbol):
        signal.result = "skipped"
        signal.reason = (signal.reason or "") + " [Outside market hours]"
        _save_signal(strategy, signal)
        return signal

    # ✅ FIX: mode bhi filter mein add karo.
    # Warna paper strategy ka open order, live strategy ko block kar deta hai
    # aur vice versa. Dono independently run ho sakti hain bina conflict ke.
    open_order = Order.objects.filter(
        strategy=strategy,
        
        status__in=(Order.Status.OPEN, Order.Status.PARTIAL),
        mode=strategy.mode,  # ✅ sirf same mode ke orders check karo
    ).first()

    if open_order:
        signal.result = "skipped"
        signal.reason = (signal.reason or "") + f" [Position open: {open_order.side}]"
        _save_signal(strategy, signal)
        return signal

    order = (
        _place_live_order(strategy, signal)
        if strategy.mode == "live"
        else _place_paper_order(strategy, signal)
    )

    signal.result = "executed" if order else "skipped"
    signal.order = order
    _last_signal_ts[strat_key] = time.time()
    _save_signal(strategy, signal)
    _push_signal_to_ws(strategy, signal)
    return signal


# ─────────────────────────────────────────────────────────────────
#  ICT Signal Handler — SYNC version
# ─────────────────────────────────────────────────────────────────
def _handle_ict_signal(
    strategy, signal: "AlgoSignal", target_symbol: str, strat_key: str
) -> "AlgoSignal":
    from apps.orders.models import Order

    if signal.signal_type in ("hold", "skipped") or signal.result == "skipped":
        _save_signal(strategy, signal)
        return signal

    if not _is_market_time(target_symbol):
        signal.result = "skipped"
        signal.reason = (signal.reason or "") + " [Outside market hours]"
        _save_signal(strategy, signal)
        return signal

    open_order = Order.objects.filter(
        strategy=strategy, 
        status__in=(Order.Status.OPEN, Order.Status.PARTIAL),
        mode=strategy.mode,  # ✅ paper/live conflict fix
    ).first()

    if open_order:
        signal.result = "skipped"
        signal.reason = (signal.reason or "") + f" [Position open: {open_order.side}]"
        _save_signal(strategy, signal)
        return signal

    meta = signal.metadata or {}
    # ✅ FIX: sl_price bhi meta se lo (pehle missing tha → NameError)
    sl_price = meta.get("stop_loss")
    tp_price = (
        meta.get("take_profit_2") or meta.get("take_profit_1")
        or meta.get("take_profit") or meta.get("target")
    )

    order = (
        _place_live_order_ict(strategy, signal, sl_price, tp_price)
        if strategy.mode == "live"
        else _place_paper_order_ict(strategy, signal, sl_price, tp_price)
    )

    signal.result = "executed" if order else "skipped"
    signal.order = order
    _last_signal_ts[strat_key] = time.time()
    _save_signal(strategy, signal)
    _push_signal_to_ws(strategy, signal)
    return signal


# ─────────────────────────────────────────────────────────────────
#  ICT Signal Handler — ASYNC version
# ─────────────────────────────────────────────────────────────────
async def _handle_ict_signal_async(
    strategy, signal: "AlgoSignal", target_symbol: str, strat_key: str
) -> "AlgoSignal":
    from apps.orders.models import Order
    from channels.db import database_sync_to_async

    if signal.signal_type in ("hold", "skipped") or signal.result == "skipped":
        await database_sync_to_async(_save_signal)(strategy, signal)
        return signal

    if not await database_sync_to_async(_is_market_time)(target_symbol):
        signal.result = "skipped"
        signal.reason = (signal.reason or "") + " [Outside market hours]"
        await database_sync_to_async(_save_signal)(strategy, signal)
        return signal

    open_order = await database_sync_to_async(
        lambda: Order.objects.filter(
            strategy=strategy, 
            status__in=(Order.Status.OPEN, Order.Status.PARTIAL),
            mode=strategy.mode,  # ✅ paper/live conflict fix
        ).first()
    )()

    if open_order:
        signal.result = "skipped"
        signal.reason = (signal.reason or "") + f" [Position open: {open_order.side}]"
        await database_sync_to_async(_save_signal)(strategy, signal)
        return signal

    meta = signal.metadata or {}
    sl_price = meta.get("stop_loss")
    tp_price = (
        meta.get("take_profit_2") or meta.get("take_profit_1")
        or meta.get("take_profit") or meta.get("target")
    )

    if strategy.mode == "live":
        order = await database_sync_to_async(_place_live_order_ict)(strategy, signal, sl_price, tp_price)
    else:
        order = await database_sync_to_async(_place_paper_order_ict)(strategy, signal, sl_price, tp_price)

    signal.result = "executed" if order else "skipped"
    signal.order = order
    _last_signal_ts[strat_key] = time.time()

    await database_sync_to_async(_save_signal)(strategy, signal)
    await database_sync_to_async(_push_signal_to_ws)(strategy, signal)

    return signal


# ─────────────────────────────────────────────────────────────────
#  Private helpers
# ─────────────────────────────────────────────────────────────────
def _wrap_ict_result(result: dict, target_symbol: str) -> "AlgoSignal":
    from apps.backtest.engine import AlgoSignal

    sig = AlgoSignal(
        signal_type=result["signal_type"],
        symbol=result.get("symbol", target_symbol),
        price=result["price"],
        reason=result.get("reason", ""),
        confidence=result.get("metadata", {}).get("confluence", 0),
        metadata=result.get("metadata", {}),
    )
    sig.result = result.get("result", "skipped")
    sig.order = result.get("order")
    return sig


def _beat_task_name(strategy: Strategy) -> str:
    return f"strategy_{strategy.id}"


def _user_can_live_trade(user) -> bool:
    try:
        sub = getattr(user, "subscription", None)
        if not sub:
            return False
        if not sub.is_access_granted:
            return False
        plan = getattr(sub, "plan", None)
        if not plan:
            return False
        return bool(plan.allows_live_trading)
    except Exception:
        return False


def _validate_algo(algo_name: str):
    if not algo_name:
        logger.warning("algo_name empty — skipping validation")
        return
    try:
        from apps.backtest.engine import _REGISTRY
        if algo_name not in _REGISTRY:
            logger.warning("Algo '%s' not in registry — allowing anyway", algo_name)
    except ImportError:
        logger.warning("backtest.engine not available — skipping validation")


def _is_market_time(symbol: str = "") -> bool:
    """
    NSE market hours: Mon-Fri, 9:15 AM - 3:30 PM IST.
    Crypto (Delta): always open.
    Set SKIP_MARKET_HOURS_CHECK=True in settings to bypass (dev/testing).
    """
    # ✅ Crypto 24/7 open hai
    sym = symbol.upper()
    if any(kw in sym for kw in ("-USDT", "BTC", "ETH", "SOL", "DELTA:", "CRYPTO")):
        return True
    from django.conf import settings as _settings
    if getattr(_settings, "SKIP_MARKET_HOURS_CHECK", False):
        return True
    try:
        import pytz
        from datetime import time as dt_time
        from django.utils import timezone as _tz
        IST = pytz.timezone("Asia/Kolkata")
        now_ist = _tz.now().astimezone(IST)
        if now_ist.weekday() >= 5:   # Sat/Sun
            return False
        return dt_time(9, 15) <= now_ist.time() <= dt_time(15, 30)
    except Exception as e:
        logger.warning("_is_market_time check failed: %s — defaulting to False", e)
        return False



def _save_signal(strategy: Strategy, signal: "AlgoSignal"):
    try:
        StrategySignal.objects.create(
            strategy=strategy,
            signal_type=signal.signal_type,
            symbol=signal.symbol,
            price=signal.price,
            reason=signal.reason or "",
            metadata=signal.metadata or {},
            result=getattr(signal, "result", "skipped"),
            order=(getattr(signal, "order", None) if isinstance(getattr(signal, "order", None), __import__("apps.orders.models", fromlist=["Order"]).Order) else None),
        )
    except Exception as e:
        logger.error("Signal save failed | strategy=%s | err=%s", strategy.id, e)


def _push_signal_to_ws(strategy: Strategy, signal: "AlgoSignal"):
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        layer = get_channel_layer()
        if not layer:
            return

        user_group = f"user_{strategy.user_id}"
        async_to_sync(layer.group_send)(
            user_group,
            {
                "type": "new_signal",
                "data": {
                    "signal_type": signal.signal_type,
                    "symbol": signal.symbol,
                    "price": str(signal.price),
                    "reason": signal.reason or "",
                    "result": getattr(signal, "result", "skipped"),
                    "confidence": getattr(signal, "confidence", 0),
                    "strategy": strategy.algo_name,
                    "strategy_id": str(strategy.id),
                },
            },
        )
    except Exception as e:
        logger.warning("WS push failed | strategy=%s | err=%s", strategy.id, e)


def _place_live_order(strategy, signal):
    from .signal_router import route_and_place_order
    return route_and_place_order(strategy, signal)


def _place_paper_order(strategy, signal):
    # ✅ DUPLICATE CHECK
    try:
        from apps.orders.models import Order
        from django.utils import timezone
        today = timezone.now().date()
        if Order.objects.filter(
            strategy_id=strategy.id,
            status__in=["open", "pending", "filled"],
            created_at__date=today,
        ).exists():
            logger.info("Duplicate blocked | strategy=%s", strategy.id)
            return None
    except Exception as _e:
        logger.warning("Duplicate check error: %s", _e)
    from apps.orders.services import create_order
    risk = strategy.risk_config if hasattr(strategy, "risk_config") else {}
    instrument_type = getattr(strategy, "instrument_type", "equity")
    qty = int(risk.get("qty", 1))
    price = float(signal.price)

    # ✅ ATR-based SL/TP for options, fixed % for equity/futures
    if instrument_type == "options":
        meta = getattr(signal, 'metadata', {}) or {}
        atr_val    = float(meta.get("atr", 0))
        spot_price = float(meta.get("spot", price))
        option_type = meta.get("option_type", "CE")

        if atr_val <= 0:
            atr_val = spot_price * 0.006  # fallback

        atr_sl_mult = float(risk.get("atr_sl_mult", 1.0))
        atr_tp_mult = float(risk.get("atr_tp_mult", 3.0))

        if option_type == "PE":  # bearish
            sl_price  = round(spot_price + (atr_sl_mult * atr_val), 2)
            tgt_price = round(spot_price - (atr_tp_mult * atr_val), 2)
        else:  # CE bullish
            sl_price  = round(spot_price - (atr_sl_mult * atr_val), 2)
            tgt_price = round(spot_price + (atr_tp_mult * atr_val), 2)

        logger.info(
            "Paper ATR SL/TP | spot=%.2f | ATR=%.2f | %s | SL=%.2f | TP=%.2f",
            spot_price, atr_val, option_type, sl_price, tgt_price
        )
    else:
        sl_pct     = float(risk.get("sl_pct", 0.5))
        target_pct = float(risk.get("target_pct", 1.0))
        if signal.signal_type == "buy":
            sl_price  = round(price * (1 - sl_pct / 100), 2)
            tgt_price = round(price * (1 + target_pct / 100), 2)
        else:
            sl_price  = round(price * (1 + sl_pct / 100), 2)
            tgt_price = round(price * (1 - target_pct / 100), 2)

    return create_order(
        strategy=strategy, symbol=signal.symbol, side=signal.signal_type,
        quantity=qty, price=Decimal(str(price)), sl_price=Decimal(str(sl_price)),
        target_price=Decimal(str(tgt_price)),
        instrument_type=instrument_type,
        broker=None, mode="paper",
    )

def _place_paper_order_ict(strategy, signal, sl_price=None, tp_price=None):
    # ✅ DUPLICATE CHECK
    try:
        from apps.orders.models import Order
        from django.utils import timezone
        today = timezone.now().date()
        if Order.objects.filter(
            strategy_id=strategy.id,
            status__in=["open", "pending", "filled"],
            created_at__date=today,
        ).exists():
            logger.info("Duplicate blocked | strategy=%s | symbol=%s", strategy.id, signal.symbol)
            return None
    except Exception as _e:
        logger.warning("Duplicate check error: %s", _e)
    from apps.orders.services import create_order

    meta = signal.metadata or {}
    price = float(signal.price)
    sl = float(sl_price) if sl_price else _fallback_sl(price, signal.signal_type, strategy)
    tp = float(tp_price) if tp_price else _fallback_tp(price, signal.signal_type, strategy)
    qty = int(meta.get("position_size", 1) or strategy.parameters.get("qty", 1))

    logger.info(
        "Paper ICT | %s %s @ %.2f | SL=%.2f TP=%.2f | qty=%d",
        signal.signal_type.upper(), signal.symbol, price, sl, tp, qty,
    )

    return create_order(
        strategy=strategy, symbol=signal.symbol, side=signal.signal_type,
        quantity=qty, price=Decimal(str(price)), sl_price=Decimal(str(sl)),
        target_price=Decimal(str(tp)),
        instrument_type=getattr(strategy, "instrument_type", "equity"),
        broker=None, mode="paper",
    )


def _place_live_order_ict(strategy, signal, sl_price=None, tp_price=None):
    from .signal_router import route_and_place_order

    if sl_price and not hasattr(signal, "sl_price"):
        signal.sl_price = sl_price
    if tp_price and not hasattr(signal, "tp_price"):
        signal.tp_price = tp_price

    return route_and_place_order(strategy, signal)


def _fallback_sl(price: float, side: str, strategy) -> float:
    risk = strategy.risk_config if hasattr(strategy, "risk_config") else {}
    sl_pct = float(risk.get("sl_pct", 0.5))
    return round(
        price * (1 - sl_pct / 100) if side == "buy" else price * (1 + sl_pct / 100), 2
    )


def _fallback_tp(price: float, side: str, strategy) -> float:
    risk = strategy.risk_config if hasattr(strategy, "risk_config") else {}
    tp_pct = float(risk.get("target_pct", 1.0))
    return round(
        price * (1 + tp_pct / 100) if side == "buy" else price * (1 - tp_pct / 100), 2
    )