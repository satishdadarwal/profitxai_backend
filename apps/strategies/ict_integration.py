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
        # Redis cache — 5 min tak same candles reuse karo (429 fix)
        try:
            from django.core.cache import cache
            import hashlib
            _key_str = symbol + "_" + timeframe + "_" + str(bars)
            cache_key = "fyers_candles_" + hashlib.md5(_key_str.encode()).hexdigest()
            cached = cache.get(cache_key)
            if cached is not None:
                import pickle
                return pickle.loads(cached)
        except Exception:
            cache_key = None
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

    def __init__(self, user, instrument_type: str = "futures", trader_type: str = "buyer", sl_pct: float = 20, tp_pct: float = 40):
        self.instrument_type = instrument_type
        self.trader_type = trader_type
        self._sl_pct = sl_pct
        self._tp_pct = tp_pct
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
            from apps.strategies.fyers_utils import get_atm_option_symbol
            # Options ke liye ATM symbol generate karo
            instr_type = self.instrument_type
            if instr_type == "options":
                direction = order.direction.value  # "long" or "short"
                trader_type = self.trader_type  # "buyer" or "seller"
                if trader_type == "buyer":
                    # Buyer: Bearish→PE Buy, Bullish→CE Buy
                    option_type = "CE" if direction == "long" else "PE"
                    side = 1  # Always BUY
                else:
                    # Seller: Bearish→CE Sell, Bullish→PE Sell
                    option_type = "PE" if direction == "long" else "CE"
                    side = -1  # Always SELL
                fyers_sym = get_atm_option_symbol(
                    symbol=order.symbol,
                    current_price=float(order.price),
                    option_type=option_type,
                    user=self.user,
                )
                logger.info("Options symbol=%s | direction=%s | trader=%s | side=%s",
                    fyers_sym, direction, trader_type, "BUY" if side == 1 else "SELL")
            # ── Options: place_live_option_trade() use karo ──────────────
            if instr_type == "options" and side == 1:
                try:
                    from apps.options.services import place_live_option_trade
                    base_sym = order.symbol.upper().replace("NSE:", "").replace("BSE:", "")
                    spot_price = float(order.price) if order.price else 0
                    sl_price = round(spot_price * (1 - self._sl_pct / 100), 2)
                    tp_price = round(spot_price * (1 + self._tp_pct / 100), 2)
                    result = place_live_option_trade(
                        user=self.user,
                        symbol_name=base_sym,
                        option_type=option_type,
                        action="buy",
                        lots=1,
                        spot=spot_price,
                        entry_price=spot_price,
                        stop_loss=sl_price,
                        target_price=tp_price,
                        setup_type=f"ICT-Auto",
                        timeframe="15",
                        strategy=None,
                    )
                    broker_id = result.get("broker_order_id", "unknown")
                    logger.info("place_live_option_trade result: %s", result)
                    return str(broker_id)
                except Exception as live_err:
                    logger.error("place_live_option_trade error: %s", live_err)
            else:
                fyers_sym = normalize_for_fyers(order.symbol)
                side = 1 if order.direction.value == "long" else -1

            LOT_SIZES = {
                "NIFTY": 75, "BANKNIFTY": 30, "FINNIFTY": 65,
                "MIDCPNIFTY": 120, "SENSEX": 20, "BANKEX": 20,
            }
            base_sym = order.symbol.upper().replace("NSE:", "").replace("BSE:", "")
            lot_size = LOT_SIZES.get(base_sym, 1)
            qty = lot_size  # 1 lot
            logger.info("Options qty | lot_size=%d | symbol=%s", lot_size, base_sym)
            _order_params = {
                "symbol": fyers_sym,
                "qty": qty,
                "type": 2,
                "side": side,
                "productType": "INTRADAY",
                "validity": "DAY",
                "offlineOrder": False,
                "stopLoss": 0,
                "takeProfit": 0,
            }
            result = cast(dict, fyers.place_order(data=_order_params))
            broker_id = result.get("id") or result.get("orderNumber") or result.get("order_id", "unknown")
            logger.info("Live order placed | broker_id=%s | full_result=%s", broker_id, result)

            # ── Duplicate Check — same symbol ka open order hai? ────────
            try:
                from fyers_apiv3 import fyersModel as _fm
                _fyers_check = _fm.FyersModel(
                    client_id=account.app_id,
                    token=account.access_token,
                    log_path="", is_async=False,
                )
                positions = _fyers_check.positions()
                open_syms = [
                    p.get("symbol", "") 
                    for p in positions.get("netPositions", [])
                    if p.get("netQty", 0) != 0
                ]
                if fyers_sym in open_syms:
                    logger.warning("Duplicate skip | %s already open", fyers_sym)
                    return "duplicate_skipped"
            except Exception as dup_err:
                logger.warning("Duplicate check error: %s", dup_err)

            # ── Margin Check — enough balance hai? ───────────────────────
            try:
                funds = _fyers_check.funds()
                available = float(funds.get("fund_limit", [{}])[0].get("equityAmount", 0))
                # Options buying mein margin = premium × qty × 1.1 (10% buffer)
                required = float(order.price or 0) * qty * 1.1
                if required > 0 and available < required:
                    logger.warning(
                        "Margin insufficient | required=%.2f | available=%.2f | skipping",
                        required, available
                    )
                    return "margin_insufficient"
            except Exception as margin_err:
                logger.warning("Margin check error: %s", margin_err)

            # ── GTT SL/Target order lagao ────────────────────────────────
            if broker_id and broker_id != "unknown" and instr_type == "options":
                try:
                    # Market order mein price=0 hota hai, LTP se lo
                    entry_price = float(order.price) if (order.price and float(order.price) > 0) else 0
                    if entry_price == 0:
                        try:
                            ltp_data = fyers.fyers.quotes(data={"symbols": fyers_sym})
                            entry_price = float(ltp_data["d"][0]["v"]["lp"])
                            logger.info("Entry price from LTP: %.2f", entry_price)
                        except Exception as ltp_err:
                            logger.warning("LTP fetch failed: %s", ltp_err)
                    sl_pct = float(getattr(self, "_sl_pct", 20)) / 100
                    tp_pct = float(getattr(self, "_tp_pct", 40)) / 100
                    if entry_price > 0:
                        sl_price = round(entry_price * (1 - sl_pct), 2)
                        tp_price = round(entry_price * (1 + tp_pct), 2)

                        # OCO GTT — leg1=Target (above LTP), leg2=SL (below LTP)
                        gtt_result = fyers.fyers.place_gtt_order(data={
                            "side": -1,
                            "symbol": fyers_sym,
                            "productType": "INTRADAY",
                            "orderInfo": {
                                "leg1": {
                                    "price": tp_price,
                                    "triggerPrice": tp_price,
                                    "qty": qty,
                                },
                                "leg2": {
                                    "price": round(sl_price * 0.98, 2),
                                    "triggerPrice": sl_price,
                                    "qty": qty,
                                },
                            },
                        })
                        logger.info("OCO GTT placed | sl=%.2f | tp=%.2f | result=%s", sl_price, tp_price, gtt_result)

                except Exception as gtt_err:
                    logger.warning("GTT failed: %s", gtt_err)
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


