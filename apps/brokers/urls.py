from django.urls import path

from .views import FyersSavePinView, FyersAutoRefreshStatusView
from .views import BrokerRemoveView 
from .views import (
    BrokerAccountCreateView,
    BrokerAccountListView,
    FyersAuthURLView,
    FyersCallbackView,
    FyersTokenRefreshView,
)

urlpatterns = [
    path("", BrokerAccountListView.as_view()),
    path("connect/", BrokerAccountCreateView.as_view()),
    path("<int:pk>/remove/", BrokerRemoveView.as_view()),  # ← add this
    path("fyers/auth-url/", FyersAuthURLView.as_view()),
    path("fyers/callback/", FyersCallbackView.as_view()),
    path("fyers/refresh-token/", FyersTokenRefreshView.as_view()),
    path("fyers/save-pin/", FyersSavePinView.as_view()),
    path("fyers/refresh-status/", FyersAutoRefreshStatusView.as_view()),
]
