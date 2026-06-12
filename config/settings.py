# config/settings.py

import os
import sys
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ENCRYPTION_KEY = os.environ.get("ENCRYPTION_KEY")
FERNET_KEYS = [os.environ["FERNET_KEYS"]]

# ─── Paths ────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent


# ─── Security ─────────────────────────────────────────────────
SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]
DEBUG = os.getenv("DEBUG", "False") == "True"
ALLOWED_HOSTS = ['*']
CORS_ALLOW_ALL_ORIGINS = True

# Fyers Platform credentials
FYERS_APP_ID = os.getenv("FYERS_APP_ID", "")
FYERS_SECRET_KEY = os.getenv("FYERS_SECRET_KEY", "")
FYERS_REDIRECT_URI = os.getenv("FYERS_REDIRECT_URI", "")
FLUTTER_DEEP_LINK = os.getenv("FLUTTER_DEEP_LINK", "http://localhost:3000/#/fyers-callback")
FLUTTER_WEB_BASE_URL = os.getenv("FLUTTER_WEB_BASE_URL", "http://localhost:50067")
FYERS_MASTER_TOTP_SECRET = os.getenv("FYERS_MASTER_TOTP_SECRET", "")
FYERS_MASTER_REFRESH_TOKEN = os.getenv("FYERS_MASTER_REFRESH_TOKEN", "")
DELTA_BASE_URL = os.getenv("DELTA_BASE_URL", "https://api.india.delta.exchange")

# ─── Applications ─────────────────────────────────────────────
DJANGO_APPS = [
    "jazzmin",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

THIRD_PARTY_APPS = [
    "rest_framework",
    "rest_framework_simplejwt",
    "rest_framework_simplejwt.token_blacklist",
    "django_extensions",
    "corsheaders",
    "channels",
    "django_celery_beat",
    "django_celery_results",
    "drf_yasg",
]

LOCAL_APPS = [
    "apps.users",
    "apps.subscriptions",
    "apps.brokers",
    "apps.strategies",
    "apps.orders",
    "apps.backtest",
    "apps.notifications",
    "apps.websocket",
    "apps.admin_panel",
    "apps.wallet",
    "apps.market",
    "apps.options",
    "apps.ict_engine",
    "apps.paper_trading",
    "apps.predictions",
    "apps.live_trading",
    "apps.risk",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

ENABLED_BROKERS = ['fyers', 'delta']

# ─── Middleware ───────────────────────────────────────────────
MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.subscriptions.middleware.SubscriptionMiddleware",
    "config.middleware.NgrokSkipWarningMiddleware",  # ✅ ngrok browser warning bypass
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# ─── Templates ────────────────────────────────────────────────
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }
]


# ─── Database ─────────────────────────────────────────────────
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("DB_NAME", "trading_db"),
        "USER": os.getenv("DB_USER", "postgres"),
        "PASSWORD": os.getenv("DB_PASSWORD", "postgres"),
        "HOST": os.getenv("DB_HOST", "localhost"),
        "PORT": os.getenv("DB_PORT", "5432"),
        # ✅ UPDATED: Increased connection pooling for better performance
        "CONN_MAX_AGE": 60,  # 10 minutes (was 60)
        "OPTIONS": {
            "connect_timeout": 10,
            # ✅ Additional optimizations
            "options": "-c statement_timeout=30000",  # 30 second query timeout
        },
    }
}

# ─── Cache — Redis ────────────────────────────────────────────
REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")

