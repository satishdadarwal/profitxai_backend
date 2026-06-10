# apps/orders/models.py
# UPDATED VERSION - WITH NOTES, TAGS, EMOJI_REACTION SUPPORT + POSITION MODEL

import uuid
from decimal import Decimal
from typing import TYPE_CHECKING

from django.contrib.auth import get_user_model
from django.db import models

if TYPE_CHECKING:
    from apps.market.models import Asset as AssetType

User = get_user_model()


class Order(models.Model):

    # ── Enums ────────────────────────────────────────────────────
    class Side(models.TextChoices):
        BUY = "buy", "Buy"
        SELL = "sell", "Sell"

    class OrderType(models.TextChoices):
        MARKET = "market", "Market"
        LIMIT = "limit", "Limit"
        STOP = "stop", "Stop"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        FILLED = "filled", "Filled"
        PARTIAL = "partial", "Partially Filled"
        CANCELLED = "cancelled", "Cancelled"
        REJECTED = "rejected", "Rejected"

    class Mode(models.TextChoices):
        LIVE = "live", "Live"
        PAPER = "paper", "Paper"

    # ── Fields ───────────────────────────────────────────────────
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="orders")
    asset = models.ForeignKey(
        "market.Asset", on_delete=models.PROTECT, related_name="orders"
    )
    strategy = models.ForeignKey(
        "strategies.Strategy",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="orders",
    )

    broker_account = models.ForeignKey(
        "brokers.BrokerAccount",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="orders",
        help_text="Broker used to execute this order",
    )
    
    class ExecutionStatus(models.TextChoices):
        PENDING  = "pending",  "Pending"
        SENT     = "sent",     "Sent"
        ACCEPTED = "accepted", "Accepted"
        REJECTED = "rejected", "Rejected"
        FAILED   = "failed",   "Failed"

    execution_status = models.CharField(
        max_length=12,
        choices=ExecutionStatus.choices,
        default=ExecutionStatus.PENDING,
        db_index=True,
    )
    broker_response = models.JSONField(
        default=dict,
        blank=True,
        help_text="Raw broker API response for audit",
    )
    rejection_reason = models.TextField(blank=True, default="")

    side = models.CharField(max_length=5, choices=Side.choices)
    order_type = models.CharField(max_length=8, choices=OrderType.choices)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.OPEN
    )
    mode = models.CharField(max_length=5, choices=Mode.choices, default=Mode.LIVE)

    quantity = models.DecimalField(max_digits=20, decimal_places=8)
    filled_qty = models.DecimalField(
        max_digits=20, decimal_places=8, default=Decimal("0")
    )
    limit_price = models.DecimalField(
        max_digits=20, decimal_places=8, null=True, blank=True
    )
    stop_price = models.DecimalField(
        max_digits=20, decimal_places=8, null=True, blank=True
    )
    avg_fill_price = models.DecimalField(
        max_digits=20, decimal_places=8, null=True, blank=True
    )
    sl_price = models.DecimalField(
        max_digits=20, decimal_places=8, null=True, blank=True
    )
    target_price = models.DecimalField(
        max_digits=20, decimal_places=8, null=True, blank=True
    )

    # ── Idempotency ───────────────────────────────────────────────
    # Flutter retry pe duplicate order mat bano.
    # Flutter har order ke saath ek UUID bheje — agar same UUID dobara aaye
    # toh naya order nahi banega, pehle wala return hoga.
    client_order_id = models.CharField(
        max_length=64,
        unique=True,
        null=True,
        blank=True,
        db_index=True,
        help_text="Flutter se aaya idempotency key — duplicate orders rokta hai",
    )

    # Execution
    exchange_order_id = models.CharField(max_length=128, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    # ── Journal fields ────────────────────────────────────────────
    symbol_display = models.CharField(max_length=64, blank=True, default="")
    broker = models.CharField(max_length=20, blank=True, default="")
    instrument_type = models.CharField(max_length=20, blank=True, default="")
    option_type = models.CharField(max_length=4, blank=True, default="")
    lots = models.IntegerField(null=True, blank=True)
    tags = models.JSONField(default=list, blank=True)
    emoji_reaction = models.CharField(max_length=10, blank=True, default="")
    journal_notes = models.TextField(blank=True, default="")

    # ── Trade tracking fields ─────────────────────────────────────
    entry_price = models.DecimalField(max_digits=16, decimal_places=6, null=True, blank=True)
    exit_price = models.DecimalField(max_digits=16, decimal_places=6, null=True, blank=True)
    realized_pnl = models.DecimalField(max_digits=16, decimal_places=6, null=True, blank=True)
    unrealized_pnl = models.DecimalField(max_digits=16, decimal_places=6, null=True, blank=True)
    current_price = models.DecimalField(max_digits=16, decimal_places=6, null=True, blank=True)
    entry_time = models.DateTimeField(null=True, blank=True)
    exit_time = models.DateTimeField(null=True, blank=True)
    exit_reason = models.CharField(max_length=50, blank=True, default='')
    position_size = models.IntegerField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    # ── Meta ─────────────────────────────────────────────────────
    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "status"]),
            models.Index(fields=["asset", "status"]),
            models.Index(fields=["mode", "status"]),
            models.Index(fields=["strategy", "status"]),
        ]

    STATUS_CHOICES = Status.choices

    # ── Properties ───────────────────────────────────────────────
    @property
    def remaining_qty(self) -> Decimal:
        return self.quantity - self.filled_qty

    @property
    def is_paper(self) -> bool:
        return self.mode == self.Mode.PAPER

    @property
    def symbol(self) -> str:
        try:
            return self.asset.symbol  # type: ignore[union-attr]
        except Exception:
            return ""

    def __str__(self):
        return (
            f"[{self.mode.upper()}] {self.side} {self.quantity} "
            f"{self.asset} @ {self.limit_price or 'MARKET'}"
        )


