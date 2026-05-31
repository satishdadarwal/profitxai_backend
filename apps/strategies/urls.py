# apps/strategies/urls.py
# ✅ FIXED: Import errors + Admin endpoint sahi jagah

from django.urls import path
from .views import (
    AlgoListView,
    StrategyListCreateView,
    StrategyDetailView,
    StrategyStartView,
    StrategyStopView,
    StrategyToggleModeView,
    StrategySignalListView,
    StrategyPerformanceView,
    StrategySnapshotListView,
    StrategyBacktestView,
    StrategyActivityLogView,
    ManualOrderView,
    CapitalWarningView,
    AdminGlobalStrategyCreateView,
    UserStrategyPreferenceView,
)

urlpatterns = [
    # ── Algo registry ───────────────────────────────────────────
    path("algos/", AlgoListView.as_view(), name="algo-list"),

    # ── List + Create ───────────────────────────────────────────
    path("", StrategyListCreateView.as_view(), name="strategy-list-create"),

    # ── Admin: Global Strategy Create/List (staff-only) ─────────
    # ✅ FIX: Yeh pehle hona chahiye taaki "admin/global/" match ho sake
    #    nahi to "<uuid:strategy_id>/" catch kar lega
    path("admin/global/", AdminGlobalStrategyCreateView.as_view(), name="admin-global-strategy"),

    # ── Activity Log (all user strategies) ─────────────────────
    path("activity/", StrategyActivityLogView.as_view(), name="strategy-activity"),

    # ── Retrieve + Update + Delete ──────────────────────────────
    path("<uuid:strategy_id>/", StrategyDetailView.as_view(), name="strategy-detail"),

    # ── Actions ─────────────────────────────────────────────────
    path("<uuid:strategy_id>/start/", StrategyStartView.as_view(), name="strategy-start"),
    path("<uuid:strategy_id>/stop/", StrategyStopView.as_view(), name="strategy-stop"),
    path("<uuid:strategy_id>/toggle-mode/", StrategyToggleModeView.as_view(), name="strategy-toggle-mode"),

    # ✅ User apna preferred mode set kare (paper/live) per strategy
    path("<uuid:strategy_id>/preference/", UserStrategyPreferenceView.as_view(), name="strategy-preference"),

    # ── Manual + Capital Warning ────────────────────────────────
    path("<uuid:strategy_id>/manual-order/", ManualOrderView.as_view(), name="strategy-manual-order"),
    path("<uuid:strategy_id>/capital-warning/", CapitalWarningView.as_view(), name="strategy-capital-warning"),

    # ── Signals ─────────────────────────────────────────────────
    path("<uuid:strategy_id>/signals/", StrategySignalListView.as_view(), name="strategy-signals"),

    # ── Performance ─────────────────────────────────────────────
    path("<uuid:strategy_id>/performance/", StrategyPerformanceView.as_view(), name="strategy-performance"),
    path("<uuid:strategy_id>/snapshots/", StrategySnapshotListView.as_view(), name="strategy-snapshots"),

    # ── Backtest ────────────────────────────────────────────────
    path("<uuid:strategy_id>/backtest/", StrategyBacktestView.as_view(), name="strategy-backtest"),
]