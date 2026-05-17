# apps/paper_trading/urls.py
# Add these to your existing URL patterns

from django.urls import path
from .views import (
    # Existing views
    PaperAccountView,
    OpenTradeView,
    CloseTradeView,
    CloseAllTradesView,
    UpdateTradePriceView,
    ResetAccountView,
    TopUpAccountView,
    TradeListView,
    GetTierInfoView,
    
    # New risk management views
    UpdateRiskSettingsView,
    GetRiskStatusView,
)

urlpatterns = [
    # ─────────────────────────────────────────────
    # ACCOUNT
    # ─────────────────────────────────────────────
    path('account/', PaperAccountView.as_view(), name='paper-account'),
    path('reset/', ResetAccountView.as_view(), name='reset-account'),
    path('topup/', TopUpAccountView.as_view(), name='topup-account'),
    
    # ─────────────────────────────────────────────
    # RISK MANAGEMENT (NEW)
    # ─────────────────────────────────────────────
    path('risk-settings/', UpdateRiskSettingsView.as_view(), name='update-risk-settings'),
    path('risk-status/', GetRiskStatusView.as_view(), name='get-risk-status'),
    
    # ─────────────────────────────────────────────
    # TRADES
    # ─────────────────────────────────────────────
    path('trades/', TradeListView.as_view(), name='trade-list'),
    path('open/', OpenTradeView.as_view(), name='open-trade'),
    path('close/<uuid:trade_id>/', CloseTradeView.as_view(), name='close-trade'),
    path('close-all/', CloseAllTradesView.as_view(), name='close-all-trades'),
    path('update-price/', UpdateTradePriceView.as_view(), name='update-price'),
    path('tier-info/', GetTierInfoView.as_view(), name='tier-info'),
]