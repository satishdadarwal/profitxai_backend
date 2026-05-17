from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from .base import RiskParameters, Signal
from .dispatcher import (
    Dispatcher,
    InMemoryDatabase,
    LoggingWebSocket,
    PaperExecutionAdapter,
)
from .ict import MTFAnalysis, run_mtf_analysis
from .scanner import Scanner

logger = logging.getLogger(__name__)


# -- data provider interface ---------------------------------------------------


class DataProvider(ABC):
    """
    Abstract market data provider.
    Implement this for your broker / exchange / data vendor.
    """

    @abstractmethod
    async def fetch(
        self,
        symbol: str,
        timeframe: str,
        bars: int = 500,
    ) -> pd.DataFrame:
        """
        Fetch OHLCV bars.
        Returns DataFrame with columns: open, high, low, close[, volume]
        and a DatetimeIndex (UTC preferred).
        """

    async def fetch_many(
        self,
        symbol: str,
        timeframes: list[str],
        bars: int = 500,
    ) -> dict[str, pd.DataFrame]:
        """Fetch multiple TFs concurrently for a single symbol."""
        results = await asyncio.gather(
            *[self.fetch(symbol, tf, bars) for tf in timeframes],
            return_exceptions=True,
        )
        out: dict[str, pd.DataFrame] = {}
        for tf, result in zip(timeframes, results):
            if isinstance(result, Exception):
                logger.error("Data fetch failed %s/%s: %s", symbol, tf, result)
                out[tf] = pd.DataFrame()
            else:
                out[tf] = result
        return out


class SyntheticDataProvider(DataProvider):
    """
    Development stub that generates synthetic OHLCV data.
    Replace with a real broker adapter in production.
    """

    async def fetch(
        self,
        symbol: str,
        timeframe: str,
        bars: int = 500,
    ) -> pd.DataFrame:
        await asyncio.sleep(0)  # simulate async I/O
        _tf_minutes = {
            "1m": 1,
            "5m": 5,
            "15m": 15,
            "30m": 30,
            "1H": 60,
            "4H": 240,
            "1D": 1440,
            "1W": 10080,
        }
        minutes = _tf_minutes.get(timeframe, 60)
        freq = f"{minutes}min"
        import numpy as np

        rng = np.random.default_rng(hash(symbol + timeframe) % (2**32))
        end = pd.Timestamp.utcnow().floor(freq)
        idx = pd.date_range(end=end, periods=bars, freq=freq, tz="UTC")
        price = 1.1000
        closes = [price]
        for _ in range(bars - 1):
            price *= 1 + rng.normal(0, 0.0008)
            closes.append(price)
        closes_arr = pd.Series(closes)
        highs = closes_arr + abs(pd.Series(rng.normal(0, 0.0003, bars)))
        lows = closes_arr - abs(pd.Series(rng.normal(0, 0.0003, bars)))
        opens = closes_arr.shift(1).fillna(closes_arr)
        volumes = pd.Series(rng.integers(500, 5000, bars))
        return pd.DataFrame(
            {
                "open": opens.values,
                "high": highs.values,
                "low": lows.values,
                "close": closes_arr.values,
                "volume": volumes.values,
            },
            index=idx,
        )


# -- runner configuration ------------------------------------------------------


@dataclass
class RunnerConfig:
    # Timeframes to fetch and analyse
    timeframes: list[str] = field(default_factory=lambda: ["4H", "1H", "15m", "5m"])
    anchor_tf: str = "4H"
    execution_tf: str = "5m"

    # ICT analysis
    swing_method: str = "fractal"
    swing_left: int = 3
    swing_right: int = 3
    bars_per_tf: int = 500

    # Signal filters
    min_confluence: float = 60.0
    min_rr: float = 2.0
    atr_period: int = 14

    # Execution
    dry_run: bool = True  # paper trade by default
    max_signals_per_cycle: int = 3

    # Loop
    loop_interval_seconds: int = 60
    max_concurrent_symbols: int = 10


# -- runner --------------------------------------------------------------------


