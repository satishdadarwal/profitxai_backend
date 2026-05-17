# profitxai/urls.py
# ─────────────────────────────────────────────────────────────────────────────
#  Main project URL configuration
#  Production-ready with health checks, Swagger, and all app routes
# ─────────────────────────────────────────────────────────────────────────────

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.core.cache import cache
from django.db import connection
from django.http import JsonResponse
from django.urls import include, path

from drf_yasg import openapi
from drf_yasg.views import get_schema_view
from rest_framework import permissions

# ── API version prefix ────────────────────────────────────────────────────────
API = "api/v1/"

# ── Swagger configuration ─────────────────────────────────────────────────────
schema_view = get_schema_view(
    openapi.Info(
        title="ProfitXAI API",
        default_version="v1",
        description="""
        🚀 ProfitXAI Trading Backend APIs
        
        Features:
        - 📊 Live & Paper Options Trading (NSE F&O)
        - 🤖 ICT-based Algorithmic Strategies
        - 💰 Multi-broker Integration (Fyers, Zerodha, Delta, Binance)
        - 📈 Real-time Market Data & Signals
        - 🎯 Advanced Order Management
        - 📉 Backtest Engine
        - 💳 Subscription & Wallet Management
        
        Authentication: JWT Bearer Token
        Rate Limits: 100 req/min per user
        """,
        contact=openapi.Contact(email="support@profitxai.com"),
        license=openapi.License(name="Proprietary"),
    ),
    public=True,
    permission_classes=(permissions.AllowAny,),
)


# ── Health Check View ─────────────────────────────────────────────────────────
def health_check(request):
    """
    Health check endpoint for load balancers and monitoring.

    Returns:
        200 OK: All systems operational
        503 Service Unavailable: Database or Redis down

    Checks:
        - Database connection
        - Redis cache connection
    """
    status = {
        "status": "ok",
        "version": "1.0.0",
        "db": False,
        "redis": False,
    }

    # Check database
    try:
        connection.ensure_connection()
        status["db"] = True
    except Exception as e:
        status["status"] = "degraded"
        status["db_error"] = str(e)

    # Check Redis
    try:
        cache.set("healthcheck", "1", timeout=5)
        redis_ok = cache.get("healthcheck") == "1"
        status["redis"] = redis_ok
        if not redis_ok:
            status["status"] = "degraded"
    except Exception as e:
        status["status"] = "degraded"
        status["redis_error"] = str(e)

    http_status = 200 if status["status"] == "ok" else 503
    return JsonResponse(status, status=http_status)


