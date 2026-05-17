"""
Strategy Engine Base Classes
Defines the core data models shared across the engine:
  Signal, Position, RiskParameters, ExecutionOrder
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional

import pandas as pd


# -- enums ---------------------------------------------------------------------
class SignalDirection(str, Enum):
    LONG = "long"
    SHORT = "short"


class SignalStrength(str, Enum):
    WEAK = "weak"
    MODERATE = "moderate"
    STRONG = "strong"
    VERY_STRONG = "very_strong"


class SignalStatus(str, Enum):
    PENDING = "pending"  # created, not yet acted on
    ACTIVE = "active"  # order placed
    FILLED = "filled"  # position open
    PARTIAL = "partial"  # partially filled
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    STOPPED = "stopped"  # stopped out
    CLOSED = "closed"  # target hit / manually closed


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class PositionSide(str, Enum):
    LONG = "long"
    SHORT = "short"


# -- risk parameters -----------------------------------------------------------
@dataclass
class RiskParameters:
    """Per-trade and portfolio risk controls."""

    # Account risk
    account_balance: float = 100_000.0
    risk_per_trade_pct: float = 1.0  # % of account at risk
    max_daily_drawdown_pct: float = 3.0
    max_open_positions: int = 5
    max_correlated_positions: int = 2  # same asset class
    # Execution
    default_order_type: OrderType = OrderType.LIMIT
    slippage_allowance_pct: float = 0.05  # 5 bps
    # Trade management
    use_trailing_stop: bool = False
    trailing_stop_atr_mult: float = 2.0
    breakeven_trigger_rr: float = 1.0  # move SL to BE at 1:1
    partial_close_at_rr: float = 1.5  # close 50% at 1.5R
    # Filters
    min_confluence_score: float = 60.0
    min_rr_ratio: float = 2.0
    max_spread_pct: float = 0.1  # reject if spread > 10 bps


# -- signal --------------------------------------------------------------------
@dataclass
class Signal:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=datetime.utcnow)
    symbol: str = ""
    direction: Optional[SignalDirection] = None
    strength: SignalStrength = SignalStrength.WEAK
    status: SignalStatus = SignalStatus.PENDING
    # Price levels
    entry_price: float = 0.0
    stop_loss: float = 0.0
    take_profit_1: float = 0.0
    take_profit_2: Optional[float] = None
    take_profit_3: Optional[float] = None
    # Risk metrics
    risk_reward: float = 0.0
    risk_amount: float = 0.0  # in account currency
    position_size: float = 0.0  # units / contracts / lots
    pip_value: float = 0.0
    # ICT confluence
    confluence_score: float = 0.0
    confluence_breakdown: dict[str, float] = field(default_factory=dict)
    # Context
    timeframes: list[str] = field(default_factory=list)
    anchor_tf: str = ""
    execution_tf: str = ""
    killzone: str = ""
    session: str = ""
    # ICT rationale tags
    tags: list[str] = field(default_factory=list)  # e.g. ['OB', 'FVG', 'BOS', 'KZ']
    # Metadata
    strategy_name: str = "ICT_MTF"
    notes: str = ""
    raw_analysis: Optional[Any] = field(default=None, repr=False)

    def risk_pips(self) -> float:
        return abs(self.entry_price - self.stop_loss)

    def reward_pips(self) -> float:
        return abs(self.take_profit_1 - self.entry_price)

    def is_actionable(self) -> bool:
        return (
            self.status == SignalStatus.PENDING
            and self.entry_price > 0
            and self.stop_loss > 0
            and self.take_profit_1 > 0
            and self.risk_reward >= 2.0
            and self.confluence_score >= 60.0
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "created_at": self.created_at.isoformat(),
            "symbol": self.symbol,
            "direction": self.direction.value if self.direction else None,
            "strength": self.strength.value,
            "status": self.status.value,
            "entry_price": self.entry_price,
            "stop_loss": self.stop_loss,
            "take_profit_1": self.take_profit_1,
            "take_profit_2": self.take_profit_2,
            "take_profit_3": self.take_profit_3,
            "risk_reward": self.risk_reward,
            "risk_amount": self.risk_amount,
            "position_size": self.position_size,
            "confluence_score": self.confluence_score,
            "confluence_breakdown": self.confluence_breakdown,
            "timeframes": self.timeframes,
            "anchor_tf": self.anchor_tf,
            "execution_tf": self.execution_tf,
            "killzone": self.killzone,
            "tags": self.tags,
            "strategy_name": self.strategy_name,
            "notes": self.notes,
        }


# -- position ------------------------------------------------------------------
@dataclass
class Position:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    signal_id: str = ""
    symbol: str = ""
    side: PositionSide = PositionSide.LONG
    # Fill details
    open_price: float = 0.0
    open_time: datetime = field(default_factory=datetime.utcnow)
    size: float = 0.0
    commission: float = 0.0
    # Levels
    stop_loss: float = 0.0
    take_profit: float = 0.0
    trailing_stop: Optional[float] = None
    # Current state
    current_price: float = 0.0
    unrealised_pnl: float = 0.0
    realised_pnl: float = 0.0
    # Close details
    close_price: Optional[float] = None
    close_time: Optional[datetime] = None
    close_reason: str = ""
    is_open: bool = True

    def update_pnl(self, current_price: float) -> None:
        self.current_price = current_price
        if self.side == PositionSide.LONG:
            self.unrealised_pnl = (current_price - self.open_price) * self.size
        else:
            self.unrealised_pnl = (self.open_price - current_price) * self.size

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "signal_id": self.signal_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "open_price": self.open_price,
            "open_time": self.open_time.isoformat(),
            "size": self.size,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "current_price": self.current_price,
            "unrealised_pnl": self.unrealised_pnl,
            "realised_pnl": self.realised_pnl,
            "is_open": self.is_open,
            "close_reason": self.close_reason,
        }


# -- execution order ------------------------------------------------------------
@dataclass
class ExecutionOrder:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    signal_id: str = ""
    symbol: str = ""
    direction: SignalDirection = SignalDirection.LONG
    order_type: OrderType = OrderType.LIMIT
    price: float = 0.0  # limit/stop price
    size: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    created_at: datetime = field(default_factory=datetime.utcnow)
    expires_at: Optional[datetime] = None
    broker_order_id: Optional[str] = None
    status: str = "pending"
    fill_price: Optional[float] = None
    fill_time: Optional[datetime] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "signal_id": self.signal_id,
            "symbol": self.symbol,
            "direction": self.direction.value,
            "order_type": self.order_type.value,
            "price": self.price,
            "size": self.size,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "created_at": self.created_at.isoformat(),
            "broker_order_id": self.broker_order_id,
            "status": self.status,
        }
