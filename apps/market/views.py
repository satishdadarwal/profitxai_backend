# apps/market/views.py
#
# CHANGES:
#   1. CandleDataView     — symbol se auto-detect: Indian → Fyers, Crypto → Delta
#   2. LiveQuoteView      — crypto symbols → Delta ticker
#   3. BulkQuoteView      — new: watchlist ke liye multiple quotes ek call mein
#   4. health_check       — complete system health check
#   5. feed_status        — detailed feed status for debugging
#   6. restart_feeds      — manually restart feeds (admin only)
#   7. broker_stats       — real-time broker statistics

import datetime
import logging

import redis
from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views import View

from fyers_apiv3 import fyersModel
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.brokers.models import BrokerAccount
from apps.websocket.delta_feed import delta_feed_manager
from apps.websocket.fyers_feed import feed_manager

from .delta_service import (
    fetch_delta_candles,
    fetch_delta_ticker,
    fetch_delta_tickers_bulk,
    is_crypto_symbol,
)
from .models import Asset, MarketQuote
from .serializers import AssetSerializer, MarketQuoteSerializer
from .services import fetch_bulk_quotes, fetch_live_quote, search_assets

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
#  Health Check
#  GET /api/market/health/
# ─────────────────────────────────────────────────────────────────
@api_view(['GET'])
def health_check(request):
    """
    Complete system health check
    """
    # Fyers feed status
    fyers_status = feed_manager.status()

    # Delta feed status
    delta_status = delta_feed_manager.status()

    # Redis connection
    try:
        r = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
        r.ping()
        redis_ok = True
        snapshot_keys = r.hlen('tick_snapshot')
    except Exception:
        redis_ok = False
        snapshot_keys = 0

    is_healthy = (
        fyers_status.get('started', False)
        and fyers_status.get('connected', False)
        and delta_status.get('running', False)
        and redis_ok
    )

    return Response(
        {
            'status': 'healthy' if is_healthy else 'degraded',
            'timestamp': datetime.datetime.now().isoformat(),
            'feeds': {
                'fyers': {
                    'connected': fyers_status.get('connected', False),
                    'subscribed_count': fyers_status.get('sub_count', 0),
                    'market_open': fyers_status.get('market_open', False),
                    'heartbeat': fyers_status.get('heartbeat_running', False),
                },
                'delta': {
                    'running': delta_status.get('running', False),
                    'subscribed_count': delta_status.get('count', 0),
                    'loop_alive': delta_status.get('loop_alive', False),
                },
            },
            'redis': {
                'connected': redis_ok,
                'cached_symbols': snapshot_keys,
            },
        },
        status=200 if is_healthy else 503,
    )


# ─────────────────────────────────────────────────────────────────
#  Feed Status
#  GET /api/market/feed-status/
# ─────────────────────────────────────────────────────────────────
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def feed_status(request):
    """
    Detailed feed status for debugging
    """
    fyers_status = feed_manager.status()
    delta_status = delta_feed_manager.status()

    return Response({
        'fyers': fyers_status,
        'delta': delta_status,
        'user_id': request.user.id,
        'timestamp': datetime.datetime.now().isoformat(),
    })


# ─────────────────────────────────────────────────────────────────
#  Restart Feeds
#  POST /api/market/restart-feeds/
# ─────────────────────────────────────────────────────────────────
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def restart_feeds(request):
    """
    Manually restart feeds (admin only)
    """
    if not request.user.is_staff:
        return Response({'error': 'Admin access required'}, status=403)

    try:
        feed_manager.stop()
        feed_manager.start()

        delta_feed_manager.stop()
        delta_feed_manager.start()

        return Response({
            'status': 'success',
            'message': 'Both feeds restarted',
            'timestamp': datetime.datetime.now().isoformat(),
        })
    except Exception as e:
        return Response({'status': 'error', 'message': str(e)}, status=500)


