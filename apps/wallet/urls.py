from django.urls import path

from .views import (
    DepositView,
    TransactionListView,
    WalletDetailView,
    WalletListView,
    WithdrawView,
)

APP_NAME = "wallet"

urlpatterns = [
    # GET  /api/wallet/               → sab wallets
    path("", WalletListView.as_view(), name="wallet-list"),
    # GET  /api/wallet/<currency>/    → ek currency ka wallet
    path("<str:currency>/", WalletDetailView.as_view(), name="wallet-detail"),
    # POST /api/wallet/deposit/       → paisa add karo
    path("deposit/", DepositView.as_view(), name="wallet-deposit"),
    # POST /api/wallet/withdraw/      → paisa nikalo
    path("withdraw/", WithdrawView.as_view(), name="wallet-withdraw"),
    # GET  /api/wallet/transactions/  → history
    path("transactions/", TransactionListView.as_view(), name="wallet-transactions"),
]
