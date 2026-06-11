# apps/backtest/engine.py
#
# ── Algo Registry ─────────────────────────────────────────────
# Har algo yahan register hota hai.
# Strategy model ka `algo_name` field iska key se match karta hai.
#
# Naya algo add karna:
#   1. BaseAlgo se inherit karo
#   2. generate_signal() implement karo
#   3. _REGISTRY mein register karo
# ─────────────────────────────────────────────────────────────

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
#  Signal Result — har algo yahi return karta hai
# ─────────────────────────────────────────────────────────────
@dataclass
class AlgoSignal:
    signal_type: str  # 'buy' | 'sell' | 'hold'
    symbol: str
    price: Decimal
    reason: str = ""
    confidence: float = 0.0  # 0-100
    metadata: dict = field(default_factory=dict)

    # execute_cycle result set karta hai
    result: str = "skipped"  # 'executed' | 'skipped'
    order: object = None


# ─────────────────────────────────────────────────────────────
#  BaseAlgo — sabhi strategies yahan se inherit karti hain
# ─────────────────────────────────────────────────────────────
class BaseAlgo(ABC):
    """
    Naya strategy banana:

        class MyAlgo(BaseAlgo):
            name = 'my_algo'

            def generate_signal(self, symbol, price, strategy, **ctx) -> AlgoSignal:
                # apna logic yahan
                return AlgoSignal(signal_type='hold', symbol=symbol, price=price)

        register('my_algo', MyAlgo)
    """

    name: str = "base"  # subclass mein override karo
    

    def __init__(self, parameters: Optional[dict] = None):
        self.params = parameters or {}

    @abstractmethod
    def generate_signal(
        self,
        symbol: str,
        price: Decimal,
        strategy: object,
        **ctx,
    ) -> AlgoSignal:
        """
        Signal generate karo.

        Args:
            symbol   — e.g. 'NIFTY', 'NSE:NIFTY50-INDEX'
            price    — current LTP (Decimal)
            strategy — Strategy model instance (params access ke liye)
            **ctx    — extra context (candles, indicators, etc.)

        Returns:
            AlgoSignal with signal_type = 'buy' | 'sell' | 'hold'
        """
        ...

    def get_param(self, key: str, default=None):
        """Strategy.parameters se param lo."""
        return self.params.get(key, default)


# ─────────────────────────────────────────────────────────────
#  Registry
# ─────────────────────────────────────────────────────────────
_REGISTRY: dict[str, type[BaseAlgo]] = {}


def register(name: str, cls: type[BaseAlgo]):
    """Algo ko registry mein add karo."""
    _REGISTRY[name] = cls
    logger.debug("Algo registered: %s → %s", name, cls.__name__)


def get_algo(name: str, parameters: Optional[dict] = None) -> BaseAlgo:
       
    """Registry se algo instance lo."""
    cls = _REGISTRY.get(name)
    if cls is None:
        raise KeyError(f"Algo '{name}' not found. Available: {list(_REGISTRY.keys())}")
    return cls(parameters or {})


