# apps/strategies/ict_integration.py
#
# ICT Engine ↔ Django integration layer.
# Teeno modes (paper / live / backtest) yahan se run hote hain.
#
# Architecture:
#   FyersDataProvider     — Fyers API se OHLCV fetch karo
#   DeltaDataProvider     — Delta Exchange se crypto OHLCV fetch karo
#   DjangoDatabaseAdapter — StrategySignal model mein save karo
#   DjangoWebSocketAdapter— Channel Layer pe push karo
#   FyersExecutionAdapter — Live orders Fyers pe bhejo
#   execute_cycle_ict()   — strategies/tasks.py se call hota hai
#   run_backtest_ict()    — historical candles pe ICT engine run karo

from __future__ import annotations

import asyncio
import datetime
import logging
from decimal import Decimal
from typing import Optional, cast

import pandas as pd

from apps.ict_engine.base import RiskParameters, Signal, SignalStatus
from apps.ict_engine.dispatcher import (
    DatabaseAdapter,
    ExecutionAdapter,
    PaperExecutionAdapter,
    WebSocketAdapter,
)
from apps.ict_engine.ict import run_mtf_analysis
from apps.ict_engine.runner import DataProvider, RunnerConfig, StrategyRunner
from apps.ict_engine.scanner import Scanner

logger = logging.getLogger(__name__)

# ─── Timeframe label map — Fyers resolution → ICT label ──────────────────────
_FYERS_RESOLUTION_MAP = {
    "1m": "1",
    "5m": "5",
    "15m": "15",
    "30m": "30",
    "1H": "60",
    "4H": "60",
    "1D": "D",
}