# ─── 4b. Delta Execution Adapter ─────────────────────────────────────────────
class DeltaExecutionAdapter(ExecutionAdapter):
    """
    Live mode — Delta Exchange India pe actual order place karta hai.
    BTC-USDT / ETH-USDT → _call_delta_api ke through order jaata hai.
    ✅ FIX: Crypto symbols ke liye FyersExecutionAdapter ki jagah yeh use karo.

    ✅ SECURITY FIX: Sirf tabhi order jaega jab user ka apna Delta broker account
    ho. Dusre user ka account ya Fyers-only user ka Delta pe order NAHI jaega.
    """

    def __init__(self, user, broker_account=None):
        self.user = user
        # Pre-validated broker account pass karo — agar None hai toh place_order fail karega
        self._broker_account = broker_account

    async def place_order(self, order) -> str:
        from asgiref.sync import sync_to_async
        return await sync_to_async(self._place_order_sync)(order)

    def _place_order_sync(self, order) -> str:
        try:
            from apps.strategies.signal_router import (
                _call_delta_api,
                _to_delta_symbol,
            )

            # ✅ SECURITY FIX: Pre-validated account use karo — fresh DB query nahi
            # execute_cycle_ict() mein already validate ho chuka hai ki:
            #   1. User ka Delta broker account hai
            #   2. is_active=True, is_verified=True
            #   3. account.user == strategy.user (ownership verified)
            account = self._broker_account
            if not account:
                raise RuntimeError(
                    f"No active Delta account for user {self.user.pk}. "
                    f"Fix: Admin panel mein is user ke liye Delta broker account add karo."
                )

            # DUPLICATE GUARD: already open position hai iss symbol+user+mode pe?
            from apps.orders.models import Position
            from django.apps import apps
            Asset = [m for m in apps.get_models() if m.__name__ == 'Asset'][0]
            _side = "buy" if order.direction.value in ("long", "buy") else "sell"
            _mode = getattr(order, '_mode', 'live')
            try:
                _asset = Asset.objects.filter(symbol=order.symbol).first()
                if _asset:
                    _existing = Position.objects.filter(
                        user=self.user,
                        asset=_asset,
                        mode=_mode,
                        status='open',
                        side=_side,
                    ).first()
                    if _existing:
                        logger.warning(
                            "DUPLICATE BLOCKED | Delta | symbol=%s | mode=%s | "
                            "existing_position=%s opened_at=%s",
                            order.symbol, _mode, _existing.id, _existing.opened_at,
                        )
                        return "duplicate_skipped"
            except Exception as _de:
                logger.warning("Duplicate check failed (non-fatal): %s", _de)

            # Direction: long → buy, short → sell
            side = "buy" if order.direction.value in ("long", "buy") else "sell"

            # instrument_type — strategy se milega agar available ho
            product_type = "perp"  # default perpetual

            # ✅ FIX: risk_config.qty se cap lagao — ICT engine position_size bahut bada ho sakta hai
            from apps.strategies.models import Strategy as _S
            _strat = getattr(order, '_strategy', None)
            _max_qty = 1
            try:
                if _strat:
                    _max_qty = int(_strat.risk_config.get('qty', 1))
            except Exception:
                pass
            _safe_qty = min(int(order.size) or 1, _max_qty)

            resp = _call_delta_api(
                account=account,
                symbol=order.symbol,
                side=side,
                qty=_safe_qty,
                product_type=product_type,
                current_price=float(order.price),
                sl_price=float(order.stop_loss) if order.stop_loss else None,
                tp_price=float(order.take_profit) if order.take_profit else None,
            )

            # Delta API response: {"success": true, "result": {"id": 12345}}
            if resp.get("success"):
                broker_id = str(resp.get("result", {}).get("id", "unknown"))
            else:
                err_msg = resp.get("error", {}).get("message", str(resp))
                logger.error(
                    "Delta order FAILED | symbol=%s | side=%s | err=%s",
                    order.symbol, side, err_msg,
                )
                raise RuntimeError(f"Delta order rejected: {err_msg}")

            logger.info(
                "Delta live order placed | broker_id=%s | symbol=%s | side=%s | qty=%s",
                broker_id, order.symbol, side, int(order.size),
            )
            return broker_id

        except Exception as e:
            logger.error("DeltaExecutionAdapter.place_order error: %s", e)
            raise

    async def cancel_order(self, broker_order_id: str) -> bool:
        return True

    async def get_account_balance(self) -> float:
        return 0.0

    async def get_positions(self):
        return []