# ✅ UPDATED: Optimized cache configuration for WebSocket performance
CACHES = {
    "default": {
        # ✅ Using django-redis for better performance and features
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": REDIS_URL,
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
            #"PARSER_CLASS": "redis.connection.HiredisParser",
            "CONNECTION_POOL_CLASS_KWARGS": {
                "max_connections": 50,
                "retry_on_timeout": True,
            },
            "SOCKET_CONNECT_TIMEOUT": 5,
            "SOCKET_TIMEOUT": 5,
            # ✅ Compression for larger cached objects
            "COMPRESSOR": "django_redis.compressors.zlib.ZlibCompressor",
        },
        "KEY_PREFIX": "profitxai",
        "TIMEOUT": 300,  # 5 minutes default
    },
    # ✅ NEW: Separate cache for candles with shorter TTL
    "candles": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": os.getenv("REDIS_URL", "redis://127.0.0.1:6379/1"),
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
            #"PARSER_CLASS": "redis.connection.HiredisParser",
            "CONNECTION_POOL_CLASS_KWARGS": {
                "max_connections": 30,
                "retry_on_timeout": True,
            },
        },
        "KEY_PREFIX": "candles",
        "TIMEOUT": 5,  # 5 seconds for real-time candle data
    },
}

# ✅ UPDATED: Use cache for sessions (faster WebSocket auth)
SESSION_ENGINE = "django.contrib.sessions.backends.cache"
SESSION_CACHE_ALIAS = "default"

# ─── Celery ───────────────────────────────────────────────────
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", REDIS_URL)
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", REDIS_URL)
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = "Asia/Kolkata"
CELERY_BEAT_SCHEDULER = "celery.beat:PersistentScheduler"
# ⚠️  NOTE: DatabaseScheduler (django_celery_beat) HATA DIYA.
# DatabaseScheduler admin panel se runtime schedule change allow karta hai,
# lekin iska side effect hai ki purani/stale DB entries worker restart pe
# sab ek saath fire ho jaati hain — isliye 18x flood ho raha tha.
# PersistentScheduler file-based hai, code se sync rehta hai, no flood.
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 300  # 5 minutes hard limit
CELERY_TASK_SOFT_TIME_LIMIT = 240  # 4 minutes soft limit
CELERY_WORKER_PREFETCH_MULTIPLIER = 1  # fair queue for long tasks
CELERY_RESULT_EXPIRES = timedelta(hours=1)
CELERY_IGNORE_RESULT = True
CELERY_WORKER_CONCURRENCY = 20   # 20 parallel workers
CELERY_WORKER_PREFETCH_MULTIPLIER = 1  # fair distribution
CELERY_TASK_ACKS_LATE = True     # reliability: task ack only after completion


if sys.platform == "win32":
    CELERY_WORKER_POOL = "solo"

from celery.schedules import crontab

