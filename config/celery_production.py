# config/celery_production.py
"""
Production-Grade Celery Configuration
====================================

Multi-queue architecture with priority routing:
- critical: Order execution (highest priority)
- orders: Order management
- signals: Signal detection & expiry (live trading)
- ticks: Tick processing
- strategies: Strategy execution
- notifications: User notifications
- default: Background tasks

Worker Commands:
    # Critical orders worker - dedicated CPU
    celery -A config worker -Q critical --pool=solo --concurrency=1 -n critical@%h

    # Signals worker - low latency, dedicated
    celery -A config worker -Q signals --concurrency=2 -n signals@%h

    # High-priority worker
    celery -A config worker -Q orders,ticks --concurrency=4 -n highpri@%h

    # Strategy worker
    celery -A config worker -Q strategies --concurrency=8 -n strategies@%h

    # Background worker
    celery -A config worker -Q default,notifications --concurrency=4 -n background@%h
"""

import hashlib
import json
import logging
import os
import sys
from functools import wraps

from celery import Celery
from celery.schedules import crontab, schedule
from celery.signals import (
    task_failure,
    task_retry,
    task_success,
    worker_ready,
    worker_shutdown,
)
from kombu import Exchange, Queue

logger = logging.getLogger(__name__)

# ── Django settings ──
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

# ── Celery app ──
app = Celery("profitxai")
app.config_from_object("django.conf:settings", namespace="CELERY")


# ────────────────────────────────────────────────────────────────────
#  Exchanges
# ────────────────────────────────────────────────────────────────────
critical_exchange   = Exchange("critical",    type="direct", durable=True)
orders_exchange     = Exchange("orders",      type="direct", durable=True)
signals_exchange    = Exchange("signals",     type="direct", durable=True)
ticks_exchange      = Exchange("ticks",       type="direct", durable=True)
strategies_exchange = Exchange("strategies",  type="direct", durable=True)
default_exchange    = Exchange("default",     type="direct", durable=True)


# ────────────────────────────────────────────────────────────────────
#  Queue Definitions
# ────────────────────────────────────────────────────────────────────
app.conf.task_queues = (
    # Critical — highest priority, order execution
    Queue(
        "critical",
        exchange=critical_exchange,
        routing_key="critical",
        queue_arguments={"x-max-priority": 10},
        priority=10,
    ),
    # Orders — high priority
    Queue(
        "orders",
        exchange=orders_exchange,
        routing_key="orders",
        queue_arguments={"x-max-priority": 10},
        priority=9,
    ),
    # Ticks — market data
    Queue(
        "ticks",
        exchange=ticks_exchange,
        routing_key="ticks",
        queue_arguments={"x-max-priority": 10},
        priority=8,
    ),
    # Strategies — strategy execution
    Queue(
        "strategies",
        exchange=strategies_exchange,
        routing_key="strategies",
        queue_arguments={"x-max-priority": 10},
        priority=7,
    ),
    # Signals — live trading signal detection
    Queue(
        "signals",
        exchange=signals_exchange,
        routing_key="signals",
        queue_arguments={"x-max-priority": 10},
        priority=6,
    ),
    # Notifications — lower priority
    Queue(
        "notifications",
        exchange=default_exchange,
        routing_key="notifications",
        priority=5,
    ),
    # Default — background tasks
    Queue(
        "default",
        exchange=default_exchange,
        routing_key="default",
        priority=5,
    ),
)