# ─────────────────────────────────────────────────────────────
#  STRATEGY 1 — EMA Crossover (Testing ke liye)
# ─────────────────────────────────────────────────────────────
class EmaCrossoverAlgo(BaseAlgo):
    """
    Simple EMA 9/21 crossover strategy.
    Paper trading aur testing ke liye.

    Parameters (strategy.parameters mein set karo):
        fast_ema  : int  = 9    (fast EMA period)
        slow_ema  : int  = 21   (slow EMA period)
        min_conf  : float = 60  (minimum confidence to act)
    """

    name = "ema_crossover"

    def generate_signal(self, symbol, price, strategy, **ctx) -> AlgoSignal:
        candles = (ctx.get("candles") or ctx.get("mtf") or ctx.get("ltf") or ctx.get("htf") or [])
        if candles and hasattr(candles[0], "close"):
            candles = [{"open": c.open, "high": c.high, "low": c.low, "close": c.close, "volume": c.volume, "ts": c.timestamp} for c in candles]

        if len(candles) < 25:
            return AlgoSignal(
                signal_type="hold",
                symbol=symbol,
                price=price,
                reason="Not enough candles",
            )

        fast = int(self.get_param("fast_ema", 9) or 9)
        slow = int(self.get_param("slow_ema", 21) or 21)

        closes = [c["close"] for c in candles]

        ema_fast = self._ema(closes, fast)
        ema_slow = self._ema(closes, slow)

        if not ema_fast or not ema_slow:
            return AlgoSignal(
                signal_type="hold",
                symbol=symbol,
                price=price,
                reason="EMA calculation failed",
            )

        fast_now = ema_fast[-1]
        fast_prev = ema_fast[-2] if len(ema_fast) > 1 else fast_now
        slow_now = ema_slow[-1]
        slow_prev = ema_slow[-2] if len(ema_slow) > 1 else slow_now

        # Bullish crossover
        if fast_prev <= slow_prev and fast_now > slow_now:
            return AlgoSignal(
                signal_type="buy",
                symbol=symbol,
                price=price,
                reason=f"EMA{fast} crossed above EMA{slow}",
                confidence=80.0,
                metadata={"ema_fast": fast_now, "ema_slow": slow_now},
            )

        # Bearish crossover
        if fast_prev >= slow_prev and fast_now < slow_now:
            return AlgoSignal(
                signal_type="sell",
                symbol=symbol,
                price=price,
                reason=f"EMA{fast} crossed below EMA{slow}",
                confidence=80.0,
                metadata={"ema_fast": fast_now, "ema_slow": slow_now},
            )

        return AlgoSignal(
            signal_type="hold",
            symbol=symbol,
            price=price,
            reason="No crossover detected",
            metadata={"ema_fast": fast_now, "ema_slow": slow_now},
        )

    @staticmethod
    def _ema(prices: list, period: int) -> list:
        if len(prices) < period:
            return []
        k = 2 / (period + 1)
        seed = sum(prices[:period]) / period
        result = [seed]
        for p in prices[period:]:
            seed = p * k + seed * (1 - k)
            result.append(seed)
        return result


# ─────────────────────────────────────────────────────────────
#  STRATEGY 2 — RSI Reversal
# ─────────────────────────────────────────────────────────────
class RsiReversalAlgo(BaseAlgo):
    """
    RSI overbought/oversold reversal strategy.

    Parameters:
        rsi_period    : int   = 14
        oversold_level: float = 30   (buy signal)
        overbought_level: float = 70 (sell signal)
    """

    name = "rsi_reversal"

    def generate_signal(self, symbol, price, strategy, **ctx) -> AlgoSignal:
        candles = ctx.get("candles", [])

        period = int(self.get_param("rsi_period", 14) or 14)
        oversold = float(self.get_param("oversold_level", 30) or 30)
        overbought = float(self.get_param("overbought_level", 70) or 70)
        if len(candles) < period + 1:
            return AlgoSignal(
                signal_type="hold",
                symbol=symbol,
                price=price,
                reason="Not enough candles for RSI",
            )

        closes = [c["close"] for c in candles]
        rsi = self._rsi(closes, period)

        if rsi is None:
            return AlgoSignal(
                signal_type="hold",
                symbol=symbol,
                price=price,
                reason="RSI calculation failed",
            )

        if rsi <= oversold:
            return AlgoSignal(
                signal_type="buy",
                symbol=symbol,
                price=price,
                reason=f"RSI oversold: {rsi:.1f}",
                confidence=75.0,
                metadata={"rsi": rsi},
            )

        if rsi >= overbought:
            return AlgoSignal(
                signal_type="sell",
                symbol=symbol,
                price=price,
                reason=f"RSI overbought: {rsi:.1f}",
                confidence=75.0,
                metadata={"rsi": rsi},
            )

        return AlgoSignal(
            signal_type="hold",
            symbol=symbol,
            price=price,
            reason=f"RSI neutral: {rsi:.1f}",
            metadata={"rsi": rsi},
        )

    @staticmethod
    def _rsi(prices: list, period: int) -> Optional[float]:
        if len(prices) < period + 1:
            return None
        deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
        gains = [d if d > 0 else 0 for d in deltas[-period:]]
        losses = [-d if d < 0 else 0 for d in deltas[-period:]]
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))


# ─────────────────────────────────────────────────────────────
#  STRATEGY 3 — Always Hold (Testing / Dummy)
# ─────────────────────────────────────────────────────────────
class AlwaysHoldAlgo(BaseAlgo):
    """
    Kuch nahi karta — sirf 'hold' signal deta hai.
    System test karne ke liye use karo.
    algo_name: 'always_hold'
    """

    name = "always_hold"

    def generate_signal(self, symbol, price, strategy, **ctx) -> AlgoSignal:
        return AlgoSignal(
            signal_type="hold",
            symbol=symbol,
            price=price,
            reason="Test algo — always hold",
            confidence=100.0,
        )


