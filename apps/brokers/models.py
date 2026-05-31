# apps/brokers/models.py
import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import models
from django.utils import timezone
from apps.brokers.encryption import EncryptedCharField


User = get_user_model()


class BrokerAccount(models.Model):
    class BrokerType(models.TextChoices):
        ZERODHA = "zerodha", "Zerodha"
        BINANCE = "binance", "Binance"
        FYERS   = "fyers",   "Fyers"
        DELTA   = "delta",   "Delta"
        DHAN    = "dhan",    "Dhan"

    user         = models.ForeignKey(User, on_delete=models.CASCADE, related_name="broker_accounts")
    broker       = models.CharField(max_length=20, choices=BrokerType.choices)
    label        = models.CharField(max_length=50, blank=True, default="")

    # ── Fyers credentials ─────────────────────────────────────────────────────
    app_id       = models.CharField(max_length=255, blank=True, default="")
    secret_key   = models.CharField(max_length=255, blank=True, default="")
    redirect_uri = models.CharField(max_length=500, blank=True, default="")

    # ── Zerodha / legacy credentials ──────────────────────────────────────────
    api_key      = models.CharField(max_length=255, blank=True, default="")
    api_secret   = models.CharField(max_length=255, blank=True, null=True)

    # ── Dhan credentials ──────────────────────────────────────────────────────
    dhan_client_id = models.CharField(
        max_length=50, blank=True, default="",
        help_text="Dhan Client ID (e.g. '1000000001')",
    )
    dhan_access_token = models.TextField(
        blank=True, default="",
        help_text="Dhan access token (24hr validity — SEBI mandate)",
    )

    # ── Zerodha credentials ───────────────────────────────────────────────────
    zerodha_user_id = models.CharField(
        max_length=20, blank=True, default="",
        db_index=True,
        help_text="Zerodha User ID (e.g. 'AB1234') — from OAuth response",
    )
    zerodha_request_token = models.CharField(
        max_length=500, blank=True, default="",
        help_text="Temporary request_token (cleared after exchange)",
    )

    # ── Tokens ────────────────────────────────────────────────────────────────
    access_token  = models.TextField(blank=True, null=True)
    refresh_token = models.TextField(blank=True, null=True)
    token_expiry  = models.DateTimeField(blank=True, null=True)

    # ── Fyers client ID (multi-user account identification) ───────────────────
    fyers_client_id = models.CharField(
        max_length=20,
        blank=True,
        default="",
        db_index=True,
        help_text=(
            "User ka Fyers client ID (e.g. 'YC00329'). "
            "Auto-login aur multi-user account identification ke liye."
        ),
    )

    # ── Fyers daily auto-refresh PIN (Fernet encrypted at rest) ───────────────
    # User ek baar Flutter app se save karta hai.
    # Celery roz 8:30 AM pe use karta hai token refresh ke liye.
    # DB mein ciphertext store hota hai — plain PIN kabhi persist nahi hota.
    # Requires FERNET_KEYS = ['<32-byte-base64-key>'] in settings.py
    fyers_pin = EncryptedCharField(
        max_length=10,
        blank=True,
        default="",
        help_text="Fyers 4-digit PIN — Fernet-encrypted at rest. Plain text kabhi store nahi hota.",
    )
    totp_secret = EncryptedCharField(
        max_length=32,
        blank=True,
        default="",
        help_text="Fyers TOTP secret key — Fernet-encrypted at rest. Plain text kabhi store nahi hota.",
    )

    # ── Status ────────────────────────────────────────────────────────────────
    is_active   = models.BooleanField(default=True)
    is_verified = models.BooleanField(default=False)
    created_at  = models.DateTimeField(auto_now_add=True)
    updated_at  = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("user", "broker", "label")]
        indexes = [
            # Fast lookup: FyersAutoLoginView + auto_refresh_fyers_tokens
            # filter(user=X, broker="fyers", fyers_client_id=Y)
            models.Index(
                fields=["user", "broker", "fyers_client_id"],
                name="ba_user_broker_client_idx",
            ),
            # Fast lookup: factory._get_adapter_for_user()
            # filter(user=X, broker="fyers", is_active=True, is_verified=True)
            models.Index(
                fields=["user", "broker", "is_active", "is_verified"],
                name="ba_user_broker_active_idx",
            ),
        ]

    def __str__(self):
        return f"{self.user} - {self.broker} - {self.label}"


