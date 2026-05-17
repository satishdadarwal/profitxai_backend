from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

from .base import ExecutionOrder, OrderType, Position, Signal, SignalStatus

logger = logging.getLogger(__name__)


# -- abstract adapters ---------------------------------------------------------


class DatabaseAdapter(ABC):
    """Pluggable database backend (PostgreSQL, MongoDB, SQLite …)."""

    @abstractmethod
    async def save_signal(self, signal: Signal) -> str:
        """Persist signal; return DB record ID."""

    @abstractmethod
    async def update_signal_status(
        self, signal_id: str, status: SignalStatus, **kwargs
    ) -> None:
        """Update signal lifecycle status."""

    @abstractmethod
    async def save_position(self, position: Position) -> str:
        """Persist or update an open position."""

    @abstractmethod
    async def get_open_positions(self, symbol: Optional[str] = None) -> list[Position]:
        """Fetch currently open positions."""

    @abstractmethod
    async def save_order(self, order: ExecutionOrder) -> str:
        """Persist an execution order."""


class WebSocketAdapter(ABC):
    """Pluggable WebSocket broadcast backend."""

    @abstractmethod
    async def broadcast(self, channel: str, payload: dict) -> None:
        """Broadcast a message to all subscribers of a channel."""

    @abstractmethod
    async def send_to_user(self, user_id: str, payload: dict) -> None:
        """Send a targeted message to a specific user/session."""


class ExecutionAdapter(ABC):
    """Pluggable broker / exchange adapter."""

    @abstractmethod
    async def place_order(self, order: ExecutionOrder) -> str:
        """Submit order to broker; return broker order ID."""

    @abstractmethod
    async def cancel_order(self, broker_order_id: str) -> bool:
        """Cancel a pending order."""

    @abstractmethod
    async def get_account_balance(self) -> float:
        """Return current available balance."""

    @abstractmethod
    async def get_positions(self) -> list[dict]:
        """Fetch open positions from broker."""


# -- in-memory / stub implementations -----------------------------------------


class InMemoryDatabase(DatabaseAdapter):
    """Development stub -- stores everything in RAM."""

    def __init__(self) -> None:
        self._signals: dict[str, Signal] = {}
        self._positions: dict[str, Position] = {}
        self._orders: dict[str, ExecutionOrder] = {}

    async def save_signal(self, signal: Signal) -> str:
        self._signals[signal.id] = signal
        logger.debug(
            "DB: saved signal %s (%s %s %.2f)",
            signal.id[:8],
            signal.direction,
            signal.symbol,
            signal.confluence_score,
        )
        return signal.id

    async def update_signal_status(
        self, signal_id: str, status: SignalStatus, **kwargs
    ) -> None:
        if signal_id in self._signals:
            self._signals[signal_id].status = status
            logger.debug("DB: signal %s → %s", signal_id[:8], status)

    async def save_position(self, position: Position) -> str:
        self._positions[position.id] = position
        return position.id

    async def get_open_positions(self, symbol: Optional[str] = None) -> list[Position]:
        positions = [p for p in self._positions.values() if p.is_open]
        if symbol:
            positions = [p for p in positions if p.symbol == symbol]
        return positions

    async def save_order(self, order: ExecutionOrder) -> str:
        self._orders[order.id] = order
        return order.id


class LoggingWebSocket(WebSocketAdapter):
    """Development stub -- logs broadcasts instead of sending."""

    async def broadcast(self, channel: str, payload: dict) -> None:
        logger.info("WS [%s]: %s", channel, json.dumps(payload, default=str)[:200])

    async def send_to_user(self, user_id: str, payload: dict) -> None:
        logger.info("WS → user %s: %s", user_id, json.dumps(payload, default=str)[:200])


class PaperExecutionAdapter(ExecutionAdapter):
    """Paper-trading adapter -- logs orders without sending to a broker."""

    def __init__(self, initial_balance: float = 100_000.0) -> None:
        self._balance = initial_balance
        self._orders: list[ExecutionOrder] = []

    async def place_order(self, order: ExecutionOrder) -> str:
        self._orders.append(order)
        broker_id = f"PAPER-{order.id[:8]}"
        logger.info(
            "PAPER ORDER: %s %s %s @ %.5f  SL=%.5f  TP=%.5f  size=%.4f",
            order.direction.value.upper(),
            order.symbol,
            order.order_type.value,
            order.price,
            order.stop_loss,
            order.take_profit,
            order.size,
        )
        return broker_id

    async def cancel_order(self, broker_order_id: str) -> bool:
        logger.info("PAPER: cancel order %s", broker_order_id)
        return True

    async def get_account_balance(self) -> float:
        return self._balance

    async def get_positions(self) -> list[dict]:
        return []


# -- dispatcher ----------------------------------------------------------------