# ────────────────────────────────────────────────────────────────────
#  Task Routing
#  ✅ FIX: Wildcard glob ('tasks.*') Celery mein kaam nahi karta.
#  Saare tasks explicitly list kiye hain.
#  Best practice: har task pe queue= directly define karo.
# ────────────────────────────────────────────────────────────────────
app.conf.task_routes = {
    # ── Critical ──
    "apps.orders.tasks.execute_order":       {"queue": "critical",  "priority": 10},
    "apps.orders.tasks.cancel_order":        {"queue": "critical",  "priority": 10},
    "apps.orders.tasks.modify_order":        {"queue": "critical",  "priority": 9},
    "apps.orders.tasks.monitor_order_fill":  {"queue": "orders",    "priority": 9},

    # ── Ticks ──
    "apps.strategies.tasks.process_tick":       {"queue": "ticks", "priority": 8},
    "apps.strategies.tasks.process_tick_batch": {"queue": "ticks", "priority": 8},

    # ── Strategies ──
    "apps.strategies.tasks.run_strategy":           {"queue": "strategies", "priority": 7},
    "apps.strategies.tasks.execute_strategy_cycle": {"queue": "strategies", "priority": 7},

    # ── Live trading signals ──
    "live_trading.detect_signals":         {"queue": "signals", "priority": 7},
    "live_trading.expire_pending_signals": {"queue": "signals", "priority": 6},
    "live_trading.execute_trade":          {"queue": "orders",  "priority": 9},
    "live_trading.manual_order_place":     {"queue": "orders",  "priority": 8},
    "live_trading.close_session_summary":  {"queue": "default", "priority": 4},

    # ── Orders (non-critical) ──
    "apps.orders.tasks.update_order_status": {"queue": "orders", "priority": 6},
    "apps.orders.tasks.sync_orders":         {"queue": "orders", "priority": 5},

    # ── Brokers ──
    "apps.brokers.tasks.place_broker_order":            {"queue": "orders",  "priority": 9},
    "apps.brokers.tasks.retry_pending_orders":          {"queue": "orders",  "priority": 8},
    "apps.brokers.tasks.auto_refresh_master_fyers_token": {"queue": "default", "priority": 7},
    "apps.brokers.tasks.auto_refresh_fyers_tokens":     {"queue": "default", "priority": 6},
    "apps.brokers.tasks.start_all_active_feeds":        {"queue": "default", "priority": 5},
    "apps.brokers.tasks.stop_all_feeds":                {"queue": "default", "priority": 5},

    # ── Notifications ──
    "apps.notifications.tasks.send_notification_task":   {"queue": "notifications", "priority": 5},
    "apps.notifications.tasks.send_urgent_notification": {"queue": "notifications", "priority": 8},

    # ── Backtest ──
    "apps.backtest.tasks.run_backtest":    {"queue": "default", "priority": 3},
    "apps.backtest.tasks.process_results": {"queue": "default", "priority": 3},

    # ── Users ──
    "apps.users.tasks.send_welcome_email": {"queue": "default", "priority": 4},
    "apps.users.tasks.update_profile":     {"queue": "default", "priority": 4},

    # ── Paper trading ──
    "apps.paper_trading.tasks.execute_paper_trade": {"queue": "default", "priority": 4},

    # ── Wallet / subscriptions ──
    "apps.wallet.tasks.process_transaction":       {"queue": "default", "priority": 4},
    "apps.subscriptions.tasks.renew_subscription": {"queue": "default", "priority": 4},

    # ── Market ──
    "apps.market.tasks.update_cached_quotes": {"queue": "ticks", "priority": 8},

    # ── Common ──
    "apps.common.tasks.cleanup_old_celery_results": {"queue": "default", "priority": 3},
}