CELERY_BEAT_SCHEDULE = {
    # ── Market scanner ──────────────────────────────────────
    "market-scanner": {
        "task": "apps.strategies.tasks.run_strategy_scanner",
        "schedule": 60,
        "options": {"expires": 55},
    },
    # ── REMOVED: start-active-feeds (har 5 min) ─────────────
    # ⚠️  BUG: Har 5 min pe feed restart → duplicate connections.
    # Feed lifecycle: worker_ready (ek baar) + crontab 8:45 AM (daily).
    # Yahan se NAHI chalna chahiye.

    # ── REMOVED: run-all-active-strategies (har 60s) ────────
    # ⚠️  BUG: settings.py + celery_production.py dono mein tha →
    # DatabaseScheduler dono merge karta tha → duplicate dispatches.
    # Ab sirf celery_production.py ke beat_schedule mein manage hoga.

    # ── Subscription expiry ─────────────────────────────────
    "check-subscriptions": {
        "task": "apps.subscriptions.tasks.check_expired_subscriptions",
        "schedule": timedelta(hours=24),
    },
    # ── OTP cleanup ─────────────────────────────────────────
    "clean-otps": {
        "task": "apps.users.tasks.clean_expired_otps",
        "schedule": timedelta(hours=1),
    },
    # ── Prediction EOD ──────────────────────────────────────
    "generate-eod-predictions": {
        "task": "predictions.generate_eod_predictions",
        "schedule": crontab(hour=15, minute=45),
    },
    "update-prediction-outcomes": {
        "task": "predictions.update_prediction_outcomes",
        "schedule": crontab(hour=15, minute=35),
    },
    "generate-hourly-predictions": {
        "task": "predictions.generate_hourly_predictions",
        "schedule": crontab(minute=0),
    },
    "update-hourly-outcomes": {
        "task": "predictions.update_hourly_outcomes",
        "schedule": crontab(minute=30),
    },
    "fetch-all-option-chains": {
        "task": "options.fetch_all_chains",
        "schedule": crontab(minute="*/5"),
    },
    # ── Options SL/TP check ─────────────────────────────────
    "options-sltp-check": {
        "task": "apps.options.tasks.update_spot_and_check_sltp",
        "schedule": 10,
        "options": {"expires": 9},
    },
    "ict-screener": {
        "task": "apps.strategies.tasks.run_ict_screener",
        "schedule": 900,
    },
    # ── Strategy performance snapshots ──────────────────────
    "strategy-snapshots": {
        "task": "strategies.take_performance_snapshots",
        "schedule": 3600,
        "options": {"queue": "default"},
    },
    # ── GTT exit detection → Order.realized_pnl sync ────────
    "poll-gtt-order-status": {
        "task": "brokers.poll_gtt_order_status",
        "schedule": crontab(minute="*/5"),
    },
    # ── Manual exit detection (position gone from Fyers) ────
    "sync-manual-exits": {
        "task": "brokers.sync_manual_exits",
        "schedule": crontab(minute="*/3"),
    },
}


# ─── Django Channels (WebSocket) ─────────────────────────────
# ✅ UPDATED: Optimized channel layer configuration
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            "hosts": [os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")],
            # Global capacity — total messages in Redis per channel
            "capacity": 2000,
            "expiry": 60,
            "group_expiry": 86400,  # 24 hours
            "symmetric_encryption_keys": [SECRET_KEY],
            # ✅ FIX: Per-group channel capacity
            # Default is 100 — "market" group gets flooded by high-frequency ticks
            # "8 of 11 channels over capacity in group market" — yeh warning isi se aati thi
            "channel_capacity": {
                # market group — all connected WS clients subscribe this
                # high tick rate (5 symbols × ~3 ticks/sec = 15 msg/sec × 11 clients)
                "market": 500,
                # per-symbol groups — targeted broadcasts
                "symbol_*": 200,
                # user-specific groups — signals, orders, PnL
                "user_*": 100,
            },
        },
    },
}

# ─── REST Framework ───────────────────────────────────────────
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": "30/minute",
        "user": "300/minute",      # ✅ FIX: 200 → 300 (algo trading ke liye headroom)
        'login': '10/minute',
        "capital_warning": "20/minute",  # ✅ FIX: capital-warning ka dedicated throttle
    },
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 20,
    "EXCEPTION_HANDLER": "core.exceptions.custom_exception_handler",
    "DEFAULT_SCHEMA_CLASS": "rest_framework.schemas.openapi.AutoSchema",
}


# ─── JWT ──────────────────────────────────────────────────────
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(hours=12),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=30),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
    "ALGORITHM": "HS256",
    "SIGNING_KEY": SECRET_KEY,
    "AUTH_HEADER_TYPES": ("Bearer",),
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
    "TOKEN_OBTAIN_SERIALIZER": "rest_framework_simplejwt.serializers.TokenObtainPairSerializer",
}

# ─── Auth ─────────────────────────────────────────────────────
AUTH_USER_MODEL = "users.User"

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 8},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ─── CORS ─────────────────────────────────────────────────────
# ✅ Naya (fixed)
CORS_ALLOWED_ORIGINS = os.getenv(
    "CORS_ALLOWED_ORIGINS",
    "http://localhost:3000,http://localhost:8080,http://127.0.0.1:8080,http://127.0.0.1:3000",
).split(",")

