# apps/market/urls.py
#
# All market endpoints — assets, quotes, candles, search, health/status

from django.urls import path

from .views import (
    # Class-based views
    AssetDetailView,
    AssetListView,
    BulkQuoteView,
    CandleDataView,
    ChartPairView,
    LiveQuoteView,
    QuoteSearchView,
    SymbolListView,
    # Function-based views
    broker_stats,
    feed_status,
    health_check,
    restart_feeds,
    public_ticker,
)

urlpatterns = [
    # ── Health & Status ───────────────────────────────────────
    path("health/", health_check, name="health_check"),
    path("feed-status/", feed_status, name="feed_status"),
    path("restart-feeds/", restart_feeds, name="restart_feeds"),
    path("broker-stats/", broker_stats, name="broker_stats"),

    # ── Market Assets ─────────────────────────────────────────
    path("assets/", AssetListView.as_view(), name="asset_list"),
    path("assets/<str:symbol>/", AssetDetailView.as_view(), name="asset_detail"),

    # ── Quotes ────────────────────────────────────────────────
    path("quote/<str:symbol>/", LiveQuoteView.as_view(), name="live_quote"),
    path("quotes/bulk/", BulkQuoteView.as_view(), name="bulk_quote"),

    # ── Search ────────────────────────────────────────────────
    path("search/", QuoteSearchView.as_view(), name="quote_search"),

    # ── Candles ───────────────────────────────────────────────
    path("candles/", CandleDataView.as_view(), name="candle_data"),

    # ── Symbols (short names list) ────────────────────────────
    path("symbols/", SymbolListView.as_view(), name="symbol_list"),

    path("ticker/", public_ticker, name="public_ticker"),
    path("chart-pair/", ChartPairView.as_view(), name="chart_pair"),
]
