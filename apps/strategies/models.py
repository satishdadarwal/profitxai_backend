# apps/strategies/models.py
#
# CHANGES (Global Strategy feature):
#   1. `is_global` BooleanField added — admin backend se set karo, sab users ko dikhe
#   2. `allowed_plans` ArrayField added — specify karo ki kis plan wale user ko dikhe
#      e.g. ['basic', 'pro', 'elite'] means free users ko nahi dikhega
#   3. `created_by_admin` BooleanField added — admin-created strategies identify karo

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
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="strategies",
        null=True,         # ✅ Global strategies me user null hoga
        blank=True,
    )

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

    # ─── ✅ NEW: Global Strategy Fields ─────────────────────────────
    is_global = models.BooleanField(
        default=False,
        help_text="True = Admin ne banaya hai, sab eligible users ko dikhe. "
                  "False = Sirf us user ko dikhe jiska strategy hai.",
    )

    allowed_plans = ArrayField(
        models.CharField(max_length=20),
        default=list,
        blank=True,
        help_text="Kon se plans ke users ko yeh global strategy dikhe. "
                  "Empty = sab plans ke users. e.g. ['basic', 'pro', 'elite']",
    )

    created_by_admin = models.BooleanField(
        default=False,
        help_text="Admin panel se create ki gayi strategy hai ya user ne.",
    )
    # ─────────────────────────────────────────────────────────────────

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
        tag = " [GLOBAL]" if self.is_global else ""
        return f"{self.name} ({self.mode}/{self.instrument_type}) [{broker_name}] — {self.state}{tag}"

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

    def is_visible_to_user(self, user) -> bool:
        """
        Check karo ki yeh strategy `user` ko dikhni chahiye ya nahi.

        Rules:
        1. User ki apni strategy → hamesha dikhegi
        2. Global strategy → plan check karo
           - allowed_plans empty → sab plans ko dikhe
           - allowed_plans specified → sirf un plans ke users ko dikhe
        3. Global nahi, dusre user ki → kabhi nahi dikhegi

        FIX: user.plan field sync nahi hota Subscription se,
             isliye Subscription model directly check karo.
        """
        # Apni strategy
        if self.user_id == user.pk:
            return True

        # Global strategy — plan check karo
        if self.is_global:
            if not self.allowed_plans:
                return True  # Sab plans ko dikhe

            # Subscription se active plan fetch karo (reliable)
            user_plan_name = self._get_user_plan_name(user)
            if user_plan_name is None:
                return False

            # Case-insensitive comparison
            allowed_lower = [p.lower() for p in self.allowed_plans]
            return user_plan_name.lower() in allowed_lower

        return False

    @staticmethod
    def _get_user_plan_name(user) -> str | None:
        """
        User ka active plan name return karo.

        NOTE: Kai users ka Subscription record nahi hota (admin ne manually
        user.plan set kiya hoga). Dono cases handle karo.
        user.subscription directly access karne se RelatedObjectDoesNotExist
        crash hota hai — isliye filter() use karo.
        """
        # Priority 1: user.plan field (most reliable — admin directly set karta hai)
        plan_val = getattr(user, 'plan', 'free') or 'free'
        if plan_val != 'free':
            return plan_val.capitalize()  # "elite" → "Elite"

        # Priority 2: Subscription table
        try:
            from apps.subscriptions.models import Subscription, Plan
            sub = Subscription.objects.filter(
                user=user
            ).select_related('plan').first()
            if sub and sub.is_access_granted and sub.plan.tier > Plan.Tier.FREE:
                return sub.plan.name
        except Exception:
            pass

        return None


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

        if channel_layer is None:
            _log.debug("Channel layer not configured — skipping WS broadcast")
            return

        # ✅ Global strategy → sab connected users ko broadcast karo
        if instance.is_global:
            # Yeh approach sirf tab kaam karta hai jab aap sab users track kar rahe ho
            # Simplest approach: group "global_strategies" use karo
            async_to_sync(channel_layer.group_send)(
                "global_strategies",
                {
                    "type": "strategy_update",
                    "payload": StrategySerializer(instance).data,
                },
            )
        elif instance.user_id:
            group_name = f"user_{instance.user_id}"
            async_to_sync(channel_layer.group_send)(
                group_name,
                {
                    "type": "strategy_update",
                    "payload": StrategySerializer(instance).data,
                },
            )
    except Exception as e:
        _log.warning("WS broadcast failed: %s", e)

