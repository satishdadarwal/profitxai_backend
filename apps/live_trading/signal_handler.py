# apps/live_trading/signal_handler.py

from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

logger = logging.getLogger(__name__)

# SEMI_AUTO confirmation window
SEMI_AUTO_TIMEOUT_SECONDS = 60


class SignalHandler:
    """
    Ek TradingSession process karta hai:
    1. ICT engine se raw signals fetch karo
    2. Mode ke hisaab se route karo
    3. LiveSignal DB mein save karo
    4. Tasks queue karo
    """

    def process_session(self, session) -> dict:
        from .models import LiveSignal, TradingMode

        raw_signals = self._detect_signals(session)
        if not raw_signals:
            return {"session": session.id, "signals": 0}

        count = 0
        for raw in raw_signals:
            try:
                signal = self._create_live_signal(session, raw)

                if session.mode == TradingMode.AUTO:
                    self._handle_auto(signal)
                elif session.mode == TradingMode.SEMI_AUTO:
                    self._handle_semi_auto(signal)
                elif session.mode == TradingMode.MANUAL:
                    self._handle_manual(signal)

                count += 1
            except Exception as exc:
                logger.error(
                    "SignalHandler: signal processing failed | session=%s | %s",
                    session.id, exc,
                )

        return {"session": session.id, "signals": count}

    # ── Signal Detection ───────────────────────────────────────────────────
    def _detect_signals(self, session) -> list[dict]:
        """
        ICT Engine scanner se signals fetch karo.
        """
        try:
            from apps.ict_engine.scanner import Scanner

            scanner = Scanner()
            risk_config = self._get_risk_config(session)
            signals = scanner.scan_sync(
                symbol    = risk_config.get("symbol", "NIFTY"),
                timeframe = risk_config.get("timeframe", "15"),
                mode      = session.mode,
                strategy  = risk_config.get("_strategy_obj"),  # ✅ Fyers credentials ke liye
            )
            return signals or []
        except Exception as exc:
            logger.warning("_detect_signals: ICT engine error | %s", exc)
            return []

    def _get_risk_config(self, session) -> dict:
        """
        Strategy se real config fetch karo.
        Priority: risk_config JSON → strategy.timeframe/default_lots → fallback
        """
        try:
            from apps.strategies.models import Strategy

            strategy = Strategy.objects.get(
                id   = session.strategy_id,
                user = session.user,
            )

            symbols = getattr(strategy, "symbols", None) or []
            symbol  = symbols[0] if symbols else getattr(strategy, "symbol", "NIFTY")

            # risk_config JSON se override, warna naye model fields use karo
            rc        = strategy.risk_config or {}
            timeframe = str(rc.get("timeframe") or getattr(strategy, "timeframe", "15") or "15")
            lots      = int(rc.get("qty") or rc.get("lots") or getattr(strategy, "default_lots", 1) or 1)

            return {
                "symbol":        symbol,
                "timeframe":     timeframe,
                "lots":          lots,
                "strategy_name": getattr(strategy, "algo_name", ""),
                "strategy_id":   str(strategy.id),
                "_strategy_obj": strategy,           # ✅ scanner ko pass karo
            }

        except Exception as exc:
            logger.warning(
                "_get_risk_config: Strategy fetch failed | session=%s | strategy_id=%s | %s "
                "— using fallback defaults",
                session.id, getattr(session, "strategy_id", "?"), exc,
            )
            return {
                "symbol":        getattr(session, "symbol", "NIFTY"),
                "timeframe":     str(getattr(session, "timeframe", "15")),
                "lots":          int(getattr(session, "default_lots", 1)),
                "_strategy_obj": None,
            }

    # ── Create LiveSignal ──────────────────────────────────────────────────
    def _create_live_signal(self, session, raw: dict):
        from .models import LiveSignal, TradingMode

        entry  = Decimal(str(raw.get("entry_price", 0)))
        sl     = Decimal(str(raw.get("stop_loss", 0)))
        tp     = Decimal(str(raw.get("take_profit", 0)))
        risk   = abs(entry - sl)
        reward = abs(tp - entry)
        rr     = round(reward / risk, 2) if risk else Decimal("0")

        lots       = Decimal(str(raw.get("lots", 1)))
        margin_req = lots * entry * Decimal("0.15")  # 15% approx margin

        expires_at = None
        if session.mode == TradingMode.SEMI_AUTO:
            expires_at = timezone.now() + timedelta(seconds=SEMI_AUTO_TIMEOUT_SECONDS)

        signal = LiveSignal.objects.create(
            session     = session,
            user        = session.user,
            strategy_id = session.strategy_id,
            symbol      = raw.get("symbol", ""),
            direction   = raw.get("direction", "buy"),
            signal_type = raw.get("signal_type", "orderBlock"),
            strength    = raw.get("strength", "moderate"),
            entry_price = entry,
            stop_loss   = sl,
            take_profit = tp,
            rr_ratio    = rr,
            lots        = lots,
            margin_req  = margin_req,
            mode        = session.mode,
            status      = LiveSignal.Status.PENDING,
            expires_at  = expires_at,
            raw_payload = raw,
        )

        logger.info(
            "LiveSignal created | id=%s | %s %s @ %s | mode=%s",
            signal.id, signal.direction, signal.symbol,
            signal.entry_price, signal.mode,
        )
        return signal

    # ── AUTO Mode ──────────────────────────────────────────────────────────
    def _handle_auto(self, signal):
        """Signal detect hote hi turat execute karo."""
        from .tasks import execute_trade_task

        execute_trade_task.apply_async(
            args  = [signal.id, "auto"],
            queue = "orders",
        )
        logger.info("AUTO: queued execution | signal=%s", signal.id)

    # ── SEMI-AUTO Mode ─────────────────────────────────────────────────────
    def _handle_semi_auto(self, signal):
        """
        User ko alert bhejo. 60s countdown ke baad expire_pending_signals_task
        automatically expire karega agar confirm nahi hua.

        ✅ FIX #H4: direction/signal_type/strength fields ab metadata mein hain.
        Pehle Flutter mein har signal 'buy' dikhta tha kyunki direction missing tha.
        """
        from apps.notifications.tasks import send_notification_task

        send_notification_task.delay(
            user_id  = signal.user_id,
            channel  = "all",
            title    = f"🔔 Signal Alert: {signal.symbol}",
            body     = (
                f"{signal.direction.upper()} @ ₹{signal.entry_price} | "
                f"SL: ₹{signal.stop_loss} | TP: ₹{signal.take_profit} | "
                f"RR: 1:{signal.rr_ratio} | Confirm within 60s"
            ),
            level    = "warning",
            category = "trade",
            metadata = {
                "signal_id":   signal.id,
                "mode":        "semi_auto",
                "direction":   signal.direction,      # ✅ FIX #H4
                "signal_type": signal.signal_type,    # ✅ FIX #H4
                "strength":    signal.strength,       # ✅ FIX #H4
                "expires_at":  (
                    signal.expires_at.isoformat() if signal.expires_at else None
                ),
                "entry_price": str(signal.entry_price),
                "stop_loss":   str(signal.stop_loss),
                "take_profit": str(signal.take_profit),
                "rr_ratio":    str(signal.rr_ratio),
                "lots":        str(signal.lots),
                "margin_req":  str(signal.margin_req),
                "type":        "signal_alert",
            },
        )
        logger.info(
            "SEMI_AUTO: alert sent | signal=%s | expires=%s",
            signal.id, signal.expires_at,
        )

    # ── MANUAL Mode ────────────────────────────────────────────────────────
    def _handle_manual(self, signal):
        """
        Signal card dikhaao sirf — koi automatic execution nahi.
        User FAB se manually order dega.

        ✅ FIX #H4: direction/signal_type/strength yahan bhi add kiye.
        """
        from apps.notifications.tasks import send_notification_task

        send_notification_task.delay(
            user_id  = signal.user_id,
            channel  = "ws",   # sirf UI update, no email/push
            title    = f"📌 Signal Detected: {signal.symbol}",
            body     = (
                f"{signal.direction.upper()} opportunity | "
                f"Entry: ₹{signal.entry_price} | RR: 1:{signal.rr_ratio}"
            ),
            level    = "info",
            category = "trade",
            metadata = {
                "signal_id":   signal.id,
                "mode":        "manual",
                "direction":   signal.direction,      # ✅ FIX #H4
                "signal_type": signal.signal_type,    # ✅ FIX #H4
                "strength":    signal.strength,       # ✅ FIX #H4
                "entry_price": str(signal.entry_price),
                "stop_loss":   str(signal.stop_loss),
                "take_profit": str(signal.take_profit),
                "rr_ratio":    str(signal.rr_ratio),
                "lots":        str(signal.lots),
                "margin_req":  str(signal.margin_req),
                "type":        "signal_card",
            },
        )
        logger.info("MANUAL: signal card pushed | signal=%s", signal.id)