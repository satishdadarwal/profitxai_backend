# apps/strategies/models.py
#
# CHANGES from previous version:
#   1. instrument_type field added (options/futures/equity/perp)
#   2. risk_config JSONField added (sl_pct, target_pct, qty, rr_ratio)
#   3. symbol duplicate field removed (was defined twice)

import uuid

from django.contrib.auth import get_user_model
from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models.signals import post_save
from django.dispatch import receiver
from apps.brokers.models import BrokerAccount 

User = get_user_model()


class Strategy(models.Model):

    class Mode(models.TextChoices):
        PAPER = "paper", "Paper"
        LIVE = "live", "Live"

    class State(models.TextChoices):
        IDLE = "idle", "Idle"
        RUNNING = "running", "Running"
        ERROR = "error", "Error"

    class InstrumentType(models.TextChoices):
        OPTIONS = "options", "Options"
        FUTURES = "futures", "Futures"
        EQUITY = "equity", "Equity"
        PERP = "perp", "Perpetual"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="strategies")

    broker = models.ForeignKey(
        'brokers.BrokerAccount',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='strategies'
    )

    name = models.CharField(max_length=100)
    algo_name = models.CharField(max_length=100)
    symbol = models.CharField(max_length=30)
    symbols = ArrayField(
        models.CharField(max_length=30),
        default=list,
        blank=True,
        help_text="['NIFTY', 'BANKNIFTY'] ya ['BTCUSDT', 'ETHUSDT']",
    )

    instrument_type = models.CharField(
        max_length=10,
        choices=InstrumentType.choices,
        default=InstrumentType.FUTURES,
        help_text="Fyers: options/futures/equity | Delta: futures/perp",
    )

    risk_config = models.JSONField(
        default=dict,
        blank=True,
        help_text="Per-strategy risk params: sl_pct, target_pct, qty, rr_ratio etc.",
    )

    mode = models.CharField(max_length=10, choices=Mode.choices, default=Mode.PAPER)
    state = models.CharField(max_length=10, choices=State.choices, default=State.IDLE)
    interval_seconds = models.PositiveIntegerField(default=60)
    parameters = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)

    TIMEFRAME_CHOICES = [
        ("1",    "1 Minute"),
        ("5",    "5 Minutes"),
        ("15",   "15 Minutes"),
        ("30",   "30 Minutes"),
        ("60",   "1 Hour"),
        ("240",  "4 Hours"),
        ("1440", "1 Day"),
    ]

    timeframe = models.CharField(
        max_length=5,
        choices=TIMEFRAME_CHOICES,
        default="15",
        help_text="Signal detection timeframe (minutes). risk_config['timeframe'] se override hota hai.",
    )

    default_lots = models.PositiveIntegerField(
        default=1,
        help_text="Default lots per trade. risk_config['qty'] se override hota hai.",
    )
    error_msg = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    stopped_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "strategies"
        ordering = ["-created_at"]

    def __str__(self):
        broker_name = self.broker.broker if self.broker else "no broker"
        return f"{self.name} ({self.mode}/{self.instrument_type}) [{broker_name}] — {self.state}"

    @property
    def is_running(self):
        return self.state == self.State.RUNNING

    @property
    def broker_slug(self):
        """Linked broker ka slug: 'fyers' ya 'delta'"""
        return self.broker.broker if self.broker else None

    @property
    def is_fyers(self):
        return self.broker_slug == "fyers"

    @property
    def is_delta(self):
        return self.broker_slug == "delta"

    def get_risk_param(self, key, default=None):
        """risk_config se param safely fetch karo."""
        return self.risk_config.get(key, default)


class StrategySignal(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    strategy = models.ForeignKey(
        Strategy, on_delete=models.CASCADE, related_name="signals"
    )
    signal_type = models.CharField(max_length=20)
    symbol = models.CharField(max_length=30)
    price = models.DecimalField(max_digits=20, decimal_places=8)
    reason = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    result = models.CharField(max_length=20, default="skipped")
    order = models.ForeignKey(
        "orders.Order",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="signals",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "strategy_signals"
        ordering = ["-created_at"]


class StrategyPerformanceSnapshot(models.Model):

    class Granularity(models.TextChoices):
        HOURLY = "hourly", "Hourly"
        DAILY = "daily", "Daily"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    strategy = models.ForeignKey(
        Strategy, on_delete=models.CASCADE, related_name="performance_snapshots"
    )
    granularity = models.CharField(max_length=10, choices=Granularity.choices)
    period_start = models.DateTimeField()
    total_trades = models.IntegerField(default=0)
    win_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    total_pnl = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    total_fees = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "strategy_performance_snapshots"
        ordering = ["-period_start"]


@receiver(post_save, sender=Strategy)
def broadcast_strategy_update(sender, instance, **kwargs):
    """Strategy save hone pe user ke WS channel pe push karo."""
    import logging

    _log = logging.getLogger(__name__)

    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        from .serializers import StrategySerializer

        channel_layer = get_channel_layer()

        # ✅ FIX: None guard — channel layer configured nahi to silently skip
        if channel_layer is None:
            _log.debug("Channel layer not configured — skipping WS broadcast")
            return

        group_name = f"user_{instance.user_id}"

        async_to_sync(channel_layer.group_send)(  # now safe — not None
            group_name,
            {
                "type": "strategy_update",
                "payload": StrategySerializer(instance).data,
            },
        )
    except Exception as e:
        _log.warning("WS broadcast failed: %s", e)