# ─────────────────────────────────────────────────────────────────
#  Broker Stats
#  GET /api/market/broker-stats/
# ─────────────────────────────────────────────────────────────────
@api_view(['GET'])
def broker_stats(request):
    """
    Real-time broker statistics
    """
    try:
        r = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)

        all_symbols = r.hkeys('tick_snapshot')

        fyers_symbols = [s for s in all_symbols if s.startswith('NSE:') or s.startswith('BSE:')]
        delta_symbols = [s for s in all_symbols if '-USDT' in s or '-BUSD' in s]

        return Response({
            'total_symbols': len(all_symbols),
            'brokers': {
                'fyers': {
                    'count': len(fyers_symbols),
                    'symbols': fyers_symbols[:10],
                },
                'delta': {
                    'count': len(delta_symbols),
                    'symbols': delta_symbols[:10],
                },
            },
            'timestamp': datetime.datetime.now().isoformat(),
        })
    except Exception as e:
        return Response({'error': str(e)}, status=500)


# ─────────────────────────────────────────────────────────────────
#  Asset List
#  GET /api/market/assets/
# ─────────────────────────────────────────────────────────────────
class AssetListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = Asset.objects.filter(is_active=True)

        if q := request.query_params.get("q"):
            qs = qs.filter(symbol__icontains=q) | qs.filter(name__icontains=q)
        if asset_type := request.query_params.get("type"):
            qs = qs.filter(asset_type=asset_type)
        if exchange := request.query_params.get("exchange"):
            qs = qs.filter(exchange__iexact=exchange)

        return Response(AssetSerializer(qs[:50], many=True).data)


# ─────────────────────────────────────────────────────────────────
#  Asset Detail
#  GET /api/market/assets/<symbol>/
# ─────────────────────────────────────────────────────────────────
class AssetDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, symbol):
        asset = get_object_or_404(Asset, symbol__iexact=symbol, is_active=True)
        return Response(AssetSerializer(asset).data)


# ─────────────────────────────────────────────────────────────────
#  Live Quote — single symbol
#  GET /api/market/quote/<symbol>/
#  Auto-routes: crypto → Delta, indian → Fyers
# ─────────────────────────────────────────────────────────────────
class LiveQuoteView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, symbol):
        cache_key = f"quote_{symbol}"
        cached = cache.get(cache_key)
        if cached:
            return Response(cached)

        if is_crypto_symbol(symbol):
            data = fetch_delta_ticker(symbol)
        else:
            data = fetch_live_quote(
                symbol=symbol,
                user=request.user,
                broker_slug=request.query_params.get("broker", ""),
            )

        if data.get("error"):
            return Response({"error": data["error"]}, status=502)

        cache.set(cache_key, data, 30)
        return Response(data)


# ─────────────────────────────────────────────────────────────────
#  Bulk Quotes — watchlist ke liye
#  POST /api/market/quotes/bulk/
#  Body: { "symbols": ["NSE:NIFTY50-INDEX", "BTC-USDT", "ETH-USDT"] }
# ─────────────────────────────────────────────────────────────────
class BulkQuoteView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        symbols = request.data.get("symbols", [])
        if not symbols:
            return Response({"quotes": {}})

        quotes = {}
        missing = []

        for sym in symbols:
            cached = cache.get(f"quote_{sym}")
            if cached:
                quotes[sym] = cached
            else:
                missing.append(sym)

        if missing:
            fresh = fetch_bulk_quotes(
                symbols=missing,
                user=request.user,
                broker_slug="fyers",
            )
            for sym, data in fresh.items():
                cache.set(f"quote_{sym}", data, 30)
                quotes[sym] = data

        return Response({"quotes": quotes, "count": len(quotes)})


# ─────────────────────────────────────────────────────────────────
#  Search
#  GET /api/market/search/?q=<query>
# ─────────────────────────────────────────────────────────────────
class QuoteSearchView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        q = request.query_params.get("q", "").strip()
        if len(q) < 2:
            return Response({"results": []})

        results = search_assets(q=q, broker_slug="fyers", user=request.user)
        return Response({"results": results})