CORS_ALLOW_CREDENTIALS = True

CORS_ALLOW_ALL_ORIGINS = True

# ✅ FIX: CSRF_TRUSTED_ORIGINS — ngrok aur sab allowed origins add karo
# Bina iske Django CSRF middleware ngrok se aane wali requests block karta hai
# Fyers callback GET / pe redirect ho jaata tha — yahi root cause tha
_raw_cors = os.getenv(
    "CORS_ALLOWED_ORIGINS",
    "http://localhost:3000,http://localhost:8080",
).split(",")
CSRF_TRUSTED_ORIGINS = [o.strip() for o in _raw_cors if o.strip()] + [
    "https://*.ngrok-free.dev",
    "https://*.ngrok.io",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
]

CORS_ALLOW_HEADERS = [
    "accept",
    "accept-encoding",
    "authorization",
    "content-type",
    "dnt",
    "origin",
    "user-agent",
    "x-csrftoken",
    "x-requested-with",
    # ✅ FIXED: ngrok-skip-browser-warning header allow karo
    # Flutter web app yeh header bhejta hai — bina iske CORS preflight fail hota hai
    "ngrok-skip-browser-warning",
]

# ─── Email ────────────────────────────────────────────────────
EMAIL_BACKEND = os.getenv(
    "EMAIL_BACKEND", "django.core.mail.backends.smtp.EmailBackend"
)
EMAIL_HOST = os.getenv("EMAIL_HOST", "smtp.gmail.com")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_USE_TLS = True
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "noreply@yourapp.com")
EMAIL_TIMEOUT = 10  # SMTP connection timeout — bina is ke SMTP hang = Daphne thread stuck

# ─── Razorpay ─────────────────────────────────────────────────
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")

# ─── Subscription Plans ───────────────────────────────────────
SUBSCRIPTION_PLANS = {
    "free": {
        "name": "Free",
        "price_monthly": 0,
        "price_yearly": 0,
        "razorpay_plan_id_monthly": "",
        "razorpay_plan_id_yearly": "",
        "features": {
            "max_strategies": 1,
            "max_brokers": 1,
            "paper_trading": True,
            "live_trading": False,
            "backtesting": False,
            "websocket": False,
            "alerts": 5,
            "api_calls_per_min": 10,
        },
    },
    "basic": {
        "name": "Basic",
        "price_monthly": 499,
        "price_yearly": 4999,
        "razorpay_plan_id_monthly": os.getenv("RZP_BASIC_MONTHLY", ""),
        "razorpay_plan_id_yearly": os.getenv("RZP_BASIC_YEARLY", ""),
        "features": {
            "max_strategies": 3,
            "max_brokers": 2,
            "paper_trading": True,
            "live_trading": True,
            "backtesting": True,
            "websocket": True,
            "alerts": 20,
            "api_calls_per_min": 60,
        },
    },
    "pro": {
        "name": "Pro",
        "price_monthly": 999,
        "price_yearly": 9999,
        "razorpay_plan_id_monthly": os.getenv("RZP_PRO_MONTHLY", ""),
        "razorpay_plan_id_yearly": os.getenv("RZP_PRO_YEARLY", ""),
        "features": {
            "max_strategies": 10,
            "max_brokers": 5,
            "paper_trading": True,
            "live_trading": True,
            "backtesting": True,
            "websocket": True,
            "alerts": 100,
            "api_calls_per_min": 200,
        },
    },
    "elite": {
        "name": "Elite",
        "price_monthly": 2499,
        "price_yearly": 24999,
        "razorpay_plan_id_monthly": os.getenv("RZP_ELITE_MONTHLY", ""),
        "razorpay_plan_id_yearly": os.getenv("RZP_ELITE_YEARLY", ""),
        "features": {
            "max_strategies": -1,  # unlimited
            "max_brokers": -1,
            "paper_trading": True,
            "live_trading": True,
            "backtesting": True,
            "websocket": True,
            "alerts": -1,
            "api_calls_per_min": 500,
        },
    },
}