class Trade(models.Model):
    """
    Unified Trade model for both Indian (NSE/Options) and Crypto (Delta Exchange).
    Order fill record — ek order ke multiple partial fills ho sakte hain.
    """
    
    # ── Market Type ──────────────────────────────────────────────
    class MarketType(models.TextChoices):
        INDIAN = "indian", "Indian Market"
        CRYPTO = "crypto", "Crypto Market"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="fills")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="trades")
    asset = models.ForeignKey(
        "market.Asset", on_delete=models.PROTECT, related_name="trades"
    )
    
    # ── NEW: Market Type Classification ─────────────────────────
    market_type = models.CharField(
        max_length=10,
        choices=MarketType.choices,
        default=MarketType.INDIAN,
        db_index=True,
        help_text="Indian (NSE/Options/Futures) or Crypto (Delta Exchange)"
    )

    side = models.CharField(max_length=4, choices=Order.Side.choices)
    mode = models.CharField(
        max_length=5, choices=Order.Mode.choices, default=Order.Mode.LIVE
    )

    # ── Common Fields (Both Markets) ────────────────────────────
    quantity = models.DecimalField(max_digits=20, decimal_places=8)
    price = models.DecimalField(max_digits=20, decimal_places=8)
    amount = models.DecimalField(max_digits=20, decimal_places=8)
    fee = models.DecimalField(max_digits=20, decimal_places=8, default=Decimal("0"))

    realized_pnl = models.DecimalField(
        max_digits=20, decimal_places=8, null=True, blank=True
    )

    # ── NEW: Journal Fields ─────────────────────────────────────
    notes = models.TextField(
        blank=True,
        default="",
        help_text="User's trade notes, strategy explanation, learnings"
    )
    
    tags = models.JSONField(
        default=list,
        blank=True,
        help_text="List of tag strings: ['breakout', 'daily-tf', 'missed-entry']"
    )
    
    emoji_reaction = models.CharField(
        max_length=10,
        blank=True,
        default="",
        help_text="Single emoji representing trade feeling: 😊, 😢, 🤔, 🔥, etc."
    )

    # ── Indian Market Specific Fields ──────────────────────────
    strike = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Strike price for options (Indian market only)"
    )
    
    lots = models.IntegerField(
        null=True,
        blank=True,
        help_text="Number of lots (Indian market: 1 lot = lot_size * quantity)"
    )
    
    option_type = models.CharField(
        max_length=2,
        choices=[("CE", "Call"), ("PE", "Put")],
        blank=True,
        default="",
        help_text="CE/PE for Indian options"
    )

    # ── Crypto Market Specific Fields ──────────────────────────
    leverage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Leverage multiplier (Crypto futures only)"
    )
    
    funding_fee = models.DecimalField(
        max_digits=20,
        decimal_places=8,
        null=True,
        blank=True,
        help_text="Funding fee charged/earned (Crypto perpetuals only)"
    )

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "asset"]),
            models.Index(fields=["mode", "created_at"]),
            models.Index(fields=["market_type", "created_at"]),
            models.Index(fields=["user", "market_type", "created_at"]),
        ]

    def __str__(self):
        market_prefix = f"[{self.market_type.upper()}]"
        return (
            f"{market_prefix} Trade {self.id} | {self.side} {self.quantity} "
            f"{self.asset} @ {self.price}"
        )