class IctMtfAlgo(BaseAlgo):
    """
    ICT Multi-Timeframe Strategy.

    Ye class sirf registry mein placeholder hai.
    Actual logic `apps/strategies/ict_integration.py` mein hai.

    execute_cycle() detect karta hai agar algo_name == 'ict_mtf' hai
    toh directly ICT engine call karta hai — is generate_signal() ko bypass karke.

    Isliye ye method hamesha 'hold' return karta hai
    (kabhi directly call nahi hoga).

    Admin mein strategy banate waqt:
      algo_name = 'ict_mtf'
      parameters = {
        "min_confluence": 60,
        "min_rr": 2.0,
        "capital": 100000,
        "risk_pct": 1.0,
        "bars_per_tf": 300
      }
    """

    name = "ict_mtf"

    def generate_signal(self, symbol, price, strategy, **ctx) -> "AlgoSignal":
        # ICT engine execute_cycle mein directly call hota hai
        # ye method placeholder hai
        return AlgoSignal(
            signal_type="hold",
            symbol=symbol,
            price=price,
            reason="ICT MTF — use execute_cycle_ict() directly",
        )


class SilverBulletAlgo(BaseAlgo):
    name = "ict_silver_bullet"

    def generate_signal(self, symbol, price, strategy, **ctx):
        return AlgoSignal(
            signal_type="hold",
            symbol=symbol,
            price=price,
            reason="Silver Bullet — use execute cycle directly",
        )


# ── Register ──────────────────────────────────────────────────────────────────
register("ema_crossover", EmaCrossoverAlgo)
register("rsi_reversal", RsiReversalAlgo)
register("always_hold", AlwaysHoldAlgo)
register("ict_mtf", IctMtfAlgo)
register("ict_silver_bullet", SilverBulletAlgo)  # ✅ ICT strategy
from apps.backtest.algos.nse_option_seller import NseOptionSellerAlgo
from apps.backtest.algos.multi_confirm_crypto import MultiConfirmCryptoAlgo
register("nse_option_seller", NseOptionSellerAlgo)
register("multi_confirm_crypto", MultiConfirmCryptoAlgo)
# Alias — backtest/views.py get_strategy import karta hai
get_strategy = get_algo


# ─── BacktestEngine — tasks.py se import hota hai ────────────
class BacktestResult:
    """engine.run() ka result — to_dict() support karta hai."""

    def __init__(self, trades, initial_capital, fee_rate):
        self.trades = trades
        self.initial_capital = initial_capital
        self.fee_rate = fee_rate

    def to_dict(self):
        import numpy as np

        total = len(self.trades)
        wins = [t for t in self.trades if t.get("pnl", 0) > 0]
        losses = [t for t in self.trades if t.get("pnl", 0) <= 0]

        total_pnl = sum(t.get("pnl", 0) for t in self.trades)
        total_fees = sum(t.get("fee", 0) for t in self.trades)
        net_pnl = total_pnl - total_fees

        win_rate = len(wins) / total * 100 if total else 0
        avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t["pnl"] for t in losses) / len(losses) if losses else 0

        # Equity curve
        balance = self.initial_capital
        equity_curve = []
        for t in self.trades:
            balance += t.get("pnl", 0) - t.get("fee", 0)
            equity_curve.append(
                {
                    "ts": t.get("exit_ts", ""),
                    "equity": round(balance, 2),
                }
            )

        # Profit factor
        gross_profit = sum(t["pnl"] for t in wins)
        gross_loss = abs(sum(t["pnl"] for t in losses))
        profit_factor = gross_profit / gross_loss if gross_loss else 0.0

        # Max drawdown
        peak = self.initial_capital
        max_dd = 0
        bal = self.initial_capital
        for t in self.trades:
            bal += t.get("pnl", 0) - t.get("fee", 0)
            if bal > peak:
                peak = bal
            dd = (peak - bal) / peak * 100
            if dd > max_dd:
                max_dd = dd

        # Sharpe (simplified)
        returns = [t.get("pnl", 0) / self.initial_capital for t in self.trades]
        sharpe = 0
        if len(returns) > 1:
            avg_r = np.mean(returns)
            std_r = np.std(returns)
            sharpe = round(avg_r / std_r * np.sqrt(252), 2) if std_r else 0

        return {
            "total_trades": total,
            "win_trades": len(wins),
            "loss_trades": len(losses),
            "win_rate": round(win_rate, 2),
            "total_pnl": round(total_pnl, 2),
            "total_fees": round(total_fees, 2),
            "net_pnl": round(net_pnl, 2),
            "initial_capital": self.initial_capital,
            "final_capital": round(self.initial_capital + net_pnl, 2),
            "total_return_pct": round(net_pnl / self.initial_capital * 100, 2),
            "profit_factor": round(profit_factor, 2),
            "max_drawdown": round(max_dd, 2),
            "sharpe_ratio": sharpe,
            "sortino_ratio": sharpe * 0.8,  # approximation
            "calmar_ratio": (
                round(net_pnl / self.initial_capital * 100 / max_dd, 2)
                if max_dd > 0
                else 0.0
            ),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "expectancy": round(
                (win_rate / 100 * avg_win) + ((1 - win_rate / 100) * avg_loss), 2
            ),
            "equity_curve": equity_curve,
            "trades": self.trades[-100:],
        }


