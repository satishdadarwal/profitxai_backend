"""
apps/wallet/models.py

Har user ke paas ek ya zyada Wallet hote hain (USDT, BTC, etc.)
Transaction har debit/credit ka immutable record hai.
"""

import uuid

from django.contrib.auth import get_user_model
from django.db import models
from django.utils import timezone

User = get_user_model()


class Wallet(models.Model):
    """
    Ek user ka ek currency ke liye balance container.

    - available_balance  → trade ya withdraw ho sakta hai
    - locked_balance     → open orders ke liye hold mein hai
    - total_balance      → available + locked (property)

    Ek user ke multiple wallets ho sakte hain:
        user=alice, currency=USDT  → fiat/stable wallet
        user=alice, currency=BTC   → crypto wallet
    """

    class Currency(models.TextChoices):
        USDT = "USDT", "Tether (USDT)"
        BTC = "BTC", "Bitcoin"
        ETH = "ETH", "Ethereum"
        INR = "INR", "₹ Indian Rupee"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="wallets")
    currency = models.CharField(
        max_length=20,
        default=Currency.USDT,
        help_text="Currency symbol, e.g. USDT, BTC",
    )

    available_balance = models.DecimalField(
        max_digits=28,
        decimal_places=8,
        default=0,
        help_text="Trade ya withdraw ke liye available.",
    )
    locked_balance = models.DecimalField(
        max_digits=28,
        decimal_places=8,
        default=0,
        help_text="Open orders ke liye lock hua balance.",
    )

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("user", "currency")
        ordering = ["user", "currency"]
        verbose_name = "Wallet"
        verbose_name_plural = "Wallets"

    def __str__(self):
        return f"{self.user_id} | {self.currency} | avail={self.available_balance}"

    @property
    def total_balance(self):
        return self.available_balance + self.locked_balance

    def credit(self, amount):
        self.available_balance += amount
        self.save(update_fields=["available_balance", "updated_at"])

    def debit(self, amount):
        if amount > self.available_balance:
            from apps.wallet.services import InsufficientFundsError

            raise InsufficientFundsError(
                f"Need {amount} {self.currency}, available {self.available_balance}."
            )
        self.available_balance -= amount
        self.save(update_fields=["available_balance", "updated_at"])

    def lock(self, amount):
        self.debit(amount)
        self.locked_balance += amount
        self.save(update_fields=["locked_balance", "updated_at"])

    def unlock(self, amount):
        release = min(amount, self.locked_balance)
        self.locked_balance -= release
        self.available_balance += release
        self.save(update_fields=["available_balance", "locked_balance", "updated_at"])


class Transaction(models.Model):
    """
    Har wallet movement ka immutable ledger record.
    Delete nahi hota — sirf append hota hai.
    """

    class TxType(models.TextChoices):
        DEPOSIT = "deposit", "Deposit"
        WITHDRAWAL = "withdrawal", "Withdrawal"
        TRADE_SETTLEMENT = "trade_settlement", "Trade Settlement"
        FEE = "fee", "Fee"
        LOCK = "lock", "Funds Locked"
        UNLOCK = "unlock", "Funds Unlocked"
        ADJUSTMENT = "adjustment", "Manual Adjustment"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    wallet = models.ForeignKey(
        Wallet, on_delete=models.PROTECT, related_name="transactions"
    )
    transaction_type = models.CharField(
        max_length=30, choices=TxType.choices, default=TxType.TRADE_SETTLEMENT
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.COMPLETED
    )
    amount = models.DecimalField(
        max_digits=28, decimal_places=8, help_text="Transaction ki gross amount."
    )
    fee = models.DecimalField(
        max_digits=28,
        decimal_places=8,
        default=0,
        help_text="Is transaction par lagi fee.",
    )
    reference = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="External ya internal reference (trade_id, txn_hash, etc.)",
    )
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Transaction"
        verbose_name_plural = "Transactions"

    def __str__(self):
        return f"{self.transaction_type} | {self.amount} {self.wallet.currency} | ref={self.reference or '-'}"

    @property
    def net_amount(self):
        return self.amount - self.fee