class TradeJournalEntry(models.Model):
    """User-written journal entry linked to a trade or order."""

    class Emotion(models.TextChoices):
        CONFIDENT = "confident", "Confident"
        FEARFUL = "fearful", "Fearful"
        GREEDY = "greedy", "Greedy"
        NEUTRAL = "neutral", "Neutral"
        FOMO = "fomo", "FOMO"

    class Outcome(models.TextChoices):
        WIN = "win", "Win"
        LOSS = "loss", "Loss"
        BREAKEVEN = "be", "Break-even"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="journal_entries"
    )
    trade = models.OneToOneField(
        Trade, on_delete=models.SET_NULL, null=True, blank=True, related_name="journal"
    )
    order = models.ForeignKey(
        Order,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="journal_entries",
    )

    title = models.CharField(max_length=255)
    body = models.TextField()
    strategy = models.CharField(max_length=100, blank=True, default="")
    emotion = models.CharField(
        max_length=10, choices=Emotion.choices, blank=True, default=""
    )
    outcome = models.CharField(
        max_length=4, choices=Outcome.choices, blank=True, default=""
    )
    rating = models.PositiveSmallIntegerField(null=True, blank=True)

    tags = models.JSONField(default=list, blank=True)
    screenshot = models.ImageField(
        upload_to="journal/screenshots/", null=True, blank=True
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Journal: {self.title} ({self.user})"


# ══════════════════════════════════════════════════════════════
# ✅ NEW: POSITION MODEL FOR LIVE TRADING
# ══════════════════════════════════════════════════════════════

class Position(models.Model):
    """
    Open position tracking - ek filled order se banta hai.
    Live trading ke liye position management.
    
    Example flow:
    1. Order placed (BUY 1 BTC @ 50000)
    2. Order filled → Position created (OPEN)
    3. Position.current_price updates continuously
    4. User closes position → Position.status = CLOSED
    """
    
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        CLOSED = "closed", "Closed"
        PARTIAL = "partial", "Partially Closed"
    
    # ── Primary Fields ───────────────────────────────────────────
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="positions")
    asset = models.ForeignKey(
        "market.Asset", on_delete=models.PROTECT, related_name="positions"
    )
    
    # ── References ───────────────────────────────────────────────
    opening_order = models.ForeignKey(
        Order, 
        on_delete=models.SET_NULL, 
        null=True,
        blank=True,
        related_name="opened_positions",
        help_text="Order jisse ye position open hua"
    )
    
    live_signal = models.ForeignKey(
        "live_trading.LiveSignal",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="positions",
        help_text="LiveSignal jisse ye position trigger hua (if applicable)"
    )
    
    # ── Position Details ─────────────────────────────────────────
    side = models.CharField(
        max_length=5, 
        choices=Order.Side.choices,
        help_text="BUY = Long position, SELL = Short position"
    )
    quantity = models.DecimalField(
        max_digits=20, 
        decimal_places=8,
        help_text="Total quantity when position opened"
    )
    remaining_qty = models.DecimalField(
        max_digits=20, 
        decimal_places=8,
        help_text="Remaining quantity (decreases with partial closes)"
    )
    avg_entry_price = models.DecimalField(
        max_digits=20, 
        decimal_places=8,
        help_text="Average entry price"
    )
    
    # ── P&L Tracking ─────────────────────────────────────────────
    current_price = models.DecimalField(
        max_digits=20, 
        decimal_places=8, 
        null=True, 
        blank=True,
        help_text="Current market price (updated periodically)"
    )
    unrealized_pnl = models.DecimalField(
        max_digits=20, 
        decimal_places=8, 
        default=Decimal("0"),
        help_text="Current unrealized P&L"
    )
    realized_pnl = models.DecimalField(
        max_digits=20, 
        decimal_places=8, 
        default=Decimal("0"),
        help_text="Realized P&L after closing"
    )
    
    # ── Risk Management ──────────────────────────────────────────
    stop_loss = models.DecimalField(
        max_digits=20, 
        decimal_places=8, 
        null=True, 
        blank=True,
        help_text="Stop loss price"
    )
    take_profit = models.DecimalField(
        max_digits=20, 
        decimal_places=8, 
        null=True, 
        blank=True,
        help_text="Take profit target price"
    )
    
    # ── Status & Mode ────────────────────────────────────────────
    status = models.CharField(
        max_length=10, 
        choices=Status.choices, 
        default=Status.OPEN,
        db_index=True
    )
    mode = models.CharField(
        max_length=5, 
        choices=Order.Mode.choices, 
        default=Order.Mode.LIVE,
        help_text="Live or Paper trading"
    )
    
    # ── Timestamps ───────────────────────────────────────────────
    opened_at = models.DateTimeField(auto_now_add=True, db_index=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # ── Meta ─────────────────────────────────────────────────────
    class Meta:
        ordering = ["-opened_at"]
        indexes = [
            models.Index(fields=["user", "status"]),
            models.Index(fields=["asset", "status"]),
            models.Index(fields=["status", "opened_at"]),
            models.Index(fields=["mode", "status"]),
        ]
        verbose_name = "Position"
        verbose_name_plural = "Positions"
    
    # ── Properties ───────────────────────────────────────────────
    @property
    def symbol(self) -> str:
        """Get asset symbol"""
        try:
            return self.asset.symbol
        except Exception:
            return ""
    
    @property
    def is_open(self) -> bool:
        """Check if position is open"""
        return self.status == self.Status.OPEN
    
    @property
    def pnl_percentage(self) -> Decimal:
        """Calculate P&L as percentage of entry"""
        if not self.current_price or not self.avg_entry_price:
            return Decimal("0")
        
        if self.side == Order.Side.BUY:
            # Long position: profit when price goes up
            return ((self.current_price - self.avg_entry_price) / self.avg_entry_price) * 100
        else:
            # Short position: profit when price goes down
            return ((self.avg_entry_price - self.current_price) / self.avg_entry_price) * 100
    
    # ── Methods ──────────────────────────────────────────────────
    def calculate_pnl(self, current_price: Decimal) -> Decimal:
        """
        Calculate unrealized P&L at given price
        
        Args:
            current_price: Price to calculate P&L at
            
        Returns:
            Unrealized P&L amount
        """
        if self.side == Order.Side.BUY:
            # Long: profit = (current - entry) * quantity
            pnl = (current_price - self.avg_entry_price) * self.remaining_qty
        else:
            # Short: profit = (entry - current) * quantity
            pnl = (self.avg_entry_price - current_price) * self.remaining_qty
        
        return pnl
    
    def update_current_price(self, price: Decimal):
        """
        Update current price and recalculate unrealized P&L
        
        Args:
            price: New current market price
        """
        self.current_price = price
        self.unrealized_pnl = self.calculate_pnl(price)
        self.save(update_fields=['current_price', 'unrealized_pnl', 'updated_at'])
    
    def close_position(self, close_price: Decimal, closing_order=None):
        """
        Close the position completely
        
        Args:
            close_price: Price at which position is closed
            closing_order: Optional Order object that closed this position
        """
        from django.utils import timezone
        
        self.status = self.Status.CLOSED
        self.closed_at = timezone.now()
        self.realized_pnl = self.calculate_pnl(close_price)
        self.remaining_qty = Decimal("0")
        self.current_price = close_price
        self.save()
    
    def partial_close(self, close_qty: Decimal, close_price: Decimal):
        """
        Partially close the position
        
        Args:
            close_qty: Quantity to close
            close_price: Price at which to close
        """
        if close_qty >= self.remaining_qty:
            # If closing entire remaining quantity, just close fully
            return self.close_position(close_price)
        
        # Calculate proportional P&L for this partial close
        pnl_per_unit = self.calculate_pnl(close_price) / self.remaining_qty
        partial_pnl = pnl_per_unit * close_qty
        
        # Update position
        self.remaining_qty -= close_qty
        self.realized_pnl += partial_pnl
        self.status = self.Status.PARTIAL
        self.save()
    
    def __str__(self):
        return (
            f"[{self.mode.upper()}] {self.side} {self.quantity} "
            f"{self.symbol} @ {self.avg_entry_price} ({self.status})"
        )


# ── Daily PnL Snapshot ────────────────────────────────────────
class DailyPnlSnapshot(models.Model):
    user         = models.ForeignKey(User, on_delete=models.CASCADE, related_name='daily_pnl_snapshots')
    date         = models.DateField()
    mode         = models.CharField(max_length=10, default='live')
    market_type  = models.CharField(max_length=10, default='all')

    realised_pnl   = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    unrealised_pnl = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    fees           = models.DecimalField(max_digits=20, decimal_places=4, default=0)

    total_trades = models.IntegerField(default=0)
    wins         = models.IntegerField(default=0)
    losses       = models.IntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('user', 'date', 'mode', 'market_type')
        ordering = ['-date']

    def __str__(self):
        return f'{self.user} | {self.date} | {self.mode} | {self.market_type} | {self.realised_pnl}'