# ────────────────────────────────────────────────────────────────────
#  Beat Schedule
#
#  ✅ NEW: Dual mode support — master account YA individual accounts
#  
#  Production Setup (Recommended):
#  - Enable 'fyers-master-token-refresh' for centralized feed
#  - Disable 'fyers-individual-token-refresh'
#  
#  Dev/Testing Setup:
#  - Enable 'fyers-individual-token-refresh' for per-user feeds
#  - Disable 'fyers-master-token-refresh'
#
#  Architecture Decision:
#  ┌───────────────────────────────────────────────────────────────┐
#  │  PRODUCTION (Recommended)                                     │
#  │  ────────────────────────                                     │
#  │  ✅ Master Account (.env based)                              │
#  │     - Single WebSocket connection                             │
#  │     - One TOTP refresh (8:30 AM)                              │
#  │     - All users share feed                                    │
#  │     - Cost effective                                          │
#  │     - Simpler to monitor                                      │
#  │                                                                │
#  │  DEV/TESTING                                                  │
#  │  ────────────                                                 │
#  │  ✅ Individual Accounts (DB stored PIN/TOTP)                 │
#  │     - Multiple WebSocket connections                          │
#  │     - Per-user token refresh                                  │
#  │     - Useful for testing multi-account scenarios              │
#  └───────────────────────────────────────────────────────────────┘
# ────────────────────────────────────────────────────────────────────
beat_schedule = {
    "hourly-predictions": {
        "task": "predictions.generate_hourly_predictions",
        "schedule": crontab(minute=15, hour="9-15"),
    },
    "options-predictions": {
        "task": "options.generate_options_predictions",
        "schedule": crontab(minute="0,30", hour="9-15"),
    },
    # ── Live Trading ──────────────────────────────────────────────
    # NOTE: detect-live-signals aur expire-semi-auto-signals
    # settings_live_trading.py mein hain — yahan define NAHI karo (duplicate hoga)

    # ── Strategy Execution ────────────────────────────────────────
    # ✅ SINGLE SOURCE OF TRUTH: sirf yahan define hai.
    # settings.py se hata diya — DatabaseScheduler merge pe duplicate hota tha.
    "run-all-active-strategies": {
        "task": "strategies.run_all_active_strategies",
        "schedule": 60.0,
        "options": {
            "queue": "strategies",
            "priority": 7,
            "expires": 55,   # ✅ 60s cycle ke andar execute na hua toh drop karo
        },
    },

    # ── Order Management ──────────────────────────────────────────
    "retry-broker-orders": {
        "task": "apps.brokers.tasks.retry_pending_orders",
        "schedule": 60.0,
        "options": {"queue": "orders", "priority": 8, "expires": 55},
    },

    # ── Market Data ───────────────────────────────────────────────
    "update-market-data": {
        "task": "apps.market.tasks.update_cached_quotes",
        "schedule": 30.0,
        "options": {"queue": "ticks", "priority": 8, "expires": 25},
    },

    # ════════════════════════════════════════════════════════════════
    #  Fyers Daily Token + Feed Lifecycle
    #
    #  Timeline (IST):
    #  8:25 AM → Stop all feeds (pre-market cleanup)
    #  8:30 AM → Master token refresh + WS restart
    #  8:45 AM → Safety net start (agar 8:30 fail hua)
    #  3:35 PM → Stop feeds (market close)
    #
    #  ⚠️  start_all_active_feeds Beat se sirf 8:45 AM pe chalega.
    #  Worker start pe feed auto-start on_worker_ready se hoga.
    # ════════════════════════════════════════════════════════════════

    # Step 1 — 8:25 AM: Pre-market cleanup
    "fyers-pre-market-ws-stop": {
        "task": "apps.brokers.tasks.stop_all_feeds",
        "schedule": crontab(hour=8, minute=25),
        "options": {"queue": "default", "priority": 5},
    },

    # Step 2 — 8:30 AM: Master token refresh + WS restart
    "fyers-master-token-refresh": {
        "task": "apps.brokers.tasks.auto_refresh_master_fyers_token",
        "schedule": crontab(hour=8, minute=30),
        "options": {"queue": "default", "priority": 7},
    },

    # Step 3 — 8:45 AM: Safety net (agar 8:30 fail hua)
    "fyers-market-open-ws-start": {
        "task": "apps.brokers.tasks.start_all_active_feeds",
        "schedule": crontab(hour=8, minute=45),
        "options": {"queue": "default", "priority": 5},
    },

    # Step 4 — 3:35 PM: Market close cleanup
    "fyers-market-close-ws-stop": {
        "task": "apps.brokers.tasks.stop_all_feeds",
        "schedule": crontab(hour=15, minute=35),
        "options": {"queue": "default", "priority": 5},
    },

    # ── Hours-based predictions and option chain jobs ─────────────
    "generate-hourly-predictions": {
        "task": "predictions.generate_hourly_predictions",
        "schedule": crontab(minute=0),
        "options": {"queue": "default", "priority": 5},
    },
    "update-hourly-outcomes": {
        "task": "predictions.update_hourly_outcomes",
        "schedule": crontab(minute=30),
        "options": {"queue": "default", "priority": 5},
    },
    "fetch-all-option-chains": {
        "task": "options.fetch_all_chains",
        "schedule": crontab(minute="*/5"),
        "options": {"queue": "default", "priority": 5},
    },

    # ── Maintenance ───────────────────────────────────────────────
    "cleanup-old-tasks": {
        "task": "apps.common.tasks.cleanup_old_celery_results",
        "schedule": 3600.0,
        "options": {"queue": "default", "priority": 3},
    },
}