class StrategyRunner:
    """
    Main orchestrator for the ICT strategy engine.

    Parameters
    ----------
    provider    : DataProvider implementation
    dispatcher  : Dispatcher (DB + WS + executor)
    risk_params : risk controls
    config      : runner configuration
    """

    def __init__(
        self,
        provider: DataProvider,
        dispatcher: Optional[Dispatcher] = None,
        risk_params: Optional[RiskParameters] = None,
        config: Optional[RunnerConfig] = None,
    ) -> None:
        self.provider = provider
        self.config = config or RunnerConfig()
        self.risk = risk_params or RiskParameters()
        self.dispatcher = dispatcher or Dispatcher(
            db=InMemoryDatabase(),
            ws=LoggingWebSocket(),
            executor=PaperExecutionAdapter(),
            dry_run=self.config.dry_run,
        )
        self.scanner = Scanner(
            risk_params=self.risk,
            min_confluence=self.config.min_confluence,
            min_rr=self.config.min_rr,
            atr_period=self.config.atr_period,
        )
        self._running = False
        self._cycle_count = 0
        self._signals_emitted: list[Signal] = []
        self._errors: list[dict] = []

    # -- per-symbol pipeline ---------------------------------------------------

    async def _process_symbol(self, symbol: str) -> Optional[Signal]:
        """Full ICT pipeline for one symbol. Returns signal or None."""
        cycle_start = time.perf_counter()
        logger.info("[%s] Starting analysis cycle", symbol)

        # 1. FETCH
        try:
            tf_data = await self.provider.fetch_many(
                symbol,
                self.config.timeframes,
                self.config.bars_per_tf,
            )
        except Exception as exc:
            logger.error("[%s] Data fetch error: %s", symbol, exc)
            self._errors.append({"symbol": symbol, "stage": "fetch", "error": str(exc)})
            return None

        valid_tfs = {k: v for k, v in tf_data.items() if not v.empty}
        if len(valid_tfs) < 2:
            logger.warning(
                "[%s] Insufficient TF data (%d/%d)",
                symbol,
                len(valid_tfs),
                len(self.config.timeframes),
            )
            return None

        # 2. ICT ANALYSIS (all TFs)
        try:
            mtf: MTFAnalysis = run_mtf_analysis(
                symbol=symbol,
                tf_data=valid_tfs,
                anchor_tf=self.config.anchor_tf,
                execution_tf=self.config.execution_tf,
                swing_method=self.config.swing_method,
                swing_left=self.config.swing_left,
                swing_right=self.config.swing_right,
            )
        except Exception as exc:
            logger.error("[%s] ICT analysis error: %s", symbol, exc, exc_info=True)
            self._errors.append(
                {"symbol": symbol, "stage": "ict_analysis", "error": str(exc)}
            )
            return None

        # 3. CONFLUENCE already computed inside run_mtf_analysis
        conf = mtf.confluence
        logger.info(
            "[%s] Confluence: %.1f (%s) | bias=%s | aligned=%s | KZ=%s",
            symbol,
            conf.total if conf else 0,
            conf.confidence if conf else "N/A",
            mtf.primary_bias,
            mtf.aligned,
            (
                mtf.killzone.highest_priority_zone.name.value
                if mtf.killzone and mtf.killzone.highest_priority_zone
                else "none"
            ),
        )

        # 4. SIGNAL BUILD
        exec_df = valid_tfs.get(self.config.execution_tf, pd.DataFrame())
        signal = self.scanner.scan(mtf, exec_df)
        if signal is None:
            logger.info("[%s] No qualifying signal this cycle", symbol)
            return None

        # 5-7. DB SAVE → WS NOTIFY → ORDER EXECUTE
        try:
            dispatch_result = await self.dispatcher.dispatch(signal)
            logger.info(
                "[%s] Dispatched signal %s | db=%s broker=%s status=%s",
                symbol,
                signal.id[:8],
                (
                    dispatch_result.get("db_id", "?")[:8]
                    if dispatch_result.get("db_id")
                    else "?"
                ),
                dispatch_result.get("broker_id", "none"),
                dispatch_result.get("status"),
            )
        except Exception as exc:
            logger.error("[%s] Dispatch error: %s", symbol, exc, exc_info=True)
            self._errors.append(
                {"symbol": symbol, "stage": "dispatch", "error": str(exc)}
            )
            return signal  # signal built but not dispatched

        elapsed = round(time.perf_counter() - cycle_start, 3)
        logger.info("[%s] Cycle complete in %.3fs", symbol, elapsed)
        return signal

    # -- multi-symbol cycle ----------------------------------------------------

    async def run_once(self, symbols: list[str]) -> list[Signal]:
        """
        Process all symbols in a single pass.
        Returns the list of signals generated.
        """
        self._cycle_count += 1
        cycle_ts = datetime.now(tz=timezone.utc).isoformat()
        logger.info(
            "=== CYCLE %d @ %s | symbols=%s ===",
            self._cycle_count,
            cycle_ts,
            symbols,
        )

        sem = asyncio.Semaphore(self.config.max_concurrent_symbols)

        async def _guarded(sym: str) -> Optional[Signal]:
            async with sem:
                return await self._process_symbol(sym)

        results = await asyncio.gather(
            *[_guarded(s) for s in symbols], return_exceptions=True
        )

        signals: list[Signal] = []
        for sym, result in zip(symbols, results):
            if isinstance(result, Exception):
                logger.error("Unhandled exception for %s: %s", sym, result)
            elif result is not None:
                signals.append(result)
                self._signals_emitted.append(result)

        # Cap to max signals per cycle (ranked by confluence)
        signals.sort(key=lambda s: s.confluence_score, reverse=True)
        signals = signals[: self.config.max_signals_per_cycle]

        logger.info(
            "=== CYCLE %d DONE | %d signals generated ===",
            self._cycle_count,
            len(signals),
        )
        return signals

    # -- continuous loop -------------------------------------------------------

    async def run_loop(
        self,
        symbols: list[str],
        interval_seconds: Optional[int] = None,
        max_cycles: Optional[int] = None,
    ) -> None:
        """
        Run the pipeline on a fixed interval indefinitely (or until max_cycles).

        Parameters
        ----------
        symbols          : list of trading symbols
        interval_seconds : sleep between cycles (defaults to config value)
        max_cycles       : stop after N cycles (None = run forever)
        """
        interval = interval_seconds or self.config.loop_interval_seconds
        self._running = True
        logger.info(
            "Strategy engine starting | interval=%ds | dry_run=%s",
            interval,
            self.config.dry_run,
        )

        try:
            while self._running:
                if max_cycles and self._cycle_count >= max_cycles:
                    logger.info("Max cycles (%d) reached -- stopping", max_cycles)
                    break

                cycle_start = time.perf_counter()
                await self.run_once(symbols)
                elapsed = time.perf_counter() - cycle_start
                sleep_time = max(0.0, interval - elapsed)
                logger.info("Next cycle in %.1fs", sleep_time)
                await asyncio.sleep(sleep_time)

        except asyncio.CancelledError:
            logger.info("Runner cancelled")
        except KeyboardInterrupt:
            logger.info("Runner stopped by user")
        finally:
            self._running = False
            logger.info(
                "Engine stopped after %d cycles | %d signals emitted | %d errors",
                self._cycle_count,
                len(self._signals_emitted),
                len(self._errors),
            )

    def stop(self) -> None:
        """Signal the loop to stop after the current cycle."""
        self._running = False
        logger.info("Stop requested")

    # -- diagnostics -----------------------------------------------------------

    def summary(self) -> dict:
        return {
            "cycles_run": self._cycle_count,
            "signals_emitted": len(self._signals_emitted),
            "errors": len(self._errors),
            "recent_signals": [
                {
                    "id": s.id[:8],
                    "symbol": s.symbol,
                    "direction": s.direction.value if s.direction else None,
                    "confluence": s.confluence_score,
                    "rr": s.risk_reward,
                    "tags": s.tags,
                }
                for s in self._signals_emitted[-10:]
            ],
            "recent_errors": self._errors[-5:],
        }


# -- convenience factory -------------------------------------------------------


def build_paper_runner(
    symbols: Optional[list[str]] = None,
    config: Optional[RunnerConfig] = None,
) -> tuple[StrategyRunner, list[str]]:
    """
    Quick-start: build a paper-trading runner with synthetic data.
    Returns (runner, symbols).
    """
    cfg = config or RunnerConfig(dry_run=True)
    runner = StrategyRunner(
        provider=SyntheticDataProvider(),
        config=cfg,
    )
    syms = symbols or ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD"]
    return runner, syms


# -- entry point ---------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stdout,
    )

    runner, symbols = build_paper_runner()

    async def _main() -> None:
        await runner.run_loop(symbols, interval_seconds=30, max_cycles=3)
        import json

        print("\n=== SUMMARY ===")
        print(json.dumps(runner.summary(), indent=2, default=str))

    asyncio.run(_main())
