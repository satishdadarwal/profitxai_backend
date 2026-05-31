# apps/backtest/urls.py

from django.urls import path

from .views import BacktestDetailView, BacktestListCreateView, BacktestRunView

app_name = "backtest"

urlpatterns = [
    # ✅ BUG #2 FIX — Pehle: backtest/ aur backtest/run/ DONO ek hi view pe the
    # Problem: REST convention galat tha + duplicate POST se duplicate runs ban sakte the
    #
    # Fix:
    #   GET  backtest/        → sirf list fetch karo  (BacktestListCreateView)
    #   POST backtest/run/    → naya backtest start karo (BacktestRunView — alag view)
    #   GET  backtest/<id>/   → single run detail     (BacktestDetailView)

    path("", BacktestListCreateView.as_view(), name="list"),          # GET only
    path("run/", BacktestRunView.as_view(), name="run"),              # POST only
    path("<uuid:run_id>/", BacktestDetailView.as_view(), name="detail"),
]
