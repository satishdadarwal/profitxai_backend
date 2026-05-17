# apps/live_trading/urls.py


from django.urls import path
from . import views

app_name = "live_trading"

urlpatterns = [
    # ─────────────────────────────────────────────────────────────
    # ORIGINAL ENDPOINTS (Existing)
    # ─────────────────────────────────────────────────────────────
    
    # Session Management
    path("session/start/", views.start_session, name="start_session"),
    path("session/stop/", views.stop_session, name="stop_session"),
    path("session/<int:session_id>/summary/", views.session_summary, name="session_summary"),
    
    # Signal Actions
    path("signals/<int:signal_id>/confirm/", views.confirm_signal, name="confirm_signal"),
    path("signals/<int:signal_id>/ignore/", views.ignore_signal, name="ignore_signal"),
    
    # Manual Trading
    path("manual-order/", views.place_manual_order, name="place_manual_order"),
    
    # Activity Log
    path("activity/", views.activity_log, name="activity_log"),
    
    # ─────────────────────────────────────────────────────────────
    # NEW ENDPOINTS (✅ FIX for "No strategies" issue)
    # ─────────────────────────────────────────────────────────────
    
    # Strategy Management
    path("strategies/", views.get_strategies, name="get_strategies"),
    
    # Sessions List
    path("sessions/", views.get_active_sessions, name="get_active_sessions"),
    
    # Dashboard & Stats
    path("dashboard/", views.dashboard_stats, name="dashboard_stats"),
    
    # Signals
    path("signals/pending/", views.get_pending_signals, name="get_pending_signals"),
    path("signals/recent/", views.get_recent_signals, name="get_recent_signals"),
]

 