# ─────────────────────────────────────────────────────────────────
#  Candle Data — Indian + Crypto dono support
#  GET /api/market/candles/?symbol=NIFTY&timeframe=15
#  GET /api/market/candles/?symbol=BTC-USDT&timeframe=15
# ─────────────────────────────────────────────────────────────────
class CandleDataView(APIView):
    permission_classes = [IsAuthenticated]

    FYERS_SYMBOL_MAP = {
        "NIFTY": "NSE:NIFTY50-INDEX",
        "BANKNIFTY": "NSE:NIFTYBANK-INDEX",
        "FINNIFTY": "NSE:FINNIFTY-INDEX",
        "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX",
        "SENSEX": "BSE:SENSEX-INDEX",
    }

    FYERS_TF_MAP = {
        "1": "1",
        "3": "3",
        "5": "5",
        "15": "15",
        "30": "30",
        "60": "60",
        "D": "D",
        "1440": "D",  # ✅ FIXED: fallback if minutes value sent
    }

    def get(self, request):
        symbol = request.query_params.get("symbol", "NIFTY").upper()
        timeframe = request.query_params.get("timeframe", "15")
        limit = min(int(request.query_params.get("limit", 200)), 500)

        # Crypto → Delta
        if is_crypto_symbol(symbol):
            result = fetch_delta_candles(symbol=symbol, timeframe=timeframe, limit=limit)
            if result.get("error"):
                return Response({"error": result["error"]}, status=502)
            return Response(result)

        # Indian → Fyers
        return self._fyers_candles(request, symbol, timeframe, limit)

    def _fyers_candles(self, request, symbol: str, timeframe: str, limit: int):
        fyers_symbol = self.FYERS_SYMBOL_MAP.get(symbol, f"NSE:{symbol}-EQ")
        resolution = self.FYERS_TF_MAP.get(timeframe, "15")

        if timeframe in ["1", "3"]:
            days_back = 3        # intraday short TF — 3 days enough
        elif timeframe == "5":
            days_back = 5
        elif timeframe in ["15", "30"]:
            days_back = 15
        elif timeframe == "60":
            days_back = 60
        elif timeframe in ["D", "1440"]:
            days_back = 365      # ✅ FIXED: Daily needs 1 year
        else:
            days_back = 10

        try:
            account = BrokerAccount.objects.get(
                user=request.user,
                broker="fyers",
                is_active=True,
                is_verified=True,
            )
        except BrokerAccount.DoesNotExist:
            return Response({"error": "Fyers broker connect nahi hai"}, status=400)

        if not account.access_token:
            return Response({"error": "Fyers token missing"}, status=400)

        today = datetime.date.today()
        from_date = (today - datetime.timedelta(days=days_back)).strftime("%Y-%m-%d")
        to_date = today.strftime("%Y-%m-%d")

        try:
            fyers = fyersModel.FyersModel(
                client_id=account.app_id,
                token=account.access_token,
                log_path="",
                is_async=False,
            )

            data = fyers.history(
                data={
                    "symbol": fyers_symbol,
                    "resolution": resolution,
                    "date_format": "1",
                    "range_from": from_date,
                    "range_to": to_date,
                    "cont_flag": "1",
                }
            )

            if data.get("s") != "ok":
                return Response({"error": data}, status=502)

            def is_market_time(ts):
                dt = datetime.datetime.fromtimestamp(ts)
                t = dt.time()
                return datetime.time(9, 15) <= t <= datetime.time(15, 30)

            candles = []
            for c in data.get("candles", []):
                # ✅ FIXED: Daily candles ka timestamp midnight hota hai —
                # is_market_time filter skip karo, warna saari daily candles
                # reject ho jaati hain
                if timeframe not in ("D", "1440") and not is_market_time(c[0]):
                    continue
                candles.append({
                    "ts": c[0],
                    "open": c[1],
                    "high": c[2],
                    "low": c[3],
                    "close": c[4],
                    "volume": c[5],
                })

            return Response({"candles": candles[-limit:], "source": "fyers"})

        except Exception as exc:
            logger.exception("Fyers candle error: %s", exc)
            return Response({"error": str(exc)}, status=500)


# ─────────────────────────────────────────────────────────────────
#  Symbol List
#  GET /api/market/symbols/
# ─────────────────────────────────────────────────────────────────
class SymbolListView(View):
    def get(self, request):
        indian = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"]
        crypto = ["BTC-USDT", "ETH-USDT", "SOL-USDT", "BNB-USDT", "XRP-USDT"]
        return JsonResponse({"indian": indian, "crypto": crypto})