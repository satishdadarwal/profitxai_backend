from django.urls import path
from . import views
from .views import StrategyActivityLogView

urlpatterns = [
    # List + Create
    path("algos/", views.AlgoListView.as_view()),
    path("", views.StrategyListCreateView.as_view()),

    # Retrieve + Update + Delete
    path("<uuid:strategy_id>/", views.StrategyDetailView.as_view()),

    # Actions
    path("<uuid:strategy_id>/start/", views.StrategyStartView.as_view()),
    path("<uuid:strategy_id>/stop/", views.StrategyStopView.as_view()),
    path("<uuid:strategy_id>/toggle-mode/", views.StrategyToggleModeView.as_view()),

    # Signals
    path("<uuid:strategy_id>/signals/", views.StrategySignalListView.as_view()),

    # Performance
    path("<uuid:strategy_id>/performance/", views.StrategyPerformanceView.as_view()),
    path("<uuid:strategy_id>/snapshots/", views.StrategySnapshotListView.as_view()),

    # ✅ Activity Log
    path("activity/", StrategyActivityLogView.as_view(), name="strategy-activity"),
]