# ─── 5. execute_cycle_ict ─────────────────────────────────────────────────────
def _get_dry_run(strategy):
    """User ke preferred_mode se dry_run decide karo."""
    try:
        from apps.strategies.models import UserStrategyPreference
        subscriber = getattr(strategy, '_subscriber_user', None) or getattr(strategy, 'user', None)
        if subscriber:
            real_id = getattr(strategy, '_real', strategy).id
            pref = UserStrategyPreference.objects.get(user=subscriber, strategy_id=real_id)
            return pref.preferred_mode != 'live'
    except Exception:
        pass
    return strategy.mode == "paper"


def execute_cycle_ict(strategy, symbol: str) -> dict:
    """
    ICT MTF analysis + signal dispatch — ek cycle.
    strategies/services.py ke execute_cycle() mein call karo.

    ✅ SECURITY FIX: Executor banane se PEHLE broker account validate karo.
    Agar user ka woh broker nahi hai jiska symbol hai, toh cycle bilkul nahi chalegi.
    - Fyers-only user → crypto cycle skip
    - Delta-only user → NSE cycle skip
    - Koi bhi user dusre ka account use nahi kar sakta
    """
    from apps.brokers.models import BrokerAccount
    from apps.ict_engine.dispatcher import Dispatcher

    # ✅ FIX: DB se fresh strategy reload karo
    try:
        from apps.strategies.models import Strategy as _SM
        strategy = _SM.objects.get(pk=strategy.pk)
    except Exception:
        pass

    config = RunnerConfig(
        timeframes=["1D", "4H", "1H", "15m"],
        anchor_tf="1D",
        execution_tf="15m",
        min_confluence=strategy.parameters.get("min_confluence", 60.0),
        min_rr=strategy.parameters.get("min_rr", 2.0),
        dry_run=_get_dry_run(strategy),
        bars_per_tf=strategy.parameters.get("bars_per_tf", 300),
    )

    # Auto-detect provider
    crypto = _is_crypto(symbol)
    if crypto:
        provider = DeltaDataProvider()
    else:
        provider = FyersDataProvider(user=strategy.user)

    db_adapter = DjangoDatabaseAdapter(strategy=strategy)
    ws_adapter = DjangoWebSocketAdapter(strategy=strategy)

    # ✅ SECURITY FIX: Executor banane se PEHLE broker account validate karo.
    # Live mode mein sirf tabhi order jayega jab user ka apna correct broker account ho.
    if strategy.mode == "live":
        if crypto:
            # ── Delta account check ──────────────────────────────────────────
            delta_account = BrokerAccount.objects.filter(
                user=strategy.user,
                broker="delta",
                is_active=True,
                is_verified=True,
            ).first()

            if not delta_account:
                logger.warning(
                    "⛔ execute_cycle_ict: user=%s ke paas active Delta account nahi hai "
                    "— crypto cycle '%s' skip. "
                    "Fix: Admin panel mein Delta account add karo.",
                    strategy.user.pk, symbol,
                )
                return _null_signal(symbol)

            # Ownership double-check (defensive)
            if delta_account.user_id != strategy.user.pk:
                logger.critical(
                    "🚨 SECURITY: Delta account %s belongs to user %s "
                    "but strategy %s belongs to user %s — BLOCKING.",
                    delta_account.id, delta_account.user_id,
                    strategy.id, strategy.user.pk,
                )
                return _null_signal(symbol)

            executor = DeltaExecutionAdapter(user=strategy.user, broker_account=delta_account)

        else:
            # ── Fyers account check ──────────────────────────────────────────
            fyers_account = BrokerAccount.objects.filter(
                user=strategy.user,
                broker="fyers",
                is_active=True,
                is_verified=True,
            ).first()

            if not fyers_account:
                logger.warning(
                    "⛔ execute_cycle_ict: user=%s ke paas active Fyers account nahi hai "
                    "— NSE cycle '%s' skip. "
                    "Fix: Admin panel mein Fyers account add karo.",
                    strategy.user.pk, symbol,
                )
                return _null_signal(symbol)

            if fyers_account.user_id != strategy.user.pk:
                logger.critical(
                    "🚨 SECURITY: Fyers account %s belongs to user %s "
                    "but strategy %s belongs to user %s — BLOCKING.",
                    fyers_account.id, fyers_account.user_id,
                    strategy.id, strategy.user.pk,
                )
                return _null_signal(symbol)

            executor = FyersExecutionAdapter(
                user=strategy.user,
                instrument_type=getattr(strategy, "instrument_type", "futures"),
                trader_type=strategy.risk_config.get("trader_type", "buyer"),
                sl_pct=strategy.risk_config.get("sl_pct", 20),
                tp_pct=strategy.risk_config.get("target_pct", 40),
            )
    else:
        executor = PaperExecutionAdapter()

    dispatcher = Dispatcher(
        db=db_adapter,
        ws=ws_adapter,
        executor=executor,
        dry_run=config.dry_run,
    )

    # ✅ FIX: risk_config.qty se max position size cap karo
    _max_qty = int(strategy.risk_config.get("qty", 1))
    _risk_pct = float(strategy.risk_config.get("risk_per_trade_pct", 1.0))
    _capital = float(strategy.risk_config.get("capital", 10000))
    _risk_params = RiskParameters(
        account_balance=_capital,
        risk_per_trade_pct=_risk_pct,
    )
    runner = StrategyRunner(
        provider=provider,
        dispatcher=dispatcher,
        config=config,
        risk_params=_risk_params,
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

    # ✅ FIX: Paper mode → directly PaperTrade DB record banao
    # PaperExecutionAdapter sirf in-memory dry_run karta hai — actual DB record
    # nahi banta, isliye Auto Paper dashboard pe 0 trades dikhta hai.
    # open_trade() directly call karo — yeh proper PaperTrade record banata hai
    # jisko Flutter app ka Auto Paper page fetch kar sake.
    paper_order = None
    if strategy.mode == "paper" and sig.direction is not None:
        from apps.orders.models import Order as _Order
        _trade_user = getattr(strategy, "_subscriber_user", None) or strategy.user

        # Guard: skip if an open paper position already exists for this user+symbol.
        # Without this, every Celery cycle that sees a qualifying signal creates a
        # fresh Order — the downstream _handle_ict_signal guard can't catch them
        # because orders created here had strategy_id=None (now fixed below too).
        _already_open = _Order.objects.filter(
            user=_trade_user,
            mode=_Order.Mode.PAPER,
            status__in=[_Order.Status.OPEN, _Order.Status.PARTIAL],
            symbol_display__iexact=sig.symbol,
        ).exists()

        if _already_open:
            logger.info(
                "⏭ Paper trade skipped — position already open | "
                "user=%s | symbol=%s | strategy=%s",
                _trade_user.pk, sig.symbol, strategy.id,
            )
        else:
            try:
                from apps.paper_trading.services import open_trade

                entry  = float(sig.entry_price)
                sl     = sig.stop_loss    or round(entry * 0.99, 2)
                tp     = sig.take_profit_1 or round(entry * 1.02, 2)
                size   = 1  # Paper mode: 1 lot to stay within margin limits

                instr = getattr(strategy, "instrument_type", "crypto")
                symbol_up = sig.symbol.upper()
                if any(k in symbol_up for k in ("BTC", "ETH", "SOL", "USDT", "BNB", "XRP")):
                    asset_type = "crypto"
                elif instr in ("futures", "perp"):
                    asset_type = "futures"
                elif instr == "options":
                    asset_type = "option"
                else:
                    asset_type = instr or "crypto"

                direction = sig.direction.value  # "long"/"short" → "buy"/"sell"
                side = "buy" if direction in ("long", "buy") else "sell"

                trade_data = {
                    "symbol":          sig.symbol,
                    "asset_type":      asset_type,
                    "instrument_type": instr,
                    "display_name":    sig.symbol,
                    "side":            side,
                    "quantity":        size,
                    "lot_size":        1,
                    "leverage":        1,
                    "entry_price":     entry,
                    "stop_loss":       sl,
                    "target_price":    tp,
                    "setup_type":      f"Auto-{getattr(strategy, 'algo_name', 'ict')}",
                    "strategy_id":     str(strategy.id),
                }

                paper_order = open_trade(_trade_user, trade_data)
                # Link order to strategy so _handle_ict_signal's guard can find it
                # on the next cycle (place_order() creates with strategy=None by default).
                if paper_order and getattr(strategy, "id", None):
                    paper_order.strategy_id = strategy.id
                    paper_order.save(update_fields=["strategy_id", "updated_at"])
                logger.info(
                    "✅ Paper trade created | id=%s | symbol=%s | side=%s | entry=%.4f | "
                    "sl=%.4f | tp=%.4f | strategy=%s",
                    paper_order.id if paper_order else "?",
                    sig.symbol, side, entry, sl, tp, strategy.id,
                )
            except Exception as _pe:
                logger.error(
                    "❌ Paper trade creation failed | strategy=%s | symbol=%s | err=%s",
                    strategy.id, sig.symbol, _pe, exc_info=True,
                )

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
        "result": "executed" if paper_order else "skipped",
        "order": paper_order,
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
    # ✅ FIX: candle_service use karo — Delta/Fyers auto-detect
    from apps.common.candle_service import fetch_candles
    from_dt = datetime.datetime.strptime(from_date, "%Y-%m-%d")
    to_dt = datetime.datetime.strptime(to_date, "%Y-%m-%d")
    from_ts = int(from_dt.timestamp())
    to_ts = int(datetime.datetime.combine(to_dt, datetime.time.max).timestamp())
    sym = strategy.symbol
    tf_str = str(timeframe).replace("m", "").replace("h", "0")
    candle_bars = fetch_candles(
        symbol=sym,
        timeframe=tf_str,
        from_ts=from_ts,
        to_ts=to_ts,
        source="auto",
        strategy=strategy,
    )
    if not candle_bars:
        raise RuntimeError(f"No candle data for {sym}")
    if len(candle_bars) < 50:
        raise RuntimeError(f"Insufficient candle data: {len(candle_bars)} bars")
    candle_bars = candle_bars[-1000:]
    candles_raw = [[c.timestamp, c.open, c.high, c.low, c.close, c.volume]
                   for c in candle_bars]

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