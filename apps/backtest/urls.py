# apps/backtest/urls.py

from django.urls import path

from .views import BacktestDetailView, BacktestListCreateView

app_name = "backtest"

urlpatterns = [
    path("", BacktestListCreateView.as_view(), name="list_create"),
    path(
        "run/", BacktestListCreateView.as_view(), name="run"
    ),  # ✅ Flutter yahi call karta hai
    path("<uuid:run_id>/", BacktestDetailView.as_view(), name="detail"),
]