# ────────────────────────────────────────────────────────────────────
#  Celery Configuration
# ────────────────────────────────────────────────────────────────────
app.conf.update(
    # Serialization
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",

    # Timezone
    timezone="Asia/Kolkata",
    enable_utc=True,

    # Result backend
    result_backend="redis://localhost:6379/1",
    result_expires=3600,

    # Task execution
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_track_started=True,

    # Priority
    task_default_priority=5,
    task_queue_max_priority=10,

    # Performance
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=1000,

    # Timeouts
    task_soft_time_limit=300,
    task_time_limit=600,

    # Retries
    task_default_max_retries=3,
    task_default_retry_delay=5,

    # Logging
    worker_log_format="[%(asctime)s: %(levelname)s/%(processName)s] %(message)s",
    worker_task_log_format=(
        "[%(asctime)s: %(levelname)s/%(processName)s]"
        "[%(task_name)s(%(task_id)s)] %(message)s"
    ),

    # Broker
    broker_url="redis://localhost:6379/0",
    broker_connection_retry_on_startup=True,
    broker_connection_max_retries=10,

    # Beat
    beat_schedule=beat_schedule,
)


# ────────────────────────────────────────────────────────────────────
#  Auto-discover tasks
# ────────────────────────────────────────────────────────────────────
app.autodiscover_tasks([
    "apps.users",
    "apps.subscriptions",
    "apps.strategies",
    "apps.orders",
    "apps.notifications",
    "apps.backtest",
    "apps.paper_trading",
    "apps.market",
    "apps.live_trading",
    "apps.brokers",
    "apps.wallet",
    "apps.options",
    "apps.predictions",
    "apps.risk",
    "apps.common",
])


# ────────────────────────────────────────────────────────────────────
#  Windows fix
# ────────────────────────────────────────────────────────────────────
if sys.platform == "win32":
    # ✅ FIX: solo pool ki jagah threads use karo
    # solo = sirf 1 task ek waqt mein → rate limit + busy errors
    # threads = multiple concurrent tasks → sab symbols saath chalte hain
    app.conf.worker_pool = "threads"
    app.conf.worker_concurrency = 10
    logger.warning("Running on Windows — using threads pool (concurrency=10)")


# ────────────────────────────────────────────────────────────────────
#  Signal Handlers
# ────────────────────────────────────────────────────────────────────
@task_failure.connect
def on_task_failure(
    task_id=None,
    exception=None,
    traceback=None,
    sender=None,
    args=None,
    kwargs=None,
    **extra,
):
    """Handle task failures — critical tasks ko urgent notification bhejo."""
    logger.error(
        "❌ Task FAILED | task_id=%s | task=%s | error=%r | args=%s | kwargs=%s",
        task_id,
        getattr(sender, "name", "unknown"),
        exception,
        args,
        kwargs,
    )

    task_name = getattr(sender, "name", "")
    
    # Critical task failures — urgent notification
    if "execute_order" in task_name or "cancel_order" in task_name:
        try:
            user_id = None
            if args and len(args) >= 2:
                user_id = args[1]
            elif kwargs:
                user_id = kwargs.get("user_id")

            if user_id:
                from apps.notifications.tasks import send_urgent_notification
                send_urgent_notification.apply_async(
                    args=[user_id, f"Critical task failed: {task_name}"],
                    priority=10,
                )
            else:
                logger.warning(
                    "on_task_failure: user_id not found for critical task %s — "
                    "urgent notification skipped | args=%s kwargs=%s",
                    task_name, args, kwargs,
                )
        except Exception as exc:
            logger.error("on_task_failure: notification failed | %s", exc)
    
    # Master token refresh failures — admin alert
    elif "auto_refresh_master_fyers_token" in task_name:
        logger.critical(
            "🚨 MASTER FYERS TOKEN REFRESH FAILED | "
            "All users will lose feed if not fixed! | error=%r",
            exception
        )
        # TODO: Send admin SMS/email alert


