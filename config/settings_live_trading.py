# profitxai/settings_live_trading.py
#
 
# EXISTING CELERY_BEAT_SCHEDULE mein yeh entries ADD karo (overwrite mat karo):
 
LIVE_TRADING_BEAT_SCHEDULE = {
    # ── Signal Detection (har 15 second) ──────────────────────────
    "detect-live-signals": {
        "task":    "live_trading.detect_signals",
        "schedule": 15.0,
        "options": {"queue": "signals", "expires": 14},
    },
    # ── SEMI_AUTO Signal Expiry (har 5 second) ────────────────────
    "expire-semi-auto-signals": {
        "task":    "live_trading.expire_pending_signals",
        "schedule": 5.0,
        "options": {"queue": "signals", "expires": 4},
    },
    # ── Existing broker tasks (TOUCH NAHI KARO — yahan sirf reference) ──
    # "retry-pending-orders": already defined in apps.brokers
    # "start-feeds": already defined in apps.brokers
}

LIVE_TRADING_TASK_ROUTES = {
    # Live Trading tasks
    "live_trading.detect_signals":        {"queue": "signals"},
    "live_trading.expire_pending_signals":{"queue": "signals"},
    "live_trading.execute_trade":         {"queue": "orders"},
    "live_trading.manual_order_place":    {"queue": "orders"},
    "live_trading.close_session_summary": {"queue": "default"},
 
    # Existing broker tasks (DO NOT CHANGE)
    "apps.brokers.tasks.place_broker_order":    {"queue": "orders"},
    "apps.brokers.tasks.retry_pending_orders":  {"queue": "orders"},
    "apps.brokers.tasks.start_all_active_feeds":{"queue": "default"},
 
    # Existing notification tasks (DO NOT CHANGE)
    "notifications.send":      {"queue": "default"},
    "notifications.ws":        {"queue": "default"},
    "notifications.push":      {"queue": "default"},
    "notifications.email":     {"queue": "default"},
    "notifications.broadcast": {"queue": "default"},
}