class Dispatcher:
    """
    Orchestrates DB persistence, WS broadcasting, and order execution.

    Usage
    -----
    dispatcher = Dispatcher(db=..., ws=..., executor=...)
    await dispatcher.dispatch(signal)
    """

    def __init__(
        self,
        db: DatabaseAdapter,
        ws: WebSocketAdapter,
        executor: ExecutionAdapter,
        dry_run: bool = False,
        pre_dispatch_hooks: Optional[list[Callable[[Signal], Awaitable[None]]]] = None,
        post_dispatch_hooks: Optional[
            list[Callable[[Signal, str], Awaitable[None]]]
        ] = None,
    ) -> None:
        self.db = db
        self.ws = ws
        self.executor = executor
        self.dry_run = dry_run
        self._pre_hooks = pre_dispatch_hooks or []
        self._post_hooks = post_dispatch_hooks or []

    # -- hooks -----------------------------------------------------------------

    async def _run_pre_hooks(self, signal: Signal) -> None:
        for hook in self._pre_hooks:
            try:
                await hook(signal)
            except Exception as exc:
                logger.warning("Pre-dispatch hook failed: %s", exc)

    async def _run_post_hooks(self, signal: Signal, db_id: str) -> None:
        for hook in self._post_hooks:
            try:
                await hook(signal, db_id)
            except Exception as exc:
                logger.warning("Post-dispatch hook failed: %s", exc)

    # -- DB --------------------------------------------------------------------

    async def _persist_signal(self, signal: Signal) -> str:
        try:
            db_id = await self.db.save_signal(signal)
            return db_id
        except Exception as exc:
            logger.error("Failed to persist signal %s: %s", signal.id, exc)
            raise

    # -- WS --------------------------------------------------------------------

    async def _notify(self, signal: Signal) -> None:
        payload = {
            "event": "new_signal",
            "timestamp": datetime.utcnow().isoformat(),
            "data": signal.to_dict(),
        }
        try:
            await self.ws.broadcast(f"signals:{signal.symbol}", payload)
            await self.ws.broadcast("signals:all", payload)
        except Exception as exc:
            logger.error("WS notification failed for signal %s: %s", signal.id, exc)

    # -- order build + execute -------------------------------------------------

    def _build_order(self, signal: Signal) -> ExecutionOrder:
        return ExecutionOrder(
            signal_id=signal.id,
            symbol=signal.symbol,
            direction=signal.direction,
            order_type=OrderType.LIMIT,
            price=signal.entry_price,
            size=signal.position_size,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit_1,
        )

    async def _execute(self, signal: Signal) -> Optional[str]:
        if self.dry_run:
            logger.info("DRY RUN: skipping execution for signal %s", signal.id)
            return None

        if not signal.is_actionable():
            logger.warning(
                "Signal %s is not actionable -- skipping execution", signal.id
            )
            return None

        order = self._build_order(signal)
        try:
            broker_id = await self.executor.place_order(order)
            order.broker_order_id = broker_id
            order.status = "submitted"
            await self.db.save_order(order)

            await self.db.update_signal_status(signal.id, SignalStatus.ACTIVE)
            signal.status = SignalStatus.ACTIVE

            await self.ws.broadcast(
                f"orders:{signal.symbol}",
                {
                    "event": "order_placed",
                    "timestamp": datetime.utcnow().isoformat(),
                    "data": order.to_dict(),
                },
            )
            return broker_id
        except Exception as exc:
            logger.error("Execution failed for signal %s: %s", signal.id, exc)
            await self.db.update_signal_status(signal.id, SignalStatus.CANCELLED)
            return None

    # -- main entry point ------------------------------------------------------

    async def dispatch(self, signal: Signal) -> dict[str, Any]:
        """
        Full dispatch pipeline:
          pre-hooks → DB save → WS notify → order execute → post-hooks

        Returns a result dict with keys: db_id, broker_id, status
        """
        result: dict[str, Any] = {
            "signal_id": signal.id,
            "db_id": None,
            "broker_id": None,
            "status": "failed",
        }

        await self._run_pre_hooks(signal)

        # 1. Persist
        try:
            db_id = await self._persist_signal(signal)
            result["db_id"] = db_id
        except Exception:
            return result

        # 2. Notify
        await self._notify(signal)

        # 3. Execute
        broker_id = await self._execute(signal)
        result["broker_id"] = broker_id
        result["status"] = (
            "submitted" if broker_id else ("dry_run" if self.dry_run else "skipped")
        )

        await self._run_post_hooks(signal, db_id)
        return result

    async def dispatch_many(self, signals: list[Signal]) -> list[dict[str, Any]]:
        """Dispatch multiple signals concurrently."""
        return list(await asyncio.gather(*[self.dispatch(s) for s in signals]))

    async def cancel_signal(self, signal: Signal) -> None:
        """Cancel a pending signal and its associated order."""
        await self.db.update_signal_status(signal.id, SignalStatus.CANCELLED)
        signal.status = SignalStatus.CANCELLED
        await self.ws.broadcast(
            f"signals:{signal.symbol}",
            {"event": "signal_cancelled", "signal_id": signal.id},
        )
        logger.info("Signal %s cancelled", signal.id)
