"""
apps/wallet/serializers.py

API response aur input validation ke liye serializers.
"""

from decimal import Decimal

from rest_framework import serializers

from .models import Transaction, Wallet


class WalletSerializer(serializers.ModelSerializer):
    """Read-only wallet info — balance dikhane ke liye."""

    total_balance = serializers.DecimalField(
        max_digits=28, decimal_places=8, read_only=True
    )

    class Meta:
        model = Wallet
        fields = [
            "id",
            "currency",
            "available_balance",
            "locked_balance",
            "total_balance",
            "updated_at",
        ]
        read_only_fields = fields


class TransactionSerializer(serializers.ModelSerializer):
    """Read-only transaction history."""

    net_amount = serializers.DecimalField(
        max_digits=28, decimal_places=8, read_only=True
    )
    currency = serializers.CharField(source="wallet.currency", read_only=True)

    class Meta:
        model = Transaction
        fields = [
            "id",
            "currency",
            "transaction_type",
            "status",
            "amount",
            "fee",
            "net_amount",
            "reference",
            "notes",
            "created_at",
        ]
        read_only_fields = fields


class DepositSerializer(serializers.Serializer):
    """Deposit request validate karne ke liye."""

    amount = serializers.DecimalField(
        max_digits=28, decimal_places=8, min_value=Decimal("0.00000001")
    )
    currency = serializers.ChoiceField(
        choices=Wallet.Currency.choices, default=Wallet.Currency.USDT
    )
    reference = serializers.CharField(max_length=255, required=False, default="")
    notes = serializers.CharField(max_length=500, required=False, default="")


class WithdrawSerializer(serializers.Serializer):
    """Withdraw request validate karne ke liye."""

    amount = serializers.DecimalField(
        max_digits=28, decimal_places=8, min_value=Decimal("0.00000001")
    )
    currency = serializers.ChoiceField(
        choices=Wallet.Currency.choices, default=Wallet.Currency.USDT
    )
    fee = serializers.DecimalField(
        max_digits=28, decimal_places=8, default=Decimal("0"), min_value=Decimal("0")
    )
    reference = serializers.CharField(max_length=255, required=False, default="")
    notes = serializers.CharField(max_length=500, required=False, default="")
