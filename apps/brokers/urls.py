from django.urls import path

from .views import FyersSavePinView, FyersAutoRefreshStatusView
from .views import BrokerRemoveView
from .views import (
    BrokerAccountCreateView,
    BrokerAccountListView,
    FyersAuthURLView,
    FyersCallbackView,
    FyersTokenRefreshView,
    FyersAutoLoginView,
    BrokerFundsView,
    # ✅ Dhan
    DhanConnectView,
    DhanTokenStatusView,
    # ✅ Zerodha
    ZerodhaAuthURLView,
    ZerodhaCallbackView,
    ZerodhaTokenStatusView,
    ZerodhaFundsView,
)

urlpatterns = [
    path("", BrokerAccountListView.as_view()),
    path("connect/", BrokerAccountCreateView.as_view()),
    path("<int:pk>/remove/", BrokerRemoveView.as_view()),

    # ── Fyers ──────────────────────────────────────────────────────────────
    path("fyers/auth-url/",       FyersAuthURLView.as_view()),
    path("fyers/callback/",       FyersCallbackView.as_view()),
    path("fyers/refresh-token/",  FyersTokenRefreshView.as_view()),
    path("fyers/save-pin/",       FyersSavePinView.as_view()),
    path("fyers/refresh-status/", FyersAutoRefreshStatusView.as_view()),
    path("fyers/auto-login/",     FyersAutoLoginView.as_view(), name="fyers-auto-login"),

    # ── Dhan ───────────────────────────────────────────────────────────────
    path("dhan/connect/", DhanConnectView.as_view(), name="dhan-connect"),
    path("dhan/status/",  DhanTokenStatusView.as_view(), name="dhan-status"),

    # ── Zerodha ────────────────────────────────────────────────────────────
    # GET → login URL return karo (Flutter browser mein khulegaa)
    path("zerodha/auth-url/",  ZerodhaAuthURLView.as_view(), name="zerodha-auth-url"),
    # GET → Zerodha redirect callback (request_token → access_token)
    path("zerodha/callback/",  ZerodhaCallbackView.as_view(), name="zerodha-callback"),
    # GET → token valid/expired check
    path("zerodha/status/",    ZerodhaTokenStatusView.as_view(), name="zerodha-status"),
    # GET → live balance
    path("zerodha/funds/",     ZerodhaFundsView.as_view(), name="zerodha-funds"),

    # ── Common ─────────────────────────────────────────────────────────────
    path("funds/", BrokerFundsView.as_view(), name="broker-funds"),
]