class BacktestEngine:
    """
    Walk-forward backtest engine.
    tasks.py se yaise use hota hai:
        engine = BacktestEngine(df=df, strategy=strategy,
                                initial_capital=100000, fee_rate=0.001)
        results = engine.run()
        data = results.to_dict()
    """

    def __init__(self, df, strategy, initial_capital=100_000, fee_rate=0.001, symbol=None):
        self.df = df
        self.strategy = strategy  # BaseAlgo instance
        self.initial_capital = initial_capital
        self.symbol = symbol or "UNKNOWN"
        self.fee_rate = fee_rate

    def run(self) -> BacktestResult:
        from decimal import Decimal

        df = self.df
        trades = []
        warmup = 25

        # Get symbol from df name or use default
        # ✅ FIX: actual symbol use karo, strategy name nahi
        symbol = self.symbol

        class _FakeStrategy:
            parameters = {}
            symbol = getattr(self, "_run_symbol", "BACKTEST")
            mode = "paper"

        fake_strat = _FakeStrategy()

        # O(n) fix — incremental candles cache
        candles_cache = []
        for ts_idx, row in df.iloc[:warmup].iterrows():
            candles_cache.append({
                "ts": int(ts_idx.timestamp()) if hasattr(ts_idx, "timestamp") else 0,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row.get("volume", 0)),
            })

        for i in range(warmup, len(df)):
            ts_idx = df.index[i]
            row = df.iloc[i]
            candles_cache.append({
                "ts": int(ts_idx.timestamp()) if hasattr(ts_idx, "timestamp") else 0,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row.get("volume", 0)),
            })
            price = Decimal(str(float(row["close"])))
            candles = list(candles_cache)

            try:
                signal = self.strategy.generate_signal(
                    symbol=symbol,
                    price=price,
                    strategy=fake_strat,
                    candles=candles,
                )
            except Exception as _e:
                logger.warning("BacktestEngine signal error at bar %d: %s", i, _e)
                continue

            if signal.signal_type not in ("buy", "sell"):
                continue

            if i + 1 >= len(df):
                continue

            entry = float(df["close"].iloc[i])
            exit_ = float(df["close"].iloc[min(i + 5, len(df) - 1)])
            qty = max(1, int(self.initial_capital * 0.01 / max(abs(entry - exit_), 1)))

            pnl = (exit_ - entry) * qty
            if signal.signal_type == "sell":
                pnl = -pnl

            fee = entry * qty * self.fee_rate

            trades.append(
                {
                    "entry_ts": str(df.index[i]),
                    "exit_ts": str(df.index[min(i + 5, len(df) - 1)]),
                    "side": signal.signal_type,
                    "entry_price": round(entry, 2),
                    "exit_price": round(exit_, 2),
                    "qty": qty,
                    "pnl": round(pnl, 2),
                    "fee": round(fee, 2),
                    "reason": signal.reason,
                }
            )

        return BacktestResult(
            trades=trades,
            initial_capital=self.initial_capital,
            fee_rate=self.fee_rate,
        )