@task_success.connect
def on_task_success(sender=None, result=None, **kwargs):
    """Critical tasks ke liye success log karo."""
    task_name = getattr(sender, "name", "unknown")
    
    if "execute_order" in task_name or "cancel_order" in task_name:
        logger.info("✅ Task SUCCESS | task=%s", task_name)
    
    elif "auto_refresh_master_fyers_token" in task_name:
        logger.info(
            "✅ Master Fyers token refresh SUCCESS | result=%s",
            result
        )


@task_retry.connect
def on_task_retry(
    task_id=None, exception=None, sender=None, args=None, kwargs=None, **extra
):
    """Retries log karo."""
    logger.warning(
        "🔄 Task RETRY | task_id=%s | task=%s | error=%r",
        task_id,
        getattr(sender, "name", "unknown"),
        exception,
    )


@worker_ready.connect
def on_worker_ready(sender=None, **kwargs):
    """
    Worker startup hook.

    ✅ FIX: Worker start pe feeds ek baar start karo — Redis lock se.
    Pehle koi guard nahi tha — har thread/restart pe duplicate feeds shuru ho
    jaate the (log mein 18 baar "Feed started" dikh raha tha).

    run_all_active_strategies yahan SE NAHI chalti — Beat ka kaam hai.
    """
    logger.info("🚀 Celery worker ready — %s", sender)
    if hasattr(sender, "controller"):
        pool = sender.controller.pool
        logger.info(
            "Worker config: concurrency=%s | pool=%s",
            pool.limit if pool else "unknown",
            sender.pool_cls,
        )

    # ── Feed startup — ek baar, Redis lock ke saath ─────────────────────────
    # Lock TTL = 30s: agar multiple workers ek saath start ho rahe hain toh
    # sirf pehla worker feeds start karega, baaki skip kar denge.
    try:
        from django.core.cache import cache
        lock_key = "worker_ready:feeds_started"

        # add() = set only if NOT exists — atomic Redis operation
        acquired = cache.add(lock_key, True, timeout=30)
        if not acquired:
            logger.info(
                "on_worker_ready: feed startup skipped — another worker already starting feeds"
            )
            return

        # Market hours ke baahir feed connect karne ki zaroorat nahi
        import datetime
        from django.utils import timezone as tz
        now_ist = tz.localtime(
            tz.now(),
            datetime.timezone(datetime.timedelta(hours=5, minutes=30))
        )
        is_weekday   = now_ist.weekday() < 5
        market_open  = now_ist.replace(hour=9,  minute=10, second=0, microsecond=0)
        market_close = now_ist.replace(hour=15, minute=35, second=0, microsecond=0)

        if not (is_weekday and market_open <= now_ist <= market_close):
            logger.info(
                "on_worker_ready: market closed (%s) — feeds NOT started on boot",
                now_ist.strftime("%a %H:%M IST"),
            )
            return

        from apps.brokers.tasks import start_all_active_feeds
        start_all_active_feeds.apply_async(
            queue="default",
            priority=5,
            countdown=3,   # 3s delay — Django ORM ready hone ke baad
        )
        logger.info("on_worker_ready: feed startup task queued")

    except Exception as exc:
        logger.error("on_worker_ready: feed startup error | %s", exc)


@worker_shutdown.connect
def on_worker_shutdown(sender=None, **kwargs):
    """Worker shutdown hook."""
    logger.info("👋 Celery worker shutting down — %s", sender)


