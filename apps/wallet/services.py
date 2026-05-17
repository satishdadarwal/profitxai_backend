"""
apps/wallet/services.py

Pure business logic — no HTTP, no Celery.
Views aur orders/services dono yahan se call karte hain.
"""

import logging
from decimal import Decimal

from django.db import transaction as db_transaction

from .models import Transaction, Wallet

logger = logging.getLogger(__name__)


# ───────────────────────────────────────────────────────────────
# Exceptions
# ───────────────────────────────────────────────────────────────


class WalletError(Exception):
    """Base class for wallet errors."""


class InsufficientFundsError(WalletError):
    """Balance kam hai."""


class WalletNotFoundError(WalletError):
    """Wallet exist nahi karta."""


class InvalidAmountError(WalletError):
    """Amount zero ya negative hai."""


# ───────────────────────────────────────────────────────────────
# 1. Get or Create Wallet
# ───────────────────────────────────────────────────────────────


def get_or_create_wallet(*, user, currency: str = Wallet.Currency.USDT) -> Wallet:
    """
    User ka wallet fetch karo — agar nahi hai toh banao.
    """
    wallet, created = Wallet.objects.get_or_create(
        user=user,
        currency=currency.upper(),
        defaults={"available_balance": Decimal("0"), "locked_balance": Decimal("0")},
    )
    if created:
        logger.info("Wallet created | user=%s | currency=%s", user.id, currency)
    return wallet


# ───────────────────────────────────────────────────────────────
# 2. Deposit
# ───────────────────────────────────────────────────────────────


def deposit(
    *,
    user,
    amount: Decimal,
    currency: str = Wallet.Currency.USDT,
    reference: str = "",
    notes: str = "",
) -> Transaction:
    """
    User ke wallet mein paisa add karo.
    """
    amount = Decimal(str(amount))
    if amount <= 0:
        raise InvalidAmountError("Deposit amount must be positive.")

    with db_transaction.atomic():
        wallet, _ = Wallet.objects.select_for_update().get_or_create(
            user=user,
            currency=currency.upper(),
            defaults={
                "available_balance": Decimal("0"),
                "locked_balance": Decimal("0"),
            },
        )

        wallet.available_balance += amount
        wallet.save(update_fields=["available_balance", "updated_at"])

        txn = Transaction.objects.create(
            wallet=wallet,
            transaction_type=Transaction.TxType.DEPOSIT,
            status=Transaction.Status.COMPLETED,
            amount=amount,
            fee=Decimal("0"),
            reference=reference,
            notes=notes,
        )

    logger.info(
        "Deposit | user=%s | currency=%s | amount=%s | ref=%s",
        user.id,
        currency,
        amount,
        reference,
    )
    return txn


# ───────────────────────────────────────────────────────────────
# 3. Withdraw
# ───────────────────────────────────────────────────────────────


def withdraw(
    *,
    user,
    amount: Decimal,
    currency: str = Wallet.Currency.USDT,
    fee: Decimal = Decimal("0"),
    reference: str = "",
    notes: str = "",
) -> Transaction:
    """
    User ke wallet se paisa nikalo.
    """
    amount = Decimal(str(amount))
    fee = Decimal(str(fee))

    if amount <= 0:
        raise InvalidAmountError("Withdrawal amount must be positive.")

    total_debit = amount + fee

    with db_transaction.atomic():
        try:
            wallet = Wallet.objects.select_for_update().get(
                user=user, currency=currency.upper()
            )
        except Wallet.DoesNotExist as exc:
            raise WalletNotFoundError(
                f"No {currency} wallet found for user {user.id}."
            ) from exc

        if wallet.available_balance < total_debit:
            raise InsufficientFundsError(
                f"Need {total_debit} {currency}, available {wallet.available_balance}."
            )

        wallet.available_balance -= total_debit
        wallet.save(update_fields=["available_balance", "updated_at"])

        txn = Transaction.objects.create(
            wallet=wallet,
            transaction_type=Transaction.TxType.WITHDRAWAL,
            status=Transaction.Status.COMPLETED,
            amount=amount,
            fee=fee,
            reference=reference,
            notes=notes,
        )

    logger.info(
        "Withdrawal | user=%s | currency=%s | amount=%s | fee=%s",
        user.id,
        currency,
        amount,
        fee,
    )
    return txn


# ───────────────────────────────────────────────────────────────
# 4. Get Balance Summary
# ───────────────────────────────────────────────────────────────


def get_balance_summary(*, user) -> list[dict]:
    """
    User ke sab wallets ka balance return karo.
    """
    wallets = Wallet.objects.filter(user=user)
    return [
        {
            "currency": w.currency,
            "available": str(w.available_balance),
            "locked": str(w.locked_balance),
            "total": str(w.total_balance),
        }
        for w in wallets
    ]


# ───────────────────────────────────────────────────────────────
# 5. Record Trade Settlement
# ───────────────────────────────────────────────────────────────


def record_trade_settlement(
    *,
    wallet: Wallet,
    amount: Decimal,
    fee: Decimal,
    trade_reference: str,
) -> Transaction:
    """
    Trade complete hone ke baad Transaction record banao.
    Wallet balance orders/services.py already update kar deta hai;
    yahan sirf ledger entry hoti hai.
    """
    return Transaction.objects.create(
        wallet=wallet,
        transaction_type=Transaction.TxType.TRADE_SETTLEMENT,
        status=Transaction.Status.COMPLETED,
        amount=amount,
        fee=fee,
        reference=trade_reference,
    )