# ─────────────────────────────────────────────────────────────
#  STRATEGY 4 — MTF Trend (Multi-Timeframe)
# ─────────────────────────────────────────────────────────────
class MTFTrendAlgo(BaseAlgo):
    """
    Multi-Timeframe Trend algo.

    HTF (1H)  → trend direction
    MTF (15m) → setup confirm
    LTF (5m)  → entry trigger

    Parameters:
        htf_lookback : int = 10   (HTF mein kitne candles peeche)
        mtf_lookback : int = 5    (MTF mein kitne candles peeche)
    """

    name = "mtf_trend"

    def __init__(self, parameters: Optional[dict] = None):
        self.params = parameters or {}
        super().__init__(parameters)
        self.htf_lookback = int(self.get_param("htf_lookback", 10) or 10)
        self.mtf_lookback = int(self.get_param("mtf_lookback", 5) or 5)

    def generate_signal(self, symbol, price, strategy, **ctx) -> AlgoSignal:
        htf = ctx.get("htf") or ctx.get("candles", [])  # fallback for backtest
        mtf = ctx.get("mtf", [])
        ltf = ctx.get("ltf", [])

        # ── Guard: backtest mode mein single candles list aata hai ──
        # Agar mtf/ltf nahi aaya toh htf se hi sab kaam karo (degraded mode)
        if not mtf:
            mtf = htf
        if not ltf:
            ltf = htf

        # ── Minimum candles check ────────────────────────────────
        if len(htf) < self.htf_lookback + 1:
            return AlgoSignal(
                signal_type="hold",
                symbol=symbol,
                price=price,
                reason=f"HTF candles kam hain ({len(htf)} < {self.htf_lookback + 1})",
            )
        if len(mtf) < self.mtf_lookback + 1:
            return AlgoSignal(
                signal_type="hold",
                symbol=symbol,
                price=price,
                reason=f"MTF candles kam hain ({len(mtf)} < {self.mtf_lookback + 1})",
            )
        if len(ltf) < 2:
            return AlgoSignal(
                signal_type="hold",
                symbol=symbol,
                price=price,
                reason="LTF candles kam hain (< 2)",
            )

        # ── Candle accessor (dict ya object dono handle karo) ────
        def c(candle):
            return candle["close"] if isinstance(candle, dict) else candle.close

        # ── 1. HTF: trend direction ───────────────────────────────
        trend = "bullish" if c(htf[-1]) > c(htf[-self.htf_lookback]) else "bearish"

        # ── 2. MTF: setup confirm ─────────────────────────────────
        setup_bull = c(mtf[-1]) > c(mtf[-self.mtf_lookback])
        setup_bear = c(mtf[-1]) < c(mtf[-self.mtf_lookback])

        # ── 3. LTF: entry trigger ─────────────────────────────────
        entry_bull = c(ltf[-1]) > c(ltf[-2])
        entry_bear = c(ltf[-1]) < c(ltf[-2])

        meta = {
            "trend": trend,
            "htf_close": float(c(htf[-1])),
            "mtf_close": float(c(mtf[-1])),
            "ltf_close": float(c(ltf[-1])),
        }

        # ── BUY: teen conditions aligned ─────────────────────────
        if trend == "bullish" and setup_bull and entry_bull:
            return AlgoSignal(
                signal_type="buy",
                symbol=symbol,
                price=price,
                reason=f"MTF confluence BUY | HTF bullish | MTF setup ✓ | LTF entry ✓",
                confidence=75.0,
                metadata=meta,
            )

        # ── SELL: teen conditions aligned ────────────────────────
        if trend == "bearish" and setup_bear and entry_bear:
            return AlgoSignal(
                signal_type="sell",
                symbol=symbol,
                price=price,
                reason=f"MTF confluence SELL | HTF bearish | MTF setup ✓ | LTF entry ✓",
                confidence=75.0,
                metadata=meta,
            )

        return AlgoSignal(
            signal_type="hold",
            symbol=symbol,
            price=price,
            reason=f"No MTF confluence | trend={trend} | setup_bull={setup_bull} | entry_bull={entry_bull}",
            metadata=meta,
        )

from apps.backtest.algos.multi_confirm_options import MultiConfirmOptionsAlgo
register("multi_confirm_options", MultiConfirmOptionsAlgo)


