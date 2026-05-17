# apps/subscriptions/models.py

import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import models
from django.utils import timezone

User = get_user_model()


# ─────────────────────────────────────────────────────────────────
#  Plan tiers — keep in sync with permissions.py TIER_* constants
# ─────────────────────────────────────────────────────────────────
class Plan(models.Model):
    """
    Admin panel se manage karo — DB-driven plan catalog.

    Tier hierarchy (int for easy comparison):
        FREE = 0  →  BASIC = 1  →  PRO = 2  →  ELITE = 3

    Feature limits stored as JSON so we never need migrations for
    adding new capability fields.

    Example feature_limits:
        {
          "max_brokers":    1,
          "max_strategies": 2,
          "live_trading":   false,
          "paper_trading":  true,
          "backtest":       true
        }
    """

    class Tier(models.IntegerChoices):
        FREE = 0, "Free"
        BASIC = 1, "Basic"
        PRO = 2, "Pro"
        ELITE = 3, "Elite"

    class BillingCycle(models.TextChoices):
        MONTHLY = "monthly", "Monthly"
        YEARLY = "yearly", "Yearly"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100)
    tier = models.IntegerField(choices=Tier.choices)
    billing_cycle = models.CharField(
        max_length=10, choices=BillingCycle.choices, default=BillingCycle.MONTHLY
    )

    # Razorpay plan ID (created via Razorpay dashboard / API)
    razorpay_plan_id = models.CharField(max_length=100, blank=True, default="")

    # Pricing
    price_inr = models.DecimalField(max_digits=10, decimal_places=2)  # INR paise × 100

    # Feature gates
    feature_limits = models.JSONField(default=dict)

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["tier", "billing_cycle"]
        unique_together = [("tier", "billing_cycle")]

    # ── Convenience accessors ────────────────────────────────────
    def get_limit(self, key, default=0):
        return self.feature_limits.get(key, default)

    @property
    def allows_live_trading(self) -> bool:
        return bool(self.feature_limits.get("live_trading", False))

    @property
    def max_brokers(self) -> int:
        return int(self.feature_limits.get("max_brokers", 0))

    @property
    def max_strategies(self) -> int:
        return int(self.feature_limits.get("max_strategies", 0))

    def __str__(self):
        return f"{self.name} ({self.get_billing_cycle_display()}) — ₹{self.price_inr}"


# ─────────────────────────────────────────────────────────────────
#  Subscription
# ─────────────────────────────────────────────────────────────────
class Subscription(models.Model):
    """
    One active subscription per user at a time.
    Razorpay subscription_id ties this to a recurring billing mandate.
    """

    class Status(models.TextChoices):
        TRIALING = "trialing", "Trialing"
        ACTIVE = "active", "Active"
        PAST_DUE = "past_due", "Past Due"
        CANCELLED = "cancelled", "Cancelled"
        EXPIRED = "expired", "Expired"
        PAUSED = "paused", "Paused"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="subscription"
    )
    plan = models.ForeignKey(
        Plan, on_delete=models.PROTECT, related_name="subscriptions"
    )

    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.TRIALING, db_index=True
    )

    # Razorpay identifiers
    razorpay_subscription_id = models.CharField(
        max_length=100, blank=True, default="", db_index=True
    )
    razorpay_customer_id = models.CharField(max_length=100, blank=True, default="")

    # Billing period
    current_period_start = models.DateTimeField(null=True, blank=True)
    current_period_end = models.DateTimeField(null=True, blank=True)

    # Grace period: payment failed hone ke baad X days access milta hai
    grace_until = models.DateTimeField(null=True, blank=True)

    trial_end = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["status", "current_period_end"]),
        ]

    # ── Status helpers ───────────────────────────────────────────
    @property
    def is_access_granted(self) -> bool:
        """
        True if user should have platform access right now.
        Handles grace period for failed payments.
        """
        now = timezone.now()

        if self.status == self.Status.ACTIVE:
            return True

        if self.status == self.Status.TRIALING:
            return self.trial_end is None or now < self.trial_end

        if self.status == self.Status.PAST_DUE:
            return self.grace_until is not None and now < self.grace_until

        return False

    @property
    def tier(self) -> int:
        return self.plan.tier

    @property
    def is_free(self) -> bool:
        return self.plan.tier == Plan.Tier.FREE

    @property
    def is_pro_or_above(self) -> bool:
        return self.plan.tier >= Plan.Tier.PRO

    @property
    def days_until_renewal(self) -> int | None:
        if self.current_period_end:
            delta = self.current_period_end - timezone.now()
            return max(0, delta.days)
        return None

    def get_limit(self, key, default=0):
        return self.plan.get_limit(key, default)

    def __str__(self):
        return f"{self.user} | {self.plan} | {self.status}"


# ─────────────────────────────────────────────────────────────────
#  Payment Log
# ─────────────────────────────────────────────────────────────────
class PaymentLog(models.Model):
    """
    Har Razorpay payment attempt ka immutable audit trail.
    """

    class PaymentStatus(models.TextChoices):
        CREATED = "created", "Created"
        CAPTURED = "captured", "Captured"
        FAILED = "failed", "Failed"
        REFUNDED = "refunded", "Refunded"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subscription = models.ForeignKey(
        Subscription, on_delete=models.CASCADE, related_name="payments"
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="payment_logs"
    )

    # Razorpay IDs
    razorpay_order_id = models.CharField(
        max_length=100, blank=True, default="", db_index=True
    )
    razorpay_payment_id = models.CharField(
        max_length=100, blank=True, default="", db_index=True
    )
    razorpay_signature = models.CharField(max_length=256, blank=True, default="")

    amount_inr = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default="INR")
    payment_status = models.CharField(
        max_length=10, choices=PaymentStatus.choices, default=PaymentStatus.CREATED
    )

    # Full webhook / API payload for debugging
    raw_payload = models.JSONField(default=dict)
    failure_reason = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Payment {self.razorpay_payment_id} | {self.payment_status} | ₹{self.amount_inr}"


# ─────────────────────────────────────────────────────────────────
#  Razorpay Webhook Event  (idempotency store)
# ─────────────────────────────────────────────────────────────────
class RazorpayWebhookEvent(models.Model):
    """
    Processed webhook event IDs store karo — duplicate processing rokne ke liye.
    Razorpay kabhi kabhi same event 2-3 baar send karta hai.
    """

    event_id = models.CharField(max_length=100, unique=True, db_index=True)
    event_type = models.CharField(max_length=100)
    processed_at = models.DateTimeField(auto_now_add=True)
    payload = models.JSONField(default=dict)

    class Meta:
        ordering = ["-processed_at"]

    def __str__(self):
        return f"{self.event_type} | {self.event_id}"
