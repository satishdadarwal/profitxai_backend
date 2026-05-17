"""
apps/wallet/admin.py

Django admin mein Wallet aur Transaction dikhao.
"""

from django.contrib import admin

from .models import Transaction, Wallet


@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    """Admin view for Wallet model."""

    list_display = (
        "user",
        "currency",
        "available_balance",
        "locked_balance",
        "total_balance",
        "updated_at",
    )
    list_filter = ("currency",)
    search_fields = ("user__email", "user__username")
    readonly_fields = ("id", "created_at", "updated_at")

    def total_balance(self, obj):
        """Total balance column."""
        return obj.total_balance

    total_balance.short_description = "Total"


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    """Admin view for Transaction model."""

    list_display = (
        "wallet",
        "transaction_type",
        "status",
        "amount",
        "fee",
        "reference",
        "created_at",
    )
    list_filter = ("transaction_type", "status", "wallet__currency")
    search_fields = ("reference", "wallet__user__email")
    readonly_fields = ("id", "created_at")

    def has_change_permission(self, request, obj=None):
        """Transactions immutable hain — edit band."""
        return False

    def has_delete_permission(self, request, obj=None):
        """Transactions delete nahi honi chahiye."""
        return False