# Crypto symbol detector
_CRYPTO_KEYWORDS = {"BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "USDT"}

def _is_crypto(symbol: str) -> bool:
    upper = symbol.upper()
    return any(kw in upper for kw in _CRYPTO_KEYWORDS)


# ─── 1. Fyers Data Provider ───────────────────────────────────────────────────
class FyersDataProvider(DataProvider):
    """
    Fyers API se OHLCV fetch karta hai.
    Strategy model ke user ka Fyers account use karta hai.
    """

    def __init__(self, user, days_back: int = 30):
        self.user = user
        self.days_back = days_back
        self._fyers = None

    def _get_fyers_client(self):
        if self._fyers is not None:
            return self._fyers
        from fyers_apiv3 import fyersModel

        from apps.brokers.models import BrokerAccount

        account = BrokerAccount.objects.filter(
            user=self.user,
            broker="fyers",
            is_active=True,
            is_verified=True,
        ).first()
        if not account or not account.access_token:
            raise RuntimeError("Fyers account not connected or token missing")
        self._fyers = fyersModel.FyersModel(
            client_id=account.app_id,
            token=account.access_token,
            log_path="",
            is_async=False,
        )
        return self._fyers

    async def fetch(
        self,
        symbol: str,
        timeframe: str,
        bars: int = 500,
    ) -> pd.DataFrame:
        """Async wrapper — runs sync Fyers call in executor."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self._fetch_sync, symbol, timeframe, bars
        )

    def _fetch_sync(self, symbol: str, timeframe: str, bars: int) -> pd.DataFrame:
        try:
            fyers = self._get_fyers_client()
            from apps.brokers.symbol_mapper import normalize_for_fyers
            fyers_sym = normalize_for_fyers(symbol)
            resolution = _FYERS_RESOLUTION_MAP.get(timeframe, "15")

            days = self._days_for_tf(timeframe, bars)
            today = datetime.date.today()
            from_date = (today - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
            to_date = today.strftime("%Y-%m-%d")

            # ✅ FIX 1: _params alag variable — cast() ke andar data= conflict gone
            _params = {
                "symbol": fyers_sym,
                "resolution": resolution,
                "date_format": "1",
                "range_from": from_date,
                "range_to": to_date,
                "cont_flag": "1",
            }
            data = cast(dict, fyers.history(data=_params))

            if data.get("s") != "ok":
                logger.warning("Fyers history failed: %s", data)
                return pd.DataFrame()

            candles = data.get("candles", [])
            if not candles:
                return pd.DataFrame()

            df = pd.DataFrame(
                candles, columns=["ts", "open", "high", "low", "close", "volume"]
            )
            df.index = pd.to_datetime(df["ts"], unit="s", utc=True)
            df = df.drop(columns=["ts"])

            # ✅ FIX 2: explicit DatetimeIndex cast — .hour / .minute error gone
            if timeframe != "1D":
                dt_index = pd.DatetimeIndex(df.index)
                df = df[
                    (dt_index.hour > 9)
                    | ((dt_index.hour == 9) & (dt_index.minute >= 15))
                ]
                dt_index = pd.DatetimeIndex(df.index)  # re-cast after filter
                df = df[
                    (dt_index.hour < 15)
                    | ((dt_index.hour == 15) & (dt_index.minute <= 30))
                ]

            return df.tail(bars)

        except Exception as e:
            logger.error("FyersDataProvider._fetch_sync error: %s", e)
            return pd.DataFrame()

    @staticmethod
    def _days_for_tf(tf: str, bars: int) -> int:
        minutes_map = {
            "1m": 1,
            "5m": 5,
            "15m": 15,
            "30m": 30,
            "1H": 60,
            "4H": 240,
            "1D": 1440,
        }
        minutes = minutes_map.get(tf, 15)
        trading_mins_per_day = 375
        days_needed = (bars * minutes / trading_mins_per_day) * 1.4
        if tf == "1D":
            return min(int(days_needed) + 1, 365)
        elif tf in ("1H", "4H"):
            return min(int(days_needed) + 1, 99)
        else:
            return min(int(days_needed) + 1, 99)


# ─── 1b. Delta Data Provider ──────────────────────────────────────────────────
class DeltaDataProvider(DataProvider):
    """Delta Exchange se crypto OHLCV fetch karta hai."""

    async def fetch(
        self,
        symbol: str,
        timeframe: str,
        bars: int = 500,
    ) -> pd.DataFrame:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self._fetch_sync, symbol, timeframe, bars
        )

    def _fetch_sync(self, symbol: str, timeframe: str, bars: int) -> pd.DataFrame:
        try:
            import time
            from apps.common.candle_service import _fetch_from_delta, _DELTA_TF_MAP

            now_ts = int(time.time())
            tf_minutes_map = {
                "1m": 1, "5m": 5, "15m": 15, "30m": 30,
                "1H": 60, "4H": 240, "1D": 1440,
            }
            tf_minutes = tf_minutes_map.get(timeframe, 15)
            from_ts = now_ts - (tf_minutes * 60 * bars)

            candles = _fetch_from_delta(symbol, timeframe, from_ts, now_ts)
            if not candles:
                return pd.DataFrame()

            df = pd.DataFrame([{
                "ts": c.timestamp,
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
            } for c in candles])

            df.index = pd.to_datetime(df["ts"], unit="s", utc=True)
            df = df.drop(columns=["ts"])
            return df.tail(bars)

        except Exception as e:
            logger.error("DeltaDataProvider._fetch_sync error: %s", e)
            return pd.DataFrame()


# ─── 2. Django Database Adapter ───────────────────────────────────────────────
class DjangoDatabaseAdapter(DatabaseAdapter):
    """
    ICT Signal → StrategySignal model mein save karta hai.
    """

    def __init__(self, strategy):
        self.strategy = strategy

    async def save_signal(self, signal: Signal) -> str:
        from asgiref.sync import sync_to_async
        return await sync_to_async(self._save_signal_sync)(signal)

    def _save_signal_sync(self, signal: Signal) -> str:
        from apps.strategies.models import StrategySignal

        sig_type = signal.direction.value if signal.direction else "hold"
        db_sig = StrategySignal.objects.create(
            strategy=self.strategy,
            signal_type=sig_type,
            symbol=signal.symbol,
            price=Decimal(str(signal.entry_price)),
            reason=signal.notes or ", ".join(signal.tags),
            metadata={
                "confluence": signal.confluence_score,
                "breakdown": signal.confluence_breakdown,
                "rr": signal.risk_reward,
                "stop_loss": signal.stop_loss,
                "take_profit": signal.take_profit_1,
                "tags": signal.tags,
                "killzone": signal.killzone,
                "strength": signal.strength.value,
                "timeframes": signal.timeframes,
            },
            result="skipped",
        )
        logger.info(
            "ICT signal saved | id=%s | type=%s | score=%.1f",
            db_sig.id,
            sig_type,
            signal.confluence_score,
        )
        return str(db_sig.id)

    async def update_signal_status(
        self, signal_id: str, status: SignalStatus, **kwargs
    ):
        pass

    async def save_position(self, position) -> str:
        return position.id

    async def get_open_positions(self, symbol=None):
        return []

    async def save_order(self, order) -> str:
        return order.id


# ─── 3. Django WebSocket Adapter ──────────────────────────────────────────────
class DjangoWebSocketAdapter(WebSocketAdapter):
    """
    ICT signal → Django Channels → Flutter.
    """

    def __init__(self, strategy):
        self.strategy = strategy

    async def broadcast(self, channel: str, payload: dict) -> None:
        try:
            from channels.layers import get_channel_layer

            layer = get_channel_layer()

            # ✅ FIX 3: None guard — group_send on None error gone
            if layer is None:
                logger.warning("Channel layer not configured — skipping WS broadcast")
                return

            group = f"user_{self.strategy.user_id}"
            data = payload.get("data", payload)

            await layer.group_send(
                group,
                {
                    "type": "new_signal",
                    "direction": data.get("direction", "hold"),
                    "symbol": data.get("symbol", ""),
                    "entry": data.get("entry_price", 0),
                    "sl": data.get("stop_loss", 0),
                    "target1": data.get("take_profit_1", 0),
                    "confidence": data.get("confluence_score", 0),
                    "reason": ", ".join(data.get("tags", [])),
                    "rr": data.get("risk_reward", 0),
                    "strategy_id": str(self.strategy.id),
                    "algo": "ict_mtf",
                },
            )
        except Exception as e:
            logger.warning("DjangoWebSocketAdapter.broadcast failed: %s", e)

    async def send_to_user(self, user_id: str, payload: dict) -> None:
        await self.broadcast("", payload)


# ─── 4. Fyers Execution Adapter ───────────────────────────────────────────────
class FyersExecutionAdapter(ExecutionAdapter):
    """
    Live mode — Fyers pe actual order place karta hai.
    """

    def __init__(self, user):
        self.user = user

    async def place_order(self, order) -> str:
        from asgiref.sync import sync_to_async
        return await sync_to_async(self._place_order_sync)(order)

    def _place_order_sync(self, order) -> str:
        try:
            from fyers_apiv3 import fyersModel

            from apps.brokers.models import BrokerAccount

            account = BrokerAccount.objects.filter(
                user=self.user,
                broker="fyers",
                is_active=True,
                is_verified=True,
            ).first()
            if not account:
                raise RuntimeError("No Fyers account")

            fyers = fyersModel.FyersModel(
                client_id=account.app_id,
                token=account.access_token,
                log_path="",
                is_async=False,
            )

            from apps.brokers.symbol_mapper import normalize_for_fyers
            fyers_sym = normalize_for_fyers(order.symbol)

            _order_params = {
                "symbol": fyers_sym,
                "qty": int(order.size),
                "type": 2,
                "side": 1 if order.direction.value == "long" else -1,
                "productType": "INTRADAY",
                "validity": "DAY",
                "stopLoss": order.stop_loss,
                "takeProfit": order.take_profit,
            }
            result = cast(dict, fyers.place_order(data=_order_params))
            broker_id = result.get("id", "unknown")
            logger.info("Live order placed | broker_id=%s", broker_id)
            return str(broker_id)
        except Exception as e:
            logger.error("FyersExecutionAdapter.place_order error: %s", e)
            raise

    async def cancel_order(self, broker_order_id: str) -> bool:
        return True

    async def get_account_balance(self) -> float:
        return 0.0

    async def get_positions(self):
        return []


# ─── 5. execute_cycle_ict ─────────────────────────────────────────────────────
def execute_cycle_ict(strategy, symbol: str) -> dict:
    """
    ICT MTF analysis + signal dispatch — ek cycle.
    strategies/services.py ke execute_cycle() mein call karo.
    """
    from apps.ict_engine.dispatcher import Dispatcher

    config = RunnerConfig(
        timeframes=["1D", "4H", "1H", "15m"],
        anchor_tf="1D",
        execution_tf="15m",
        min_confluence=strategy.parameters.get("min_confluence", 60.0),
        min_rr=strategy.parameters.get("min_rr", 2.0),
        dry_run=(strategy.mode == "paper"),
        bars_per_tf=strategy.parameters.get("bars_per_tf", 300),
    )

    # Auto-detect provider
    if _is_crypto(symbol):
        from apps.common.candle_service import _fetch_from_delta
        provider = DeltaDataProvider()
    else:
        provider = FyersDataProvider(user=strategy.user)

    db_adapter = DjangoDatabaseAdapter(strategy=strategy)
    ws_adapter = DjangoWebSocketAdapter(strategy=strategy)

    if strategy.mode == "live":
        executor = FyersExecutionAdapter(user=strategy.user)
    else:
        executor = PaperExecutionAdapter()

    dispatcher = Dispatcher(
        db=db_adapter,
        ws=ws_adapter,
        executor=executor,
        dry_run=config.dry_run,
    )

    runner = StrategyRunner(
        provider=provider,
        dispatcher=dispatcher,
        config=config,
    )

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        signals = loop.run_until_complete(runner.run_once([symbol]))
        loop.close()
    except Exception as e:
        logger.error("execute_cycle_ict error: %s", e, exc_info=True)
        return _null_signal(symbol)

    if not signals:
        return _null_signal(symbol)

    sig = signals[0]
    return {
        "signal_type": sig.direction.value if sig.direction else "hold",
        "symbol": sig.symbol,
        "price": Decimal(str(sig.entry_price)),
        "reason": ", ".join(sig.tags),
        "metadata": {
            "confluence": sig.confluence_score,
            "rr": sig.risk_reward,
            "stop_loss": sig.stop_loss,
            "take_profit": sig.take_profit_1,
            "tags": sig.tags,
        },
        "result": "executed",
        "order": None,
    }


def _null_signal(symbol: str) -> dict:
    return {
        "signal_type": "hold",
        "symbol": symbol,
        "price": Decimal("0"),
        "reason": "No ICT setup found",
        "metadata": {},
        "result": "skipped",
        "order": None,
    }


# ─── 6. run_backtest_ict ──────────────────────────────────────────────────────
def run_backtest_ict(
    strategy,
    from_date: str,
    to_date: str,
    timeframe: str = "15m",
) -> dict:
    """
    ICT engine ko historical candles pe walk-forward run karo.
    strategies/services.py ke run_backtest() mein call karo.
    """
    from fyers_apiv3 import fyersModel

    from apps.brokers.models import BrokerAccount

    account = BrokerAccount.objects.filter(
        user=strategy.user,
        broker="fyers",
        is_active=True,
        is_verified=True,
    ).first()
    if not account:
        raise RuntimeError("Fyers account not connected")

    fyers = fyersModel.FyersModel(
        client_id=account.app_id,
        token=account.access_token,
        log_path="",
        is_async=False,
    )

    from apps.brokers.symbol_mapper import normalize_for_fyers
    fyers_sym = normalize_for_fyers(strategy.symbol)

    from_dt = datetime.datetime.strptime(from_date, "%Y-%m-%d")
    to_dt = datetime.datetime.strptime(to_date, "%Y-%m-%d")
    days = (to_dt - from_dt).days

    if days > 365:
        timeframe = "1D"
    elif days > 180:
        timeframe = "60"
    else:
        timeframe = "15"

    resolution = timeframe

    import datetime as _dt2

    def _fetch_chunks(sym: str, res: str, f_date: str, t_date: str, max_days: int = 95) -> list:
        all_candles: list = []
        start = _dt2.datetime.strptime(f_date, "%Y-%m-%d")
        end = _dt2.datetime.strptime(t_date, "%Y-%m-%d")
        cur = start
        while cur < end:
            chunk_end = min(cur + _dt2.timedelta(days=max_days), end)
            # ✅ FIX 1 (backtest): _params alag — cast() conflict gone
            _params = {
                "symbol": sym,
                "resolution": res,
                "date_format": "1",
                "range_from": cur.strftime("%Y-%m-%d"),
                "range_to": chunk_end.strftime("%Y-%m-%d"),
                "cont_flag": "1",
            }
            r = cast(dict, fyers.history(data=_params))
            if r.get("s") == "ok":
                all_candles.extend(r.get("candles", []))
            cur = chunk_end + _dt2.timedelta(days=1)
        return all_candles

    candles_raw = _fetch_chunks(fyers_sym, resolution, from_date, to_date)

    if not candles_raw:
        raise RuntimeError("Fyers history failed: no data returned")
    if len(candles_raw) < 50:
        raise RuntimeError("Insufficient candle data for backtest")

    candles_raw = candles_raw[-1000:]

    df_full = pd.DataFrame(
        candles_raw, columns=["ts", "open", "high", "low", "close", "volume"]
    )
    df_full.index = pd.to_datetime(df_full["ts"], unit="s", utc=True)
    df_full = df_full.drop(columns=["ts"])

    scanner = Scanner(
        risk_params=RiskParameters(
            account_balance=float(strategy.parameters.get("capital", 100000)),
            risk_per_trade_pct=float(strategy.parameters.get("risk_pct", 1.0)),
            min_rr_ratio=float(strategy.parameters.get("min_rr", 2.0)),
        ),
        min_confluence=float(strategy.parameters.get("min_confluence", 60.0)),
        min_rr=float(strategy.parameters.get("min_rr", 2.0)),
    )

    trades: list = []
    signals: list = []
    capital = float(strategy.parameters.get("capital", 100000))
    balance = capital
    warmup = 50

    total_bars = len(df_full)
    step = max(5, total_bars // 100)

    for i in range(warmup, len(df_full), step):
        window = df_full.iloc[: i + 1]

        try:
            mtf = run_mtf_analysis(
                symbol=strategy.symbol,
                tf_data={timeframe: window},
                anchor_tf=timeframe,
                execution_tf=timeframe,
            )
        except Exception as e:
            logger.debug("Backtest MTF error at bar %d: %s", i, e)
            continue

        sig = scanner.scan(mtf, window)

        # ✅ FIX 4: direction None check — .value on None error gone
        if sig is None or not sig.is_actionable() or sig.direction is None:
            continue

        if i + 1 >= len(df_full):
            continue

        fill_price = float(df_full["open"].iloc[i + 1])
        exit_bar = min(i + 20, len(df_full) - 1)
        exit_price = float(df_full["close"].iloc[exit_bar])

        pnl = (
            (exit_price - fill_price)
            if sig.direction.value == "long"
            else (fill_price - exit_price)
        )
        pnl *= sig.position_size
        balance += pnl

        signals.append(
            {
                "ts": df_full.index[i].isoformat(),
                "direction": sig.direction.value,
                "confidence": sig.confluence_score,
                "tags": sig.tags,
            }
        )

        trades.append(
            {
                "entry_ts": df_full.index[i].isoformat(),
                "exit_ts": df_full.index[exit_bar].isoformat(),
                "side": sig.direction.value,
                "entry_price": round(fill_price, 2),
                "exit_price": round(exit_price, 2),
                "qty": round(sig.position_size, 4),
                "pnl": round(pnl, 2),
                "balance": round(balance, 2),
                "confluence": sig.confluence_score,
                "tags": sig.tags,
            }
        )

    total = len(trades)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    net = round(balance - capital, 2)

    equity_curve = [{"ts": t["exit_ts"], "equity": t["balance"]} for t in trades]

    import numpy as np

    pnls = [t["pnl"] for t in trades]
    win_pnls = [p for p in pnls if p > 0]
    loss_pnls = [p for p in pnls if p <= 0]

    avg_win = round(sum(win_pnls) / len(win_pnls), 2) if win_pnls else 0.0
    avg_loss = round(sum(loss_pnls) / len(loss_pnls), 2) if loss_pnls else 0.0

    gross_profit = sum(win_pnls)
    gross_loss = abs(sum(loss_pnls))
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss else 0.0

    peak = capital
    max_dd = 0.0
    bal = capital
    for t in trades:
        bal += t["pnl"]
        if bal > peak:
            peak = bal
        dd = (peak - bal) / peak * 100
        if dd > max_dd:
            max_dd = dd
    max_dd = round(max_dd, 2)

    if len(pnls) > 1:
        returns = [p / capital for p in pnls]
        avg_r = float(np.mean(returns))
        std_r = float(np.std(returns))
        sharpe = round(avg_r / std_r * np.sqrt(252), 2) if std_r else 0.0
        neg_r = [r for r in returns if r < 0]
        down_std = float(np.std(neg_r)) if neg_r else 0.0
        sortino = round(avg_r / down_std * np.sqrt(252), 2) if down_std else 0.0
    else:
        sharpe = sortino = 0.0

    calmar = round(net / capital * 100 / max_dd, 2) if max_dd else 0.0

    wr_dec = wins / total if total else 0
    expectancy = round((wr_dec * avg_win) + ((1 - wr_dec) * avg_loss), 2)

    return {
        "strategy_name": strategy.name,
        "algo_name": "ict_mtf",
        "symbol": strategy.symbol,
        "from_date": from_date,
        "to_date": to_date,
        "timeframe": timeframe,
        "total_candles": len(df_full),
        "total_signals": len(signals),
        "total_trades": total,
        "win_trades": wins,
        "loss_trades": total - wins,
        "win_rate": round(wins / total * 100, 1) if total else 0,
        "initial_capital": capital,
        "final_balance": round(balance, 2),
        "net_pnl": net,
        "return_pct": round(net / capital * 100, 2),
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": profit_factor,
        "max_drawdown": max_dd,
        "sharpe_ratio": sharpe,
        "sortino_ratio": sortino,
        "calmar_ratio": calmar,
        "expectancy": expectancy,
        "equity_curve": equity_curve,
        "trades": trades[-100:],
    }