from decimal import Decimal

from apps.backtest.engine import AlgoSignal, BaseAlgo


class MTFTrendAlgo(BaseAlgo):
    """
    Multi-Timeframe Trend algo.

    Logic:
        HTF (1H)  → trend direction  (bullish / bearish)
        MTF (15m) → setup confirm    (recent candle > N candles back)
        LTF (5m)  → entry trigger    (last candle closes above previous)
    """

    def __init__(self, parameters: dict):
        self.htf_lookback = int(parameters.get("htf_lookback", 10))
        self.mtf_lookback = int(parameters.get("mtf_lookback", 5))

    def generate_signal(
        self,
        symbol,
        price,
        strategy,
        htf=None,
        mtf=None,
        ltf=None,
        candles=None,  # ignored — MTF algo teen TFs use karta hai
        quote=None,
    ) -> AlgoSignal:

        # ── Guard: minimum candles check ─────────────────────────
        if not htf or len(htf) < self.htf_lookback + 1:
            return AlgoSignal(
                signal_type="hold",
                symbol=symbol,
                price=price,
                reason="Insufficient HTF candles",
                result="skipped",
            )
        if not mtf or len(mtf) < self.mtf_lookback + 1:
            return AlgoSignal(
                signal_type="hold",
                symbol=symbol,
                price=price,
                reason="Insufficient MTF candles",
                result="skipped",
            )
        if not ltf or len(ltf) < 2:
            return AlgoSignal(
                signal_type="hold",
                symbol=symbol,
                price=price,
                reason="Insufficient LTF candles",
                result="skipped",
            )

        # ── 1. HTF: trend direction ───────────────────────────────
        # Candle dict ya object — dono handle karo
        htf_close = lambda c: c["close"] if isinstance(c, dict) else c.close
        mtf_close = lambda c: c["close"] if isinstance(c, dict) else c.close
        ltf_close = lambda c: c["close"] if isinstance(c, dict) else c.close

        trend = (
            "bullish"
            if htf_close(htf[-1]) > htf_close(htf[-self.htf_lookback])
            else "bearish"
        )

        # ── 2. MTF: setup confirm ─────────────────────────────────
        setup_bullish = mtf_close(mtf[-1]) > mtf_close(mtf[-self.mtf_lookback])
        setup_bearish = mtf_close(mtf[-1]) < mtf_close(mtf[-self.mtf_lookback])

        # ── 3. LTF: entry trigger ─────────────────────────────────
        entry_bullish = ltf_close(ltf[-1]) > ltf_close(ltf[-2])
        entry_bearish = ltf_close(ltf[-1]) < ltf_close(ltf[-2])

        # ── Decision ─────────────────────────────────────────────
        if trend == "bullish" and setup_bullish and entry_bullish:
            return AlgoSignal(
                signal_type="buy",
                symbol=symbol,
                price=price,
                reason=(
                    f"HTF bullish | MTF setup ✓ | LTF entry ✓ "
                    f"| htf_close={htf_close(htf[-1]):.2f}"
                ),
                confidence=0.75,
                metadata={
                    "trend": trend,
                    "htf_close": float(htf_close(htf[-1])),
                    "mtf_close": float(mtf_close(mtf[-1])),
                    "ltf_close": float(ltf_close(ltf[-1])),
                },
            )

        if trend == "bearish" and setup_bearish and entry_bearish:
            return AlgoSignal(
                signal_type="sell",
                symbol=symbol,
                price=price,
                reason=(
                    f"HTF bearish | MTF setup ✓ | LTF entry ✓ "
                    f"| htf_close={htf_close(htf[-1]):.2f}"
                ),
                confidence=0.75,
                metadata={
                    "trend": trend,
                    "htf_close": float(htf_close(htf[-1])),
                    "mtf_close": float(mtf_close(mtf[-1])),
                    "ltf_close": float(ltf_close(ltf[-1])),
                },
            )

        return AlgoSignal(
            signal_type="hold",
            symbol=symbol,
            price=price,
            reason=f"No MTF confluence | trend={trend}",
            result="skipped",
        )