# ── URL Patterns ──────────────────────────────────────────────────────────────
urlpatterns = [
    # ── Django Admin ──────────────────────────────────────────────────────────
    path("admin/", admin.site.urls),

    # ── Health & Monitoring ───────────────────────────────────────────────────
    path("health/", health_check, name="health-check"),
    path("test/", lambda r: JsonResponse({"ok": True}), name="test"),

    # ── API Documentation ─────────────────────────────────────────────────────
    path("swagger/", schema_view.with_ui("swagger", cache_timeout=0), name="swagger"),
    path("redoc/", schema_view.with_ui("redoc", cache_timeout=0), name="redoc"),

    # ── Authentication & Users ────────────────────────────────────────────────
    path(f"{API}auth/", include("apps.users.urls")),
    # POST /api/v1/auth/register/
    # POST /api/v1/auth/login/
    # POST /api/v1/auth/refresh/
    # GET  /api/v1/auth/profile/

    # ── Subscriptions & Payments ──────────────────────────────────────────────
    path(f"{API}subscriptions/", include("apps.subscriptions.urls")),
    # GET  /api/v1/subscriptions/plans/
    # POST /api/v1/subscriptions/subscribe/
    # GET  /api/v1/subscriptions/current/

    # ── Broker Management ─────────────────────────────────────────────────────
    path(f"{API}brokers/", include("apps.brokers.urls")),
    # GET  /api/v1/brokers/accounts/
    # POST /api/v1/brokers/accounts/
    # POST /api/v1/brokers/accounts/<id>/connect/
    # GET  /api/v1/brokers/accounts/<id>/positions/

    # ── Trading Strategies ────────────────────────────────────────────────────
    path(f"{API}strategies/", include("apps.strategies.urls")),
    # GET  /api/v1/strategies/
    # POST /api/v1/strategies/
    # PUT  /api/v1/strategies/<id>/
    # POST /api/v1/strategies/<id>/activate/

    # ── Orders Management ─────────────────────────────────────────────────────
    path(f"{API}orders/", include("apps.orders.urls")),
    # GET  /api/v1/orders/?status=open
    # POST /api/v1/orders/
    # POST /api/v1/orders/<id>/cancel/
    # GET  /api/v1/orders/<id>/history/

    # ── Backtest Engine ───────────────────────────────────────────────────────
    path(f"{API}backtest/", include("apps.backtest.urls")),
    # POST /api/v1/backtest/run/
    # GET  /api/v1/backtest/results/<id>/

    # ── Admin Panel (Custom) ──────────────────────────────────────────────────
    path(f"{API}admin-panel/", include("apps.admin_panel.urls")),
    # GET /api/v1/admin-panel/users/
    # GET /api/v1/admin-panel/stats/

    # ── Market Data ───────────────────────────────────────────────────────────
    path(f"{API}market/", include("apps.market.urls")),
    #
    # 📊 ASSETS:
    #   GET  /api/v1/market/assets/
    #   GET  /api/v1/market/assets/<symbol>/
    #
    # 💹 QUOTES:
    #   GET  /api/v1/market/quote/<symbol>/         ← single (crypto→Delta, indian→Fyers)
    #   POST /api/v1/market/quotes/bulk/            ← watchlist bulk fetch
    #        Body: { "symbols": ["NSE:NIFTY50-INDEX", "BTC-USDT"] }
    #
    # 🔍 SEARCH:
    #   GET  /api/v1/market/search/?q=<query>
    #
    # 🕯 CANDLES:
    #   GET  /api/v1/market/candles/?symbol=NIFTY&timeframe=15&limit=200
    #   GET  /api/v1/market/candles/?symbol=BTC-USDT&timeframe=15
    #
    # 📋 SYMBOLS:
    #   GET  /api/v1/market/symbols/
    #
    # 🩺 FEED HEALTH (market app internal):
    #   GET  /api/v1/market/health/                 ← feeds + redis health (no auth)
    #   GET  /api/v1/market/feed-status/            ← detailed debug (auth required)
    #   POST /api/v1/market/restart-feeds/          ← admin only
    #   GET  /api/v1/market/broker-stats/           ← live symbol counts (no auth)

    # ── Wallet Management ─────────────────────────────────────────────────────
    path(f"{API}wallet/", include("apps.wallet.urls")),
    # GET  /api/v1/wallet/balance/
    # POST /api/v1/wallet/deposit/
    # POST /api/v1/wallet/withdraw/
    # GET  /api/v1/wallet/transactions/

    # ══════════════════════════════════════════════════════════════════════════
    # ── Options Trading (NSE F&O) - COMPLETE ENDPOINT STRUCTURE ───────────────
    # ══════════════════════════════════════════════════════════════════════════
    path(f"{API}options/", include("apps.options.urls")),
    #
    # 📊 MARKET DATA:
    #   GET  /api/v1/options/option-chain/?symbol=NIFTY&expiry=2025-05-29
    #        → Live option chain with spot, ATM, CE/PE data, OI, IV, Greeks
    #
    # 📝 PAPER TRADING (Risk-Free Practice):
    #   GET  /api/v1/options/paper/trades/?status=open|closed|all
    #   POST /api/v1/options/paper/trades/
    #   DEL  /api/v1/options/paper/trades/<uuid>/
    #   GET  /api/v1/options/paper/account/
    #   POST /api/v1/options/paper-trade/           ← legacy
    #
    # 🔴 LIVE TRADING (Real Broker Orders):
    #   GET  /api/v1/options/live/trades/?status=open|closed|all&limit=20
    #   POST /api/v1/options/live/trades/
    #        Body: {symbol, option_type, trade_type, quantity/lots,
    #               spot, entry_price, stop_loss, target_price}
    #   POST /api/v1/options/live/trades/<uuid>/close/
    #        Body: {exit_price} (optional)
    #   POST /api/v1/options/live-trade/            ← legacy
    #
    # 🎯 UNIVERSAL TRADE MANAGEMENT:
    #   GET  /api/v1/options/open-trades/?mode=live|paper|all
    #   POST /api/v1/options/close-trade/
    #        Body: {trade_id, exit_price}
    #   POST /api/v1/options/trades/<uuid>/close/
    #        Body: {exit_price} (optional)
    #
    # 📈 BACKTEST & HISTORICAL DATA:
    #   POST /api/v1/options/backtest/
    #   GET  /api/v1/options/backtest/<uuid>/
    #   GET  /api/v1/options/snapshots/?symbol=NIFTY&limit=20
    #   GET  /api/v1/options/snapshots/<uuid>/
    #
    # Total: 16 endpoints (13 new + 3 legacy)
    # ──────────────────────────────────────────────────────────────────────────

    # ── Paper Trading (Crypto/Futures) ────────────────────────────────────────
    path(f"{API}paper/", include("apps.paper_trading.urls")),
    # GET  /api/v1/paper/trades/
    # POST /api/v1/paper/trades/
    # GET  /api/v1/paper/account/

    # ── AI Predictions ────────────────────────────────────────────────────────
    path(f"{API}predictions/", include("apps.predictions.urls")),
    # GET /api/v1/predictions/signals/?symbol=NIFTY
    # POST /api/v1/predictions/analyze/

    # ── Live Trading Signals (ICT Engine) ─────────────────────────────────────
    path("api/live-trading/", include("apps.live_trading.urls")),
    # WebSocket: ws://api/live-trading/signals/
    # GET /api/live-trading/signals/recent/
]

# ── Static & Media Files (Development) ────────────────────────────────────────
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)

    # Django Debug Toolbar
    try:
        import debug_toolbar
        urlpatterns = [
            path("__debug__/", include(debug_toolbar.urls))
        ] + urlpatterns
    except ImportError:
        pass