# ────────────────────────────────────────────────────────────────────
#  Task Decorators
# ────────────────────────────────────────────────────────────────────
def idempotent_task(func):
    """
    Tasks ko idempotent banao — Redis cache se duplicate runs rokta hai.

    ✅ FIX: json.dumps(sort_keys=True) + md5 hash se deterministic key banti hai.
    Pehle dict key order non-deterministic tha — same call ke liye alag
    keys ban sakti thi aur task double-run ho sakta tha.

    Usage:
        @app.task(bind=True)
        @idempotent_task
        def my_task(self, order_id):
            ...
    """
    @wraps(func)
    def wrapper(self, *args, **kwargs):
        from django.core.cache import cache

        key_data = json.dumps(
            {"a": list(args), "k": kwargs}, sort_keys=True, default=str
        )
        key_hash = hashlib.md5(key_data.encode()).hexdigest()
        task_key = f"idempotent:{self.name}:{key_hash}"

        if cache.get(task_key):
            logger.warning("Idempotent task already running: %s", task_key)
            return None

        cache.set(task_key, True, timeout=300)

        try:
            return func(self, *args, **kwargs)
        finally:
            cache.delete(task_key)

    return wrapper


def with_timeout(seconds: int):
    """
    Task timeout decorator.

    ✅ FIX: Windows pe signal.SIGALRM nahi hota — gracefully skip karta hai
    aur Celery ke built-in soft_time_limit pe rely karta hai.

    Usage:
        @app.task
        @with_timeout(30)
        def my_task():
            ...
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            if sys.platform == "win32":
                logger.debug(
                    "with_timeout: SIGALRM not available on Windows — "
                    "skipping timeout for %s (rely on Celery soft_time_limit)",
                    getattr(func, "__name__", "unknown"),
                )
                return func(*args, **kwargs)

            import signal as _signal

            def timeout_handler(signum, frame):
                raise TimeoutError(
                    f"Task '{func.__name__}' exceeded {seconds}s timeout"
                )

            _signal.signal(_signal.SIGALRM, timeout_handler)
            _signal.alarm(seconds)

            try:
                result = func(*args, **kwargs)
                _signal.alarm(0)
                return result
            except TimeoutError:
                logger.error(
                    "with_timeout: task '%s' timed out after %ds",
                    func.__name__, seconds,
                )
                raise
            finally:
                try:
                    _signal.alarm(0)
                except Exception:
                    pass

        return wrapper
    return decorator


# ────────────────────────────────────────────────────────────────────
#  Example / utility tasks
# ────────────────────────────────────────────────────────────────────
@app.task(bind=True)
def debug_task(self):
    """Debug task — Celery setup test karne ke liye."""
    print(f"Request: {self.request!r}")
    return {
        "task_id": self.request.id,
        "hostname": self.request.hostname,
        "queue": self.request.delivery_info.get("routing_key"),
        "priority": self.request.delivery_info.get("priority"),
    }


@app.task(bind=True, base=app.Task, max_retries=0, queue="critical")
@idempotent_task
def execute_order_task(self, order_id: int, user_id: int):
    """
    Critical task: order execute karo.
    No retries, idempotent.
    """
    from apps.orders.services import execute_order

    logger.info("Executing order %s for user %s", order_id, user_id)

    try:
        return execute_order(order_id, user_id)
    except Exception as exc:
        logger.error("Order execution failed: %s", exc, exc_info=True)
        raise


@app.task(bind=True, max_retries=3, default_retry_delay=5, queue="ticks")
def process_tick_task(self, symbol: str, tick_data: dict, user_ids: list):
    """
    Tick process karo — exponential backoff ke saath retry.
    """
    from apps.strategies.services import process_tick_for_users

    try:
        process_tick_for_users(symbol, tick_data, user_ids)
    except Exception as exc:
        logger.error("Tick processing failed: %s", exc)
        countdown = 2 ** self.request.retries
        raise self.retry(exc=exc, countdown=countdown)


# ────────────────────────────────────────────────────────────────────
#  Monitoring helpers
# ────────────────────────────────────────────────────────────────────
def get_queue_stats() -> dict:
    """Saare queues ki stats lo."""
    from celery import current_app
    inspect = current_app.control.inspect()
    return {
        "active":    inspect.active(),
        "scheduled": inspect.scheduled(),
        "reserved":  inspect.reserved(),
        "stats":     inspect.stats(),
    }


def get_worker_stats() -> dict:
    """Worker stats lo."""
    from celery import current_app
    inspect = current_app.control.inspect()
    return {
        "active_queues":    inspect.active_queues(),
        "registered_tasks": inspect.registered(),
        "ping":             inspect.ping(),
    }


if __name__ == "__main__":
    app.start()