# ─────────────────────────────────────────────────────────────────
#  UserStrategyPreference — User ka per-strategy mode preference
#
#  Admin ek strategy banata hai (is_global=True) — usme mode field
#  sirf "master default" hai. Har user apna preferred_mode choose
#  kar sakta hai: paper ya live.
#
#  Agar koi preference nahi hai → strategy.mode (master default) use hoga.
# ─────────────────────────────────────────────────────────────────
class UserStrategyPreference(models.Model):
    """
    User ka per-strategy mode preference.

    Ek user ek strategy ke liye ek hi preference rakh sakta hai
    (unique_together = user + strategy).

    preferred_mode:
      - 'paper' → user paper trading karna chahta hai
      - 'live'  → user live trading karna chahta hai
      - None    → strategy ka master default use karo
    """

    class PreferredMode(models.TextChoices):
        PAPER = "paper", "Paper Trading"
        LIVE  = "live",  "Live Trading"

    id       = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user     = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="strategy_preferences",
    )
    strategy = models.ForeignKey(
        Strategy,
        on_delete=models.CASCADE,
        related_name="user_preferences",
    )
    preferred_mode = models.CharField(
        max_length=10,
        choices=PreferredMode.choices,
        default=PreferredMode.PAPER,
        help_text="User ka chosen mode: paper ya live",
    )
    is_running = models.BooleanField(
        default=False,
        help_text="Is user ke liye yeh strategy chal rahi hai?",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("user", "strategy")
        verbose_name = "User Strategy Preference"
        verbose_name_plural = "User Strategy Preferences"

    def __str__(self):
        return (
            f"{self.user.email} — {self.strategy.name} "
            f"[{self.preferred_mode}] "
            f"{'▶ Running' if self.is_running else '⏹ Stopped'}"
        )

    def effective_mode(self) -> str:
        """User ka mode — agar set nahi toh strategy ka master default."""
        return self.preferred_mode or self.strategy.mode

# ─────────────────────────────────────────────────────────────────
#  UserScreenerPreference — User ka screener signal execution mode
#
#  Screener ICT signals ke liye user choose karta hai:
#  auto   → signal aate hi paper/live trade execute
#  semi   → notification aata hai, user confirm kare 60s mein
#  manual → sirf signal dikhao, user khud trade kare
# ─────────────────────────────────────────────────────────────────
class UserScreenerPreference(models.Model):

    class ExecutionMode(models.TextChoices):
        AUTO   = "auto",   "Auto (Immediate Execution)"
        SEMI   = "semi",   "Semi (Alert + 60s Confirm)"
        MANUAL = "manual", "Manual (Show Only)"

    class TradingMode(models.TextChoices):
        PAPER = "paper", "Paper Trading"
        LIVE  = "live",  "Live Trading"

    id   = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="screener_preference",
    )
    execution_mode = models.CharField(
        max_length=10,
        choices=ExecutionMode.choices,
        default=ExecutionMode.SEMI,
        help_text="Signal aane pe kya karna hai",
    )
    trading_mode = models.CharField(
        max_length=10,
        choices=TradingMode.choices,
        default=TradingMode.PAPER,
        help_text="Paper ya live trade execute karo",
    )
    options_enabled = models.BooleanField(default=True)
    crypto_enabled  = models.BooleanField(default=True)
    risk_pct        = models.FloatField(default=1.0, help_text="Risk % per trade")
    leverage        = models.IntegerField(default=10)
    created_at      = models.DateTimeField(auto_now_add=True)
    updated_at      = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "User Screener Preference"
        verbose_name_plural = "User Screener Preferences"

    def __str__(self):
        return f"{self.user.email} | {self.execution_mode} | {self.trading_mode}"
