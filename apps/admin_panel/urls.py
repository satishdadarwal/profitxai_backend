# apps/admin_panel/urls.py

from django.urls import path

from . import views

app_name = "admin_panel"

urlpatterns = [
    # ── Dashboard ──────────────────────────────────────────────
    path("", views.AdminDashboardView.as_view(), name="dashboard"),
    # ── Users ──────────────────────────────────────────────────
    path("users/", views.AdminUserListView.as_view(), name="user_list"),
    path(
        "users/<int:user_id>/", views.AdminUserDetailView.as_view(), name="user_detail"
    ),
    path(
        "users/<int:user_id>/toggle/",
        views.AdminUserToggleActiveView.as_view(),
        name="user_toggle",
    ),
    # ── Trades ─────────────────────────────────────────────────
    path("trades/", views.AdminTradeListView.as_view(), name="trade_list"),
    path(
        "trades/<int:trade_id>/",
        views.AdminTradeDetailView.as_view(),
        name="trade_detail",
    ),
    # ── Orders ─────────────────────────────────────────────────
    path("orders/", views.AdminOrderListView.as_view(), name="order_list"),
    path(
        "orders/<int:order_id>/cancel/",
        views.AdminOrderCancelView.as_view(),
        name="order_cancel",
    ),
    # ── Assets ─────────────────────────────────────────────────
    path("assets/", views.AdminAssetListView.as_view(), name="asset_list"),
    path(
        "assets/<int:asset_id>/toggle/",
        views.AdminAssetToggleView.as_view(),
        name="asset_toggle",
    ),
    # ── Wallets / Transactions ──────────────────────────────────
    path("wallets/", views.AdminWalletListView.as_view(), name="wallet_list"),
    path(
        "wallets/transactions/",
        views.AdminTransactionListView.as_view(),
        name="transaction_list",
    ),
]
