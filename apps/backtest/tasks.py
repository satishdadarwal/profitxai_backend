import datetime
import logging

from django.utils import timezone

import pandas as pd
from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=1, time_limit=600, soft_time_limit=550)
def run_backtest_task(self, run_id: str):
    from .engine import _REGISTRY, BacktestEngine, get_strategy
    from .models import BacktestRun

    try:
        run = BacktestRun.objects.get(pk=run_id)
    except BacktestRun.DoesNotExist:
        logger.error("BacktestRun %s not found", run_id)
        return

    run.status = BacktestRun.Status.RUNNING
    run.save(update_fields=["status"])

    try:
        # ── Common Fake Strategy ─────────────────────────────────
        class FakeStrategy:
            def __init__(self, run):
                self.name = run.strategy_name
                self.symbol = run.symbol
                self.mode = "paper"
                self.user = run.user
                self.parameters = run.strategy_params or {}

        # ── ICT MTF ─────────────────────────────────────────
        if run.strategy_name == "ict_mtf":
            from apps.strategies.ict_integration import run_backtest_ict

            fake = FakeStrategy(run)

            results_dict = run_backtest_ict(
                fake,
                run.start_date.strftime("%Y-%m-%d"),
                run.end_date.strftime("%Y-%m-%d"),
                timeframe=run.timeframe or "15m",
            )

            run.results = results_dict
            run.status = BacktestRun.Status.DONE
            run.completed_at = timezone.now()
            run.save(update_fields=["status", "results", "completed_at"])

            logger.info("ICT MTF Backtest DONE %s", run_id)
            return

        # ── ICT Silver Bullet ───────────────────────────────
        if run.strategy_name == "ict_silver_bullet":
            from apps.ict_engine.silver_bullet import run_silver_bullet_backtest

            fake = FakeStrategy(run)

            results_dict = run_silver_bullet_backtest(
                fake,
                run.start_date.strftime("%Y-%m-%d"),
                run.end_date.strftime("%Y-%m-%d"),
            )

            run.results = results_dict
            run.status = BacktestRun.Status.DONE
            run.completed_at = timezone.now()
            run.save(update_fields=["status", "results", "completed_at"])

            logger.info("ICT Silver Bullet Backtest DONE %s", run_id)
            return

        # ── Normal strategies ───────────────────────────────────
        if run.strategy_name not in _REGISTRY:
            raise ValueError(f"Strategy not registered: {run.strategy_name}")

        strategy = get_strategy(run.strategy_name, run.strategy_params or {})

        from_ts = int(
            datetime.datetime.combine(run.start_date, datetime.time.min).timestamp()
        )
        to_ts = int(
            datetime.datetime.combine(run.end_date, datetime.time.max).timestamp()
        )

        from apps.common.candle_service import fetch_candles

        candles = fetch_candles(
            symbol=run.symbol,
            timeframe=run.timeframe,
            from_ts=from_ts,
            to_ts=to_ts,
        )

        if not candles:
            logger.warning("Primary candle fetch failed → using fallback")
            candles = _fetch_candles_fallback(
                symbol=run.symbol,
                timeframe=run.timeframe,
                from_ts=from_ts,
                to_ts=to_ts,
            )

        if not candles:
            raise ValueError("No candle data available")

        self.update_state(state="PROGRESS", meta={"progress": 10})

        df = pd.DataFrame(
            [
                {
                    "time": c.timestamp,
                    "open": c.open,
                    "high": c.high,
                    "low": c.low,
                    "close": c.close,
                    "volume": c.volume,
                }
                for c in candles
            ]
        )

        df["time"] = pd.to_datetime(df["time"], unit="s")
        df = df.set_index("time").sort_index()

        self.update_state(state="PROGRESS", meta={"progress": 25})

        engine = BacktestEngine(
            df=df,
            strategy=strategy,
            initial_capital=int(run.initial_capital),  # ✅ fixed
            fee_rate=float(run.fee_rate),
        )

        total = len(df)
        for i in range(total):
            if i % 50 == 0:
                progress = 25 + int((i / total) * 65)
                self.update_state(state="PROGRESS", meta={"progress": progress})

        results = engine.run()
        self.update_state(state="PROGRESS", meta={"progress": 100})

        run.results = results.to_dict()
        run.status = BacktestRun.Status.DONE
        run.completed_at = timezone.now()
        run.save(update_fields=["status", "results", "completed_at"])

        logger.info("Backtest DONE %s", run_id)

        # ── WebSocket push ───────────────────────────────────────
        try:
            from asgiref.sync import async_to_sync
            from channels.layers import get_channel_layer

            layer = get_channel_layer()
            group = f"user_{run.user.id}"

            if layer is not None:  # ✅ fix
                async_to_sync(layer.group_send)(
                    group,
                    {
                        "type": "backtest_done",
                        "id": str(run.id),
                        "status": "done",
                        "results": run.results,
                    },
                )
        except Exception as ws_err:
            logger.warning("WS push failed: %s", ws_err)

    except Exception as e:
        logger.error("Backtest FAILED %s %s", run_id, e)
        run.status = BacktestRun.Status.FAILED
        run.error_message = str(e)
        run.save(update_fields=["status", "error_message"])


def _fetch_candles_fallback(symbol, timeframe, from_ts, to_ts):
    try:
        import yfinance as yf

        yahoo_map = {
            "NIFTY": "^NSEI",
            "BANKNIFTY": "^NSEBANK",
            "SENSEX": "^BSESN",
            "FINNIFTY": "NIFTY_FIN_SERVICE.NS",
            "MIDCPNIFTY": "^NSEMDCP50",
        }

        clean = symbol.upper()
        for k in ["NSE:", "BSE:", "-INDEX", ".NS"]:
            clean = clean.replace(k, "")

        yahoo_sym = yahoo_map.get(clean, f"{clean}.NS")

        interval_map = {
            "1": "1m",
            "5": "5m",
            "15": "15m",
            "60": "1h",
            "1h": "1h",
            "D": "1d",
        }

        interval = interval_map.get(str(timeframe), "1d")

        start = datetime.datetime.fromtimestamp(from_ts).strftime("%Y-%m-%d")
        end = datetime.datetime.fromtimestamp(to_ts).strftime("%Y-%m-%d")

        hist = yf.Ticker(yahoo_sym).history(
            start=start,
            end=end,
            interval=interval,
        )

        if hist.empty:
            logger.warning("yfinance empty for %s", yahoo_sym)
            return None

        from typing import cast

        import pandas as pd

        from broker_adapters.base import CandleBar

        return [
            CandleBar(
                timestamp=int(cast(pd.Timestamp, ts).timestamp()),
                open=float(row["Open"]),
                high=float(row["High"]),
                low=float(row["Low"]),
                close=float(row["Close"]),
                volume=float(row["Volume"]),
            )
            for ts, row in hist.iterrows()
        ]

    except Exception as e:
        logger.error("_fetch_candles_fallback error: %s", e)
        return None