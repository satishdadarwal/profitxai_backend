# apps/live_trading/screener_pipeline.py
from __future__ import annotations
import logging
from decimal import Decimal
from django.utils import timezone

logger = logging.getLogger(__name__)
SEMI_TIMEOUT_SECONDS = 60


class ScreenerSignalPipeline:

    def process(self, user, signals: list[dict]) -> dict:
        if not signals:
            return {"processed": 0}

        pref = self._get_preference(user)
        results = {"processed": 0, "executed": 0, "notified": 0, "skipped": 0}

        for sig in signals:
            try:
                market_type = sig.get("market_type", "indian")

                if market_type == "crypto" and not pref.crypto_enabled:
                    results["skipped"] += 1
                    continue
                if market_type == "indian" and not pref.options_enabled:
                    results["skipped"] += 1
                    continue

                if pref.execution_mode == "auto":
                    self._execute(user, sig, pref)
                    results["executed"] += 1
                elif pref.execution_mode == "semi":
                    self._notify(user, sig, pref, mode="semi")
                    results["notified"] += 1
                else:  # manual
                    self._notify(user, sig, pref, mode="manual")
                    results["notified"] += 1

                results["processed"] += 1

            except Exception as e:
                logger.error(
                    "ScreenerPipeline error | user=%s | symbol=%s | %s",
                    user.pk, sig.get("symbol"), e, exc_info=True,
                )

        return results

    # ── Execute ───────────────────────────────────────────────────

    def _execute(self, user, sig: dict, pref):
        if pref.trading_mode == "paper":
            self._execute_paper(user, sig, pref)
        else:
            self._execute_live(user, sig, pref)

    def _execute_paper(self, user, sig: dict, pref):
        from apps.paper_trading.services import open_trade

        market_type = sig.get("market_type", "indian")
        symbol      = sig["symbol"]
        direction   = sig["direction"]
        side        = "buy" if direction in ("long", "bullish") else "sell"

        if market_type == "crypto":
            # ── Crypto paper trade ────────────────────────────────
            data = {
                "symbol":      symbol,
                "asset_type":  "crypto",
                "side":        side,
                "entry_price": sig["entry_price"],
                "stop_loss":   sig["stop_loss"],
                "target_price": sig["take_profit_1"],
                "risk_pct":    pref.risk_pct,
                "leverage":    pref.leverage,
                "lot_size":    1,
                "option_type": "NA",
            }
            trade = open_trade(user, data)
            logger.info("Paper crypto trade | %s | %s | id=%s", symbol, side, trade.id)
            self._ws_push(user, sig, event="trade_placed", trade_id=str(trade.id))

        else:
            # ── Options paper trade ───────────────────────────────
            try:
                from apps.strategies.fyers_utils import get_atm_option_symbol

                # Buyer: bullish → CE, bearish → PE
                # Seller: bullish → PE sell, bearish → CE sell
                trader_type = "buyer"  # default, pref mein add kar sakte hain baad mein
                if side == "buy":
                    option_type = "CE" if trader_type == "buyer" else "PE"
                    action = "buy"
                else:
                    option_type = "PE" if trader_type == "buyer" else "CE"
                    action = "buy"

                spot_price = sig["entry_price"]
                option_symbol = get_atm_option_symbol(
                    base_symbol=symbol,
                    spot_price=spot_price,
                    option_type=option_type,
                )

                LOT_SIZES = {
                    "NIFTY": 65, "BANKNIFTY": 30, "FINNIFTY": 60,
                    "MIDCPNIFTY": 120, "SENSEX": 10,
                }
                lot_size = LOT_SIZES.get(symbol.upper(), 65)

                data = {
                    "symbol":       option_symbol,
                    "asset_type":   "options",
                    "side":         action,
                    "entry_price":  sig["entry_price"],
                    "stop_loss":    sig["stop_loss"],
                    "target_price": sig["take_profit_1"],
                    "risk_pct":     pref.risk_pct,
                    "leverage":     1,
                    "lot_size":     lot_size,
                    "option_type":  option_type,
                    "strike_price": spot_price,
                }
                trade = open_trade(user, data)
                logger.info(
                    "Paper options trade | %s %s | %s | id=%s",
                    option_symbol, option_type, action, trade.id
                )
                self._ws_push(user, sig, event="trade_placed", trade_id=str(trade.id))

            except Exception as e:
                logger.error("Options paper trade failed | %s | %s", symbol, e)
                self._ws_push(user, sig, event="trade_failed", reason=str(e))

    def _execute_live(self, user, sig: dict, pref):
        """Live broker order — Delta ya Fyers."""
        from apps.brokers.models import BrokerAccount
        from apps.strategies.signal_router import _place_order_via_broker

        market_type = sig.get("market_type", "indian")
        broker_slug = "delta" if market_type == "crypto" else "fyers"
        instrument  = "perp" if market_type == "crypto" else "options"

        account = BrokerAccount.objects.filter(
            user=user, broker=broker_slug,
            is_active=True, is_verified=True,
        ).first()

        if not account:
            logger.warning("No %s account | user=%s", broker_slug, user.pk)
            self._ws_push(user, sig, event="trade_failed",
                          reason=f"No active {broker_slug} account")
            return

        signal_obj    = _DictSignal(sig)
        strategy_proxy = _ScreenerStrategyProxy(user, sig, account, instrument)

        _place_order_via_broker(
            strategy=strategy_proxy,
            signal=signal_obj,
            user=user,
            account=account,
            broker_slug=broker_slug,
            instrument_type=instrument,
        )
        self._ws_push(user, sig, event="trade_placed")

    # ── Notify ────────────────────────────────────────────────────

    def _notify(self, user, sig: dict, pref, mode: str):
        """SEMI / MANUAL — LiveSignal PENDING save + WS alert."""
        from apps.live_trading.models import LiveSignal, TradingSession
        from decimal import Decimal
        from django.utils import timezone
        import datetime

        # Active session get or create
        session, _ = TradingSession.objects.get_or_create(
            user=user, is_active=True,
            defaults={"mode": "paper"},
        )

        # LiveSignal PENDING status mein save karo
        signal_obj, created = LiveSignal.objects.get_or_create(
            user=user,
            symbol=sig["symbol"],
            direction=sig["direction"],
            entry_price=Decimal(str(sig["entry_price"])),
            status="pending",
            defaults=dict(
                session=session,
                signal_type=sig.get("setup_type", "ICT"),
                strength=min(100, int(sig.get("confluence", 60))),
                stop_loss=Decimal(str(sig["stop_loss"])),
                take_profit=Decimal(str(sig["take_profit_1"])),
                rr_ratio=Decimal(str(sig.get("risk_reward", 2.0))),
                lots=Decimal(str(sig.get("position_size", 1.0))),
                mode=pref.trading_mode,
                raw_payload=sig,
                expires_at=timezone.now() + datetime.timedelta(
                    seconds=SEMI_TIMEOUT_SECONDS if mode == "semi" else 3600
                ),
            ),
        )

        self._ws_push(
            user, sig,
            event="screener_signal",
            execution_mode=mode,
            trading_mode=pref.trading_mode,
            confirm_timeout=SEMI_TIMEOUT_SECONDS if mode == "semi" else 0,
            signal_id=signal_obj.id,
        )
        logger.info("ScreenerPipeline notify [%s] | user=%s | %s | signal_id=%s",
                    mode, user.pk, sig.get("symbol"), signal_obj.id)

    # ── WS Push ───────────────────────────────────────────────────

    def _ws_push(self, user, sig: dict, event: str, **extra):
        try:
            from asgiref.sync import async_to_sync
            from channels.layers import get_channel_layer

            layer  = get_channel_layer()
            group  = f"user_{user.id}"
            symbol = sig["symbol"].upper()
            is_crypto    = sig.get("market_type") == "crypto"
            delta_symbol = symbol.replace("USDT", "-USDT") if is_crypto else None

            payload = {
                "type":         "new_signal",
                "event":        event,
                "direction":    sig["direction"],
                "symbol":       sig["symbol"],
                "delta_symbol": delta_symbol,
                "market_type":  sig.get("market_type", "indian"),
                "entry":        sig["entry_price"],
                "sl":           sig["stop_loss"],
                "target1":      sig["take_profit_1"],
                "tp":           sig["take_profit_1"],
                "confidence":   sig.get("confluence", 0),
                "reason":       sig.get("notes", ""),
                "grade":        sig.get("grade", "B"),
                "grade_emoji":  sig.get("grade_emoji", "🔵"),
                "setup":        sig.get("setup_type", "ICT"),
                "rr":           sig.get("risk_reward", 0),
                "position":     sig.get("position_size", 0),
                "risk_inr":     sig.get("risk_amount", 0),
                "tags":         sig.get("tags", []),
                "breakdown":    sig.get("breakdown", {}),
                "strategy":     "ICT_SCREENER",
                "qty":          sig.get("position_size", 0.01),
                "leverage":     10,
                **extra,
            }
            async_to_sync(layer.group_send)(group, payload)

        except Exception as e:
            logger.error("ScreenerPipeline WS push failed: %s", e)

    # ── Preference ────────────────────────────────────────────────

    def _get_preference(self, user):
        from apps.strategies.models import UserScreenerPreference
        pref, _ = UserScreenerPreference.objects.get_or_create(
            user=user,
            defaults={
                "execution_mode":  "semi",
                "trading_mode":    "paper",
                "options_enabled": True,
                "crypto_enabled":  True,
                "risk_pct":        1.0,
                "leverage":        10,
            },
        )
        return pref


# ── Helper classes ────────────────────────────────────────────────

class _DictSignal:
    def __init__(self, d: dict):
        self.symbol      = d.get("symbol", "")
        self.direction   = d.get("direction", "long")
        self.entry       = Decimal(str(d.get("entry_price", 0)))
        self.stop_loss   = Decimal(str(d.get("stop_loss", 0)))
        self.take_profit = Decimal(str(d.get("take_profit_1", 0)))
        self.raw         = d


class _ScreenerStrategyProxy:
    def __init__(self, user, sig: dict, account, instrument_type: str):
        self.user            = user
        self.broker          = account
        self.mode            = "live"
        self.is_global       = False
        self.instrument_type = instrument_type
        self.symbol          = sig.get("symbol", "")
        self.parameters      = {"capital": 100_000}
        self.name            = "ICT_SCREENER"
        self.id              = "screener"
        self.risk_config     = {"trader_type": "buyer"}