class BrokerOrder(models.Model):
    """
    Algo trading order lifecycle — Unified model.

    Flow (Live via new Order layer):
        Signal → Order created → BrokerOrder(PENDING)
            → Celery → Broker API
            → OPEN → COMPLETE / REJECTED / FAILED → retry

    Flow (Legacy Live):
        OptionTrade.save(mode='live')
            → signal → BrokerOrder(PENDING)
            → Celery → Broker API
            → OPEN → COMPLETE / REJECTED / FAILED → retry

    Flow (Paper):
        PaperTrade.save()
            → signal → BrokerOrder(COMPLETE)   ← no real broker call

    Constraint: exactly ONE of order / option_trade / paper_trade must be set.
    """

    class Status(models.TextChoices):
        PENDING   = "pending",   "Pending"           # DB mein hai, broker ko nahi bheja
        PLACED    = "placed",    "Placed"             # Broker ko bheja gaya
        OPEN      = "open",      "Open"               # Broker ne accept kiya, fill awaited
        PARTIAL   = "partial",   "Partially Filled"   # Partial fill hua
        COMPLETE  = "complete",  "Complete"           # Fully executed
        FILLED    = "filled",    "Filled"             # Fully executed (alternate)
        CANCELLED = "cancelled", "Cancelled"          # User/system ne cancel kiya
        REJECTED  = "rejected",  "Rejected"           # Broker ne reject kiya (no retry)
        FAILED    = "failed",    "Failed"             # Network/system error (retry hoga)

    class OrderType(models.TextChoices):
        ENTRY  = "entry",  "Entry"
        SL     = "sl",     "Stop Loss"
        TARGET = "target", "Target"
        EXIT   = "exit",   "Exit"

    class Side(models.TextChoices):
        BUY  = "buy",  "Buy"
        SELL = "sell", "Sell"

    class OrderCategory(models.TextChoices):
        MARKET    = "market",    "Market"
        LIMIT     = "limit",     "Limit"
        STOP_LOSS = "stop_loss", "Stop Loss"
        SL_M      = "sl_m",      "Stop Loss Market"
        SL_L      = "sl_l",      "Stop Loss Limit"

    # ── Primary Key ───────────────────────────────────────────────────────────
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # ── Relations ─────────────────────────────────────────────────────────────
    broker_account = models.ForeignKey(
        BrokerAccount,
        on_delete=models.PROTECT,
        related_name="broker_orders",
    )

    # Exactly ek set hoga — CheckConstraint below enforce karta hai
    option_trade = models.ForeignKey(
        "options.OptionTrade",
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name="broker_orders",
        help_text="NSE live options trade (legacy flow)",
    )
    paper_trade = models.ForeignKey(
        "paper_trading.PaperTrade",
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name="broker_orders",
        help_text="Paper trade (crypto / futures / options simulation)",
    )
    order = models.ForeignKey(
        "orders.Order",
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name="broker_executions",
        help_text="The Order this execution attempt belongs to (new flow)",
    )

    # ── Order identity & details ──────────────────────────────────────────────
    order_type = models.CharField(
        max_length=10,
        choices=OrderType.choices,
        default=OrderType.ENTRY,
        help_text="Entry, SL, Target, or Exit",
    )
    broker_order_id = models.CharField(
        max_length=100,
        blank=True,
        default="",
        db_index=True,
        help_text="ID returned by broker API (unique per broker)",
    )
    exchange_order_id = models.CharField(
        max_length=100,
        blank=True,
        default="",
        db_index=True,
        help_text="Exchange order ID from broker (if different from broker_order_id)",
    )

    # ── Order parameters ──────────────────────────────────────────────────────
    symbol = models.CharField(
        max_length=100,
        help_text="Trading symbol (e.g., NIFTY24JUN21000CE, BTCUSDT)",
    )
    quantity = models.IntegerField(help_text="Total order quantity")
    side = models.CharField(
        max_length=10,
        choices=Side.choices,
        help_text="Buy or Sell",
    )
    order_category = models.CharField(
        max_length=20,
        choices=OrderCategory.choices,
        default=OrderCategory.MARKET,
        help_text="Market, Limit, Stop Loss, etc.",
    )
    price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True, blank=True,
        help_text="Limit price (for limit orders)",
    )
    trigger_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True, blank=True,
        help_text="Stop loss trigger price",
    )

    # ── Status & Execution ────────────────────────────────────────────────────
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    filled_quantity = models.IntegerField(
        default=0,
        help_text="Quantity filled so far",
    )
    avg_fill_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True, blank=True,
        help_text="Average execution price",
    )
    realized_pnl = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        null=True, blank=True,
        help_text="Net realized PnL in INR after this order filled (fees deducted). "
                  "Null for BUY/entry orders; populated on SELL/exit fill.",
    )
    rejection_reason = models.TextField(blank=True, default="")

    # ── Retry / reliability ───────────────────────────────────────────────────
    retry_count   = models.PositiveSmallIntegerField(default=0)
    max_retries   = models.PositiveSmallIntegerField(default=3)
    next_retry_at = models.DateTimeField(
        null=True, blank=True,
        help_text="Celery retry scheduled time (exponential backoff)",
    )

    # ── Timestamps ────────────────────────────────────────────────────────────
    created_at        = models.DateTimeField(auto_now_add=True)
    updated_at        = models.DateTimeField(auto_now=True)
    placed_at         = models.DateTimeField(
        null=True, blank=True,
        help_text="When order was first created in DB",
    )
    sent_to_broker_at = models.DateTimeField(
        null=True, blank=True,
        help_text="When order was sent to broker API",
    )
    executed_at = models.DateTimeField(
        null=True, blank=True,
        help_text="When order was fully filled",
    )
    filled_at = models.DateTimeField(
        null=True, blank=True,
        help_text="Alias for executed_at (compatibility)",
    )
    cancelled_at = models.DateTimeField(
        null=True, blank=True,
        help_text="When order was cancelled",
    )

    # ── Audit / debug ─────────────────────────────────────────────────────────
    broker_response = models.JSONField(
        default=dict, blank=True,
        help_text="Raw broker API response — debug only, not exposed via API",
    )
    notes = models.TextField(blank=True, default="")

    # ── Meta ──────────────────────────────────────────────────────────────────
    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["broker_account", "status"]),
            models.Index(fields=["option_trade", "order_type"]),
            models.Index(fields=["paper_trade", "status"]),
            models.Index(fields=["order", "status"]),
            models.Index(fields=["status", "next_retry_at"]),
            models.Index(fields=["broker_order_id"]),
            models.Index(fields=["exchange_order_id"]),
            models.Index(fields=["symbol", "status"]),
        ]
        constraints = [
            models.CheckConstraint(
                check=(
                    # Case 1: Sirf order set hai (naya flow)
                    models.Q(
                        order__isnull=False,
                        option_trade__isnull=True,
                        paper_trade__isnull=True,
                    )
                    # Case 2: Sirf option_trade set hai (purana live flow)
                    | models.Q(
                        order__isnull=True,
                        option_trade__isnull=False,
                        paper_trade__isnull=True,
                    )
                    # Case 3: Sirf paper_trade set hai (purana paper flow)
                    | models.Q(
                        order__isnull=True,
                        option_trade__isnull=True,
                        paper_trade__isnull=False,
                    )
                ),
                name="brokerorder_exactly_one_trade_fk",
            )
        ]

    # ── Dunder ────────────────────────────────────────────────────────────────
    def __str__(self):
        broker = self.broker_account.broker if self.broker_account_id else "?"
        if self.option_trade_id:
            trade = f"OT:{str(self.option_trade_id)[:8]}"
        elif self.paper_trade_id:
            trade = f"PT:{str(self.paper_trade_id)[:8]}"
        elif self.order_id:
            trade = f"OR:{str(self.order_id)[:8]}"
        else:
            trade = "UNLINKED"

        oid = self.broker_order_id or self.exchange_order_id or str(self.id)[:8]
        return (
            f"{broker} | {trade} | {self.order_type} | "
            f"{self.symbol} {self.side} {self.quantity} | {oid} | {self.status}"
        )

    # ── Properties ────────────────────────────────────────────────────────────
    @property
    def linked_trade(self):
        """Whichever trade is linked — option, paper, ya order."""
        return self.option_trade or self.paper_trade or self.order

    @property
    def is_paper(self) -> bool:
        """True agar ye paper trade ka order hai."""
        if self.paper_trade_id:
            return True
        if self.order_id:
            return self.order.mode == "paper"
        return False

    @property
    def is_option_order(self) -> bool:
        return self.option_trade is not None

    @property
    def broker_name(self) -> str | None:
        return self.broker_account.broker if self.broker_account_id else None

    @property
    def can_retry(self) -> bool:
        """True agar FAILED hai aur retries baaki hain."""
        return (
            self.status == self.Status.FAILED
            and self.retry_count < self.max_retries
        )

    @property
    def is_complete(self) -> bool:
        """Terminal states — koi aur action nahi hoga."""
        return self.status in [
            self.Status.FILLED,
            self.Status.COMPLETE,
            self.Status.REJECTED,
            self.Status.CANCELLED,
        ]

    @property
    def is_active(self) -> bool:
        return not self.is_complete

    @property
    def fill_percentage(self) -> float:
        if self.quantity == 0:
            return 0.0
        return (self.filled_quantity / self.quantity) * 100

    # ── State transitions ─────────────────────────────────────────────────────
    # Hamesha ye methods use karo — status directly set mat karo.
    # update_fields se sirf zaruri columns update honge → race conditions kam.

    def mark_sent(
        self,
        broker_order_id: str,
        broker_response: dict,
        exchange_order_id: str | None = None,
    ) -> None:
        """Broker ne order accept kar liya."""
        self.broker_order_id   = broker_order_id
        self.status            = self.Status.OPEN
        self.sent_to_broker_at = timezone.now()
        self.broker_response   = broker_response

        if exchange_order_id:
            self.exchange_order_id = exchange_order_id

        update_fields = [
            "broker_order_id", "status",
            "sent_to_broker_at", "broker_response",
        ]
        if exchange_order_id:
            update_fields.append("exchange_order_id")

        self.save(update_fields=update_fields)

    def mark_partial(
        self,
        filled_quantity: int,
        avg_fill_price: float,
        broker_response: dict | None = None,
    ) -> None:
        """Order partially fill hua."""
        self.status          = self.Status.PARTIAL
        self.filled_quantity = filled_quantity
        self.avg_fill_price  = avg_fill_price

        if broker_response:
            self.broker_response = broker_response

        self.save(update_fields=[
            "status", "filled_quantity", "avg_fill_price", "broker_response",
        ])

    def mark_complete(
        self,
        filled_quantity: int | None = None,
        avg_fill_price: float | None = None,
        broker_response: dict | None = None,
    ) -> None:
        """Order fully execute ho gaya."""
        now = timezone.now()

        self.status      = self.Status.COMPLETE
        self.executed_at = now
        self.filled_at   = now  # compatibility alias

        if filled_quantity is not None:
            self.filled_quantity = filled_quantity
        else:
            self.filled_quantity = self.quantity

        if avg_fill_price is not None:
            self.avg_fill_price = avg_fill_price

        if broker_response:
            self.broker_response = broker_response

        self.save(update_fields=[
            "status", "executed_at", "filled_at",
            "filled_quantity", "avg_fill_price", "broker_response",
        ])

    def mark_cancelled(self, reason: str = "") -> None:
        """User ya system ne cancel kiya."""
        self.status       = self.Status.CANCELLED
        self.cancelled_at = timezone.now()

        if reason:
            self.rejection_reason = reason

        self.save(update_fields=["status", "cancelled_at", "rejection_reason"])

    def mark_rejected(
        self,
        reason: str,
        broker_response: dict | None = None,
    ) -> None:
        """Broker ne reject kiya — retry nahi hoga."""
        self.status           = self.Status.REJECTED
        self.rejection_reason = reason

        if broker_response:
            self.broker_response = broker_response

        self.save(update_fields=["status", "rejection_reason", "broker_response"])

    def mark_failed(self, reason: str) -> None:
        """
        Network / system error.
        Retry baaki hain toh next_retry_at set hoga (exponential backoff).
        Celery Beat retry_pending_orders task isko pick karega.
        """
        self.status           = self.Status.FAILED
        self.rejection_reason = reason
        self.retry_count     += 1

        if self.can_retry:
            # Exponential backoff: 1 min, 2 min, 4 min
            backoff_minutes    = 2 ** (self.retry_count - 1)
            self.next_retry_at = timezone.now() + timedelta(minutes=backoff_minutes)

        self.save(update_fields=[
            "status", "rejection_reason",
            "retry_count", "next_retry_at",
        ])

    def reset_for_retry(self) -> None:
        """Reset order to PENDING state for retry (called by Celery task)."""
        self.status        = self.Status.PENDING
        self.next_retry_at = None
        self.save(update_fields=["status", "next_retry_at"])