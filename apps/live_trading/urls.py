# apps/live_trading/urls.py
# ✅ UPDATED: Screener endpoints added (incl. performance)

from django.urls import path
from . import views
from . import screener_views   # ← new file

app_name = "live_trading"

urlpatterns = [
    # ─────────────────────────────────────────────────────────────
    # Session Management
    # ─────────────────────────────────────────────────────────────
    path("session/start/",                   views.start_session,       name="start_session"),
    path("session/stop/",                    views.stop_session,        name="stop_session"),
    path("session/<int:session_id>/summary/",views.session_summary,     name="session_summary"),

    # ─────────────────────────────────────────────────────────────
    # Signal Actions (SEMI_AUTO confirm / ignore)
    # ─────────────────────────────────────────────────────────────
    path("signals/<int:signal_id>/confirm/", views.confirm_signal,      name="confirm_signal"),
    path("signals/<int:signal_id>/ignore/",  views.ignore_signal,       name="ignore_signal"),

    # ─────────────────────────────────────────────────────────────
    # Manual Trading
    # ─────────────────────────────────────────────────────────────
    path("manual-order/",                    views.place_manual_order,  name="place_manual_order"),

    # ─────────────────────────────────────────────────────────────
    # Activity Log
    # ─────────────────────────────────────────────────────────────
    path("activity/",                        views.activity_log,        name="activity_log"),

    # ─────────────────────────────────────────────────────────────
    # Strategy & Sessions
    # ─────────────────────────────────────────────────────────────
    path("strategies/",                      views.get_strategies,      name="get_strategies"),
    path("sessions/",                        views.get_active_sessions, name="get_active_sessions"),
    path("dashboard/",                       views.dashboard_stats,     name="dashboard_stats"),

    # ─────────────────────────────────────────────────────────────
    # Live Signals (existing)
    # ─────────────────────────────────────────────────────────────
    path("signals/pending/",                 views.get_pending_signals, name="get_pending_signals"),
    path("signals/recent/",                  views.get_recent_signals,  name="get_recent_signals"),

    # ─────────────────────────────────────────────────────────────
    # ✅ ICT SCREENER
    # ─────────────────────────────────────────────────────────────
    #   GET  /api/live-trading/screener/signals/      → recent signals (plan-gated)
    #   POST /api/live-trading/screener/scan/         → manual on-demand scan (Pro+)
    #   GET  /api/live-trading/screener/stats/        → aaj ka stats
    #   GET  /api/live-trading/screener/performance/  → weekly historical performance
    # ─────────────────────────────────────────────────────────────
    path("screener/signals/",     screener_views.screener_signals,     name="screener_signals"),
    path("screener/scan/",        screener_views.screener_scan,        name="screener_scan"),
    path("screener/stats/",       screener_views.screener_stats,       name="screener_stats"),
    path("screener/performance/", screener_views.screener_performance, name="screener_performance"),
    path("screener/preference/",   screener_views.screener_preference,   name="screener_preference"),
]