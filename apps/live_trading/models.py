# apps/live_trading/models.py
#
# Live Trading system ke liye naye models.
# Existing UserStrategy, BrokerOrder, BrokerAccount ke saath integrate hote hain.
#
# Migration: python manage.py makemigrations live_trading

from django.conf import settings
from django.db import models
from django.utils import timezone


class TradingMode(models.TextChoices):
    AUTO      = "auto",      "Auto (Immediate Execution)"
    SEMI_AUTO = "semi_auto", "Semi-Auto (Alert + 60s Confirmation)"
    MANUAL    = "manual",    "Manual (FAB Order Placement)"


class ActivityStatus(models.TextChoices):
    EXECUTED = "executed", "Executed"
    IGNORED  = "ignored",  "Ignored"
    EXPIRED  = "expired",  "Expired"
    PENDING  = "pending",  "Pending Confirmation"
    FAILED   = "failed",   "Failed"


# ─────────────────────────────────────────────────────────────
#  1. Trading Session  (algo start → stop ke beech ka period)
# ─────────────────────────────────────────────────────────────
class TradingSession(models.Model):
    user         = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="trading_sessions")
    strategy_id  = models.CharField(max_length=64, help_text="UserStrategy.id (algo_name)")
    mode         = models.CharField(max_length=16, choices=TradingMode.choices, default=TradingMode.SEMI_AUTO)
    started_at   = models.DateTimeField(default=timezone.now)
    ended_at     = models.DateTimeField(null=True, blank=True)
    is_active    = models.BooleanField(default=True)

    # Session summary (populated on algo stop)
    total_trades   = models.IntegerField(default=0)
    winning_trades = models.IntegerField(default=0)
    total_pnl      = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    max_drawdown   = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    peak_equity    = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    class Meta:
        ordering = ["-started_at"]
        indexes  = [models.Index(fields=["user", "is_active"])]

    def __str__(self):
        return f"Session({self.user_id} | {self.strategy_id} | {self.mode} | {self.started_at:%Y-%m-%d %H:%M})"

    @property
    def win_rate(self) -> float:
        return (self.winning_trades / self.total_trades * 100) if self.total_trades else 0.0

    def close(self):
        self.is_active = False
        self.ended_at  = timezone.now()
        self.save(update_fields=["is_active", "ended_at"])


# ─────────────────────────────────────────────────────────────
#  2. Live Signal  (backend se detect hua signal)
# ─────────────────────────────────────────────────────────────
class LiveSignal(models.Model):
    class Status(models.TextChoices):
        PENDING    = "pending",    "Pending"
        PROCESSING = "processing", "Processing"  # execute_trade_task lock
        CONFIRMED  = "confirmed",  "Confirmed"
        EXPIRED    = "expired",    "Expired"
        EXECUTED   = "executed",   "Executed"
        IGNORED    = "ignored",    "Ignored"

    session       = models.ForeignKey(TradingSession, on_delete=models.CASCADE, related_name="signals")
    user          = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="live_signals")
    strategy_id   = models.CharField(max_length=64)
    symbol        = models.CharField(max_length=32)
    direction     = models.CharField(max_length=8)    # 'buy' | 'sell'
    signal_type   = models.CharField(max_length=32)   # 'orderBlock' | 'fvg' etc.
    strength      = models.CharField(max_length=16)   # 'weak' | 'moderate' | 'strong'

    entry_price   = models.DecimalField(max_digits=16, decimal_places=6)
    stop_loss     = models.DecimalField(max_digits=16, decimal_places=6)
    take_profit   = models.DecimalField(max_digits=16, decimal_places=6)
    rr_ratio      = models.DecimalField(max_digits=8,  decimal_places=2, default=0)
    lots          = models.DecimalField(max_digits=8,  decimal_places=2, default=1)
    margin_req    = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    mode          = models.CharField(max_length=16, choices=TradingMode.choices)
    status        = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)

    detected_at   = models.DateTimeField(default=timezone.now)
    expires_at    = models.DateTimeField(null=True, blank=True)  # SEMI_AUTO: +60s
    acted_at      = models.DateTimeField(null=True, blank=True)

    raw_payload   = models.JSONField(default=dict)

    class Meta:
        ordering = ["-detected_at"]
        indexes  = [
            models.Index(fields=["user", "status"]),
            models.Index(fields=["session", "detected_at"]),
        ]

    def __str__(self):
        return f"Signal({self.symbol} {self.direction} @ {self.entry_price} [{self.status}])"

    def is_expired(self) -> bool:
        return self.expires_at is not None and timezone.now() > self.expires_at

    def mark_executed(self):
        self.status   = self.Status.EXECUTED
        self.acted_at = timezone.now()
        self.save(update_fields=["status", "acted_at"])

    def mark_ignored(self):
        self.status   = self.Status.IGNORED
        self.acted_at = timezone.now()
        self.save(update_fields=["status", "acted_at"])

    def mark_expired(self):
        self.status   = self.Status.EXPIRED
        self.acted_at = timezone.now()
        self.save(update_fields=["status", "acted_at"])


# ─────────────────────────────────────────────────────────────
#  3. Activity Log  (har action ka audit trail)
# ─────────────────────────────────────────────────────────────
class ActivityLog(models.Model):
    session    = models.ForeignKey(TradingSession, on_delete=models.CASCADE, related_name="activity_logs")
    signal     = models.ForeignKey(LiveSignal, on_delete=models.SET_NULL, null=True, blank=True, related_name="logs")
    user       = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    status     = models.CharField(max_length=16, choices=ActivityStatus.choices)
    mode       = models.CharField(max_length=16, choices=TradingMode.choices)
    symbol     = models.CharField(max_length=32)
    direction  = models.CharField(max_length=8)
    entry_price= models.DecimalField(max_digits=16, decimal_places=6, null=True, blank=True)
    pnl        = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    note       = models.CharField(max_length=256, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    metadata   = models.JSONField(default=dict)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Log({self.symbol} {self.status} @ {self.created_at:%H:%M:%S})"


# ─────────────────────────────────────────────────────────────
#  4. Manual Order (MANUAL mode FAB se placed)
# ─────────────────────────────────────────────────────────────
class ManualOrder(models.Model):
    class Status(models.TextChoices):
        DRAFT    = "draft",    "Draft"
        PLACED   = "placed",   "Placed"
        FILLED   = "filled",   "Filled"
        REJECTED = "rejected", "Rejected"
        CANCELLED= "cancelled","Cancelled"

    session     = models.ForeignKey(TradingSession, on_delete=models.CASCADE, related_name="manual_orders")
    user        = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    symbol      = models.CharField(max_length=32)
    direction   = models.CharField(max_length=8)
    order_type  = models.CharField(max_length=16, default="MARKET")  # MARKET | LIMIT | SL
    lots        = models.DecimalField(max_digits=8, decimal_places=2)
    price       = models.DecimalField(max_digits=16, decimal_places=6, null=True, blank=True)
    stop_loss   = models.DecimalField(max_digits=16, decimal_places=6, null=True, blank=True)
    take_profit = models.DecimalField(max_digits=16, decimal_places=6, null=True, blank=True)
    rr_ratio    = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    margin_req  = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    status      = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    broker_order_id = models.CharField(max_length=64, blank=True)
    placed_at   = models.DateTimeField(null=True, blank=True)
    filled_at   = models.DateTimeField(null=True, blank=True)
    fill_price  = models.DecimalField(max_digits=16, decimal_places=6, null=True, blank=True)
    created_at  = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]