# ─── Internationalisation ─────────────────────────────────────
LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

# ─── Static / Media ───────────────────────────────────────────
STATIC_URL = "/static/"
STATIC_ROOT = os.path.join(BASE_DIR, "staticfiles")


MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ─── Logging ──────────────────────────────────────────────────
import sys

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {module} {message}",
            "style": "{",
        },
        "simple": {
            "format": "{levelname} {asctime} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "simple",
        },
        "file": {
            # ✅ Sirf yeh line badli
            "class": "concurrent_log_handler.ConcurrentRotatingFileHandler",
            "filename": str(BASE_DIR / "logs" / "app.log"),
            "maxBytes": 10 * 1024 * 1024,
            "backupCount": 7,
            "encoding": "utf-8",
            "formatter": "verbose",
            "delay": True,  # ✅ Yeh bhi add karo
        },
    },
    "root": {
        "handlers": ["console", "file"],
        "level": "WARNING",
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
        "celery": {
            "handlers": ["console", "file"],
            "level": "INFO",
            "propagate": False,
        },
        "apps.websocket": {
            "handlers": ["console", "file"],
            "level": "DEBUG",
            "propagate": False,
        },
        "apps.market": {
            "handlers": ["console", "file"],
            "level": "INFO",
            "propagate": False,
        },
        # ✅ NEW: Strategy execution logging
        "apps.strategies": {
            "handlers": ["console", "file"],
            "level": "INFO",
            "propagate": False,
        },
    },
}

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

(BASE_DIR / "logs").mkdir(exist_ok=True)
# ─── Security headers (production) ───────────────────────────
if not DEBUG:
    SECURE_SSL_REDIRECT = False
    SECURE_HSTS_SECONDS = 0
    SECURE_HSTS_INCLUDE_SUBDOMAINS = False
    SECURE_HSTS_PRELOAD = True
    SECURE_BROWSER_XSS_FILTER = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SESSION_COOKIE_SECURE = False
    CSRF_COOKIE_SECURE = False
    X_FRAME_OPTIONS = "DENY"

JAZZMIN_SETTINGS = {
    "site_title": "Trading Admin",
    "site_header": "Trading Control Panel",
    "site_brand": "AlgoTrader",
    "welcome_sign": "Welcome to AlgoTrader Admin",
    "copyright": "AlgoTrader © 2026",
    "show_ui_builder": True,
    # Icons for apps
    "icons": {
        "apps.market.Asset": "fas fa-coins",
        "apps.market.MarketQuote": "fas fa-chart-line",
        "apps.wallet.Wallet": "fas fa-wallet",
        "apps.orders.Order": "fas fa-shopping-cart",
        "apps.strategies.Strategy": "fas fa-brain",
        "apps.subscriptions.Subscription": "fas fa-sync-alt",
    },
    # Top menu links
    "topmenu_links": [
        {"name": "Dashboard", "url": "admin:index", "permissions": ["auth.view_user"]},
        {"app": "market"},
        {"app": "wallet"},
        {"app": "orders"},
        {"app": "strategies"},
    ],
    # Custom side menu order
    "order_with_respect_to": [
        "market",
        "wallet",
        "orders",
        "strategies",
        "subscriptions",
    ],
}

from config.settings_live_trading import (
    LIVE_TRADING_BEAT_SCHEDULE,
    LIVE_TRADING_TASK_ROUTES,   # ← rename karo
)

CELERY_BEAT_SCHEDULE = {**CELERY_BEAT_SCHEDULE, **LIVE_TRADING_BEAT_SCHEDULE}
CELERY_TASK_ROUTES = {**getattr(locals(), 'CELERY_TASK_ROUTES', {}), **LIVE_TRADING_TASK_ROUTES}