# ── VIX Greeks Expiry Buyer ──────────────────────────────────────────────────
class VixGreeksExpiryBuyerAlgo(BaseAlgo):
    """
    Wrapper for VIX + Greeks + ICT based expiry buyer strategy.
    Runs 1 PM - 3 PM IST, DTE <= 3, VIX < 18.
    """
    def generate_signal(self, symbol: str, price: float, candles: list = None, **kwargs) -> AlgoSignal:
        try:
            from apps.backtest.algos.vix_greeks_expiry_buyer import generate_signal as _gen
            # Live cycle mein htf/mtf/ltf pass hota hai, backtest mein candles
            if candles is None:
                ltf = kwargs.get('ltf', [])
                mtf = kwargs.get('mtf', [])
                candles = ltf if ltf else mtf if mtf else []
            # CandleBar objects → dict convert karo
            def _to_dict(c):
                if isinstance(c, dict):
                    return c
                return {'open': getattr(c,'open',0), 'high': getattr(c,'high',0),
                        'low': getattr(c,'low',0), 'close': getattr(c,'close',0),
                        'volume': getattr(c,'volume',0)}
            candles = [_to_dict(c) for c in candles]
            from apps.options.nse_fetcher import fetch_nse_option_chain
            from apps.options.signal_engine import compute_pcr, find_oi_walls, compute_max_pain
            from apps.options.black_scholes import greeks_from_chain
            from apps.backtest.algos.nse_option_seller import _days_to_expiry

            # Candles split
            candles_5m  = candles[-100:] if len(candles) > 100 else candles
            candles_15m = candles[-50:]  if len(candles) > 50  else candles

            # VIX fetch
            try:
                import yfinance as yf
                vix_data = yf.Ticker("^INDIAVIX").fast_info
                vix = float(vix_data.get("lastPrice", 14.0) or 14.0)
            except Exception:
                vix = float(self.get_param("vix_fallback", 14.0))

            # Option chain
            chain_data = []
            pcr = 1.0
            max_pain = price
            call_wall = 0.0
            put_wall = 0.0
            ce_delta = 0.45
            pe_delta = -0.45
            theta = -5.0
            gamma = 0.003
            iv_rank = None

            try:
                # user inject karo strategy se — authenticated fetch ke liye
                _user = getattr(strategy, 'user', None) if strategy else None
                if _user is None:
                    try:
                        from apps.brokers.models import BrokerAccount
                        acc = BrokerAccount.objects.filter(broker='fyers', is_active=True, is_verified=True).first()
                        _user = acc.user if acc else None
                    except Exception:
                        _user = None
                chain = fetch_nse_option_chain(symbol=symbol, expiry_ts="", user=_user)
                if chain:
                    chain_data = chain.get("chain", [])
                    pcr_data = compute_pcr(chain_data)
                    pcr = pcr_data.get("pcr_oi", 1.0)
                    walls = find_oi_walls(chain_data)
                    call_wall = walls.get("call_wall", 0)
                    put_wall  = walls.get("put_wall", 0)
                    spot = float(chain.get("spot") or price)
                    price = spot
                    max_pain  = compute_max_pain(chain_data) or spot

                    # ATM greeks
                    atm_strike = int(round(spot / 100) * 100)
                    expiries = chain.get("raw_expiry_data", [])
                    expiry_str = expiries[0].get("date", "") if expiries else ""
                    if expiry_str:
                        g = greeks_from_chain(spot, atm_strike, expiry_str)
                        ce_delta = g.get("ce_delta", 0.45)
                        pe_delta = g.get("pe_delta", -0.45)
                        theta    = g.get("theta", -5.0)
                        gamma    = g.get("gamma", 0.003)
            except Exception as e:
                logger.warning("Chain fetch failed for VixGreeks | %s", e)

            # DTE
            dte = _days_to_expiry(symbol)

            result = _gen(
                symbol=symbol, spot=price,
                candles_5m=candles_5m, candles_15m=candles_15m,
                vix=vix, dte=dte, pcr=pcr,
                max_pain=max_pain, call_wall=call_wall, put_wall=put_wall,
                ce_delta=ce_delta, pe_delta=pe_delta,
                theta=theta, gamma=gamma, iv_rank=iv_rank,
                parameters=self.params,
            )

            if result.signal in ('buy_ce', 'buy_pe'):
                sig_type = 'buy' if result.signal == 'buy_ce' else 'sell'
                return AlgoSignal(
                    signal_type=sig_type,
                    symbol=symbol,
                    price=price,
                    confidence=result.score,
                    reason=f"VixGreeks {result.option_type} | score={result.score} | vix={result.vix} | dte={result.dte}",
                    metadata={
                        "option_type": result.option_type,
                        "delta": result.delta,
                        "theta": result.theta,
                        "gamma": result.gamma,
                        "vix": result.vix,
                        "dte": result.dte,
                        "sl_pct": result.sl_pct,
                        "tp_pct": result.tp_pct,
                        "reasons": result.reasons,
                    }
                )
        except Exception as e:
            logger.error("VixGreeksExpiryBuyerAlgo error | %s", e)

        return AlgoSignal(signal_type="hold", symbol=symbol, price=price,
                         reason="VixGreeks: no signal")

register("vix_greeks_expiry_buyer", VixGreeksExpiryBuyerAlgo)
