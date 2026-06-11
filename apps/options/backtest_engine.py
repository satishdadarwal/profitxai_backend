import pandas as pd

from apps.market.models import Candle  # tumhara existing candle model

from .models import BacktestRun
from .services import check_sltp_for_trade, estimate_chain_premium


class BacktestEngine:
    def __init__(self, run: BacktestRun):
        self.run = run
        self.capital = run.initial_capital
        self.trades = []

    def execute(self) -> dict:
        # 15m candles fetch karo DB se
        candles = (
            Candle.objects.filter(
                symbol=self.run.symbol.fyers_symbol,
                resolution="15",
                time__date__gte=self.run.from_date,
                time__date__lte=self.run.to_date,
            )
            .order_by("time")
            .values("time", "open", "high", "low", "close", "volume")
        )

        df = pd.DataFrame(candles)
        if df.empty:
            raise ValueError("No candle data found for this period")

        # Strategy signals generate karo (tumhara MTF logic)
        signals = self._generate_signals(df)

        # Har signal pe virtual trade place karo
        for sig in signals:
            self._place_virtual_trade(sig, df)

        return self._calculate_results()

    def _generate_signals(self, df: pd.DataFrame) -> list:
        """
        Tumhara MTFAnalyser logic yahan port karo.
        Return: [{'time': ..., 'direction': 'CE'/'PE',
                  'spot': ..., 'strike': ...}]
        """
        signals = []
        # TODO: MTFAnalyser.analyse() ka Python port yahan
        return signals

    def _place_virtual_trade(self, signal: dict, df: pd.DataFrame):
        entry_premium = estimate_chain_premium(
            signal["spot"], signal["strike"], signal["direction"]
        )
        sl = entry_premium * 0.5  # 50% SL
        tp = entry_premium * 2.0  # 2x TP
        qty = self.run.symbol.lot_size

        # Forward walk karo aur SL/TP dekho
        future = df[df["time"] > signal["time"]].head(30)  # max 30 candles
        exit_price = entry_premium
        exit_reason = "Expiry"

        for _, row in future.iterrows():
            curr_premium = estimate_chain_premium(
                row["close"], signal["strike"], signal["direction"]
            )
            result = check_sltp_for_trade_values(curr_premium, sl, tp, "buy")
            if result:
                exit_price = result["exit_price"]
                exit_reason = result["reason"]
                break

        pnl = (exit_price - entry_premium) * qty
        self.capital += pnl
        self.trades.append(
            {
                "entry": entry_premium,
                "exit": exit_price,
                "pnl": pnl,
                "reason": exit_reason,
            }
        )

    def _calculate_results(self) -> dict:
        if not self.trades:
            return {
                "final_capital": self.capital,
                "total_pnl": 0,
                "win_rate": 0,
                "max_drawdown": 0,
                "total_trades": 0,
            }

        wins = sum(1 for t in self.trades if t["pnl"] > 0)
        total_pnl = sum(t["pnl"] for t in self.trades)

        # Max drawdown
        running = self.run.initial_capital
        peak = running
        max_dd = 0
        for t in self.trades:
            running += t["pnl"]
            peak = max(peak, running)
            max_dd = max(max_dd, (peak - running) / peak * 100)

        return {
            "final_capital": self.capital,
            "total_pnl": total_pnl,
            "win_rate": wins / len(self.trades) * 100,
            "max_drawdown": max_dd,
            "total_trades": len(self.trades),
        }


def check_sltp_for_trade_values(price, sl, tp, action):
    if action == "buy":
        if price <= sl:
            return {"reason": "SL", "exit_price": sl}
        if price >= tp:
            return {"reason": "TP", "exit_price": tp}
    return None
