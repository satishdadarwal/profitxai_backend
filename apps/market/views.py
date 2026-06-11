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
#
# FIXES (2026-05-30):
#   ✅ FIX 1 — CandleDataView._fyers_candles(): Fyers SDK hataya, FyersAdapter use kiya
#              → 401 loop fix: har request pe fresh token DB se, SDK ki tarah stale nahi
#              → Master account pattern: FYERS_APP_ID → label="Master Account" → any active
#   ✅ FIX 2 — health_check(): Dhan feed status add kiya
#   ✅ FIX 3 — feed_status(): Dhan feed status add kiya
#   ✅ FIX 4 — restart_feeds(): Dhan feed restart add kiya
#   ✅ FIX 5 — broker_stats(): Dhan symbols tracking add kiya

import datetime
import logging
import time

import redis
from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views import View

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.brokers.models import BrokerAccount
from apps.websocket.delta_feed import delta_feed_manager
from apps.websocket.fyers_feed import feed_manager
from apps.websocket.dhan_feed import dhan_feed_manager

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
    Complete system health check — Fyers + Dhan + Delta + Redis
    """
    fyers_status = feed_manager.status()
    delta_status = delta_feed_manager.status()
    # ✅ FIX 2: Dhan feed status add kiya
    dhan_status  = dhan_feed_manager.status()

    try:
        r = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
        r.ping()
        redis_ok = True
        snapshot_keys = r.hlen('tick_snapshot')
    except Exception:
        redis_ok = False
        snapshot_keys = 0

    # Fyers connected OR Dhan running — at least ek Indian feed hona chahiye
    indian_feed_ok = (
        (fyers_status.get('started', False) and fyers_status.get('connected', False))
        or dhan_status.get('running', False)
    )

    is_healthy = (
        indian_feed_ok
        and delta_status.get('running', False)
        and redis_ok
    )

    return Response(
        {
            'status': 'healthy' if is_healthy else 'degraded',
            'timestamp': datetime.datetime.now().isoformat(),
            'feeds': {
                'fyers': {
                    'connected':        fyers_status.get('connected', False),
                    'subscribed_count': fyers_status.get('sub_count', 0),
                    'market_open':      fyers_status.get('market_open', False),
                    'heartbeat':        fyers_status.get('heartbeat_running', False),
                },
                # ✅ FIX 2: Dhan feed block added
                'dhan': {
                    'running':          dhan_status.get('running', False),
                    'subscribed_count': dhan_status.get('count', 0),
                    'client_id':        dhan_status.get('client_id', ''),
                    'loop_alive':       dhan_status.get('loop_alive', False),
                },
                'delta': {
                    'running':          delta_status.get('running', False),
                    'subscribed_count': delta_status.get('count', 0),
                    'loop_alive':       delta_status.get('loop_alive', False),
                },
            },
            'redis': {
                'connected':      redis_ok,
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
    Detailed feed status for debugging — Fyers + Dhan + Delta
    """
    # ✅ FIX 3: Dhan feed status add kiya
    return Response({
        'fyers':     feed_manager.status(),
        'dhan':      dhan_feed_manager.status(),
        'delta':     delta_feed_manager.status(),
        'user_id':   request.user.id,
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
    Manually restart all feeds (admin only)
    """
    if not request.user.is_staff:
        return Response({'error': 'Admin access required'}, status=403)

    try:
        feed_manager.stop()
        feed_manager.start()

        # ✅ FIX 4: Dhan feed restart add kiya
        dhan_feed_manager.stop()
        dhan_feed_manager.start()

        delta_feed_manager.stop()
        delta_feed_manager.start()

        return Response({
            'status':    'success',
            'message':   'All feeds restarted (Fyers + Dhan + Delta)',
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
    Real-time broker statistics from Redis tick_snapshot
    """
    try:
        r = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
        all_symbols = r.hkeys('tick_snapshot')

        fyers_symbols = [s for s in all_symbols if s.startswith('NSE:') or s.startswith('BSE:')]
        delta_symbols = [s for s in all_symbols if '-USDT' in s or '-BUSD' in s]
        # ✅ FIX 5: Dhan symbols — NSE/BSE wale jo Fyers mein nahi hain
        dhan_status   = dhan_feed_manager.status()
        dhan_symbols  = list(dhan_status.get('subscribed', []))

        return Response({
            'total_symbols': len(all_symbols),
            'brokers': {
                'fyers': {
                    'count':   len(fyers_symbols),
                    'symbols': fyers_symbols[:10],
                },
                'dhan': {
                    'count':   len(dhan_symbols),
                    'symbols': dhan_symbols[:10],
                    'running': dhan_status.get('running', False),
                },
                'delta': {
                    'count':   len(delta_symbols),
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
#  Auto-routes: crypto → Delta, indian → Fyers/Dhan
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
        "NIFTY":      "NSE:NIFTY50-INDEX",
        "BANKNIFTY":  "NSE:NIFTYBANK-INDEX",
        "FINNIFTY":   "NSE:FINNIFTY-INDEX",
        "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX",
        "SENSEX":     "BSE:SENSEX-INDEX",
    }

    FYERS_TF_MAP = {
        "1": "1", "3": "3", "5": "5",
        "15": "15", "30": "30", "60": "60",
        "D": "D",
        "1440": "D",
    }

    # timeframe → kitne din peeche jaana hai
    _DAYS_BACK = {
        "1": 3, "3": 3, "5": 5,
        "15": 15, "30": 15,
        "60": 60,
        "D": 365, "1440": 365,
    }

    # ── INDEX symbols — Fyers required (Dhan IDX_I segment has no candle API)
    # ── FnO/Equity symbols — Dhan first, Fyers fallback
    _INDEX_SYMBOLS = {"NIFTY", "BANKNIFTY", "NIFTYBANK", "FINNIFTY",
                      "MIDCPNIFTY", "SENSEX", "BANKEX", "NIFTYNXT50"}

    def get(self, request):
        symbol    = request.query_params.get("symbol", "NIFTY").upper()
        timeframe = request.query_params.get("timeframe", "15")
        limit     = min(int(request.query_params.get("limit", 200)), 500)

        # Crypto → Delta
        if is_crypto_symbol(symbol):
            result = fetch_delta_candles(symbol=symbol, timeframe=timeframe, limit=limit)
            if result.get("error"):
                return Response({"error": result["error"]}, status=502)
            return Response(result)

        # ── Indian candle routing ─────────────────────────────────
        # INDEX (NIFTY, BANKNIFTY, SENSEX etc):
        #   → Always Fyers — Dhan IDX_I segment ka /charts/intraday API nahi hai
        # FnO/Equity (options symbols, NSE stocks):
        #   → Dhan first (fresh token), Fyers fallback
        clean_sym  = symbol.replace("NSE:", "").replace("BSE:", "").replace("-INDEX", "")
        is_index   = clean_sym in self._INDEX_SYMBOLS
        is_options = clean_sym.endswith("CE") or clean_sym.endswith("PE")

        if is_index or is_options:
            return self._fyers_candles(request, symbol, timeframe, limit)
        else:
            return self._indian_candles(request, symbol, timeframe, limit)

    def _indian_candles(self, request, symbol: str, timeframe: str, limit: int):
        """
        FnO/Equity candles: Dhan first, Fyers fallback.
        INDEX symbols yahan nahi aate — get() mein hi Fyers ko bheja jaata hai.
        """
        dhan_result = self._dhan_candles(request, symbol, timeframe, limit)
        if dhan_result is not None:
            return dhan_result
        return self._fyers_candles(request, symbol, timeframe, limit)

    def _dhan_candles(self, request, symbol: str, timeframe: str, limit: int):
        """
        Dhan se historical candles.
        Returns Response on success, None if no dhan account.
        """
        from apps.brokers.models import BrokerAccount as _BA
        from django.conf import settings as _settings

        master_client_id = getattr(_settings, "DHAN_MASTER_CLIENT_ID", "").strip()

        account = None
        # Step 1: master account
        if master_client_id:
            account = (
                _BA.objects.filter(
                    broker="dhan", is_active=True,
                    dhan_client_id=master_client_id,
                )
                .exclude(dhan_access_token="")
                .exclude(dhan_access_token__isnull=True)
                .first()
            )
        # Step 2: any active dhan account
        if account is None:
            account = (
                _BA.objects.filter(broker="dhan", is_active=True, is_verified=True)
                .exclude(dhan_access_token="")
                .exclude(dhan_access_token__isnull=True)
                .order_by("-updated_at")
                .first()
            )

        if account is None:
            return None  # No dhan account — caller will try Fyers

        dhan_symbol = self.FYERS_SYMBOL_MAP.get(symbol, f"NSE:{symbol}-EQ")
        resolution  = self.FYERS_TF_MAP.get(timeframe, "15")
        days_back   = self._DAYS_BACK.get(timeframe, 10)

        try:
            from broker_adapters.dhan.adapter import DhanAdapter
            adapter = DhanAdapter({
                "dhan_client_id":    account.dhan_client_id,
                "dhan_access_token": account.dhan_access_token,
            })

            today   = datetime.date.today()
            extra   = 3 if today.weekday() >= 5 else 0
            from_ts = int(time.mktime(
                (today - datetime.timedelta(days=days_back + extra)).timetuple()
            ))
            to_ts   = int(time.mktime(
                (today + datetime.timedelta(days=1)).timetuple()
            ))

            candle_bars = adapter.get_candles(
                symbol     = dhan_symbol,
                resolution = resolution,
                from_ts    = from_ts,
                to_ts      = to_ts,
            )

            if not candle_bars:
                logger.warning(
                    "CandleDataView: Dhan returned 0 bars | symbol=%s tf=%s — trying Fyers",
                    symbol, timeframe,
                )
                return None

            candles = [
                {
                    "ts":     c.timestamp,
                    "open":   c.open,
                    "high":   c.high,
                    "low":    c.low,
                    "close":  c.close,
                    "volume": c.volume,
                }
                for c in candle_bars
            ]

            logger.info(
                "CandleDataView: Dhan ✅ | symbol=%s | tf=%s | bars=%d | account=%s",
                dhan_symbol, resolution, len(candles), account.id,
            )
            return Response({"candles": candles[-limit:], "source": "dhan"})

        except Exception as exc:
            logger.warning(
                "CandleDataView: Dhan failed | symbol=%s | %s — falling back to Fyers",
                symbol, exc,
            )
            return None  # Silently fall through to Fyers

    # ── ✅ FIX 1: Fyers SDK hatao, FyersAdapter use karo ────────
    def _fyers_candles(self, request, symbol: str, timeframe: str, limit: int):
        """
        Fyers historical candles — FyersAdapter.get_candles() use karta hai.

        FIX: Pehle fyersModel.FyersModel() SDK use hota tha jisko
        token refresh ke baad bhi naya token nahi milta tha → 401 loop.

        Ab FyersAdapter.get_candles() use hota hai jo:
          1. Adapter instantiate hote waqt DB se fresh token leta hai
          2. Master account priority maintain karta hai (FYERS_APP_ID)
          3. Broker-agnostic pattern — same code Dhan ke liye bhi kaam karta hai
        """
        from broker_adapters.fyers.adapter import FyersAdapter

        if symbol.endswith(("CE", "PE")):
            fyers_symbol = symbol if ":" in symbol else f"NSE:{symbol}"
        elif symbol.endswith("FUT"):
            fyers_symbol = symbol if ":" in symbol else f"NSE:{symbol}"
        else:
            fyers_symbol = self.FYERS_SYMBOL_MAP.get(symbol, f"NSE:{symbol}-EQ")
        resolution   = self.FYERS_TF_MAP.get(timeframe, "15")
        days_back    = self._DAYS_BACK.get(timeframe, 10)

        # ── Master account priority (Fyers ki tarah fyers_feed.py mein) ──
        # Step 1: FYERS_APP_ID se master account dhundo
        account = None
        master_app_id = getattr(settings, "FYERS_APP_ID", "").strip()
        if master_app_id:
            account = (
                BrokerAccount.objects
                .filter(
                    broker="fyers",
                    is_active=True,
                    app_id=master_app_id,
                )
                .exclude(access_token__isnull=True)
                .exclude(access_token="")
                .order_by("-updated_at")
                .first()
            )

        # Step 2: label="Master Account" fallback
        if account is None:
            account = (
                BrokerAccount.objects
                .filter(broker="fyers", is_active=True, is_verified=True,
                        label="Master Account")
                .exclude(access_token__isnull=True)
                .exclude(access_token="")
                .first()
            )

        # Step 3: User ka apna account
        if account is None:
            account = (
                BrokerAccount.objects
                .filter(user=request.user, broker="fyers",
                        is_active=True, is_verified=True)
                .exclude(access_token__isnull=True)
                .exclude(access_token="")
                .order_by("-updated_at")
                .first()
            )

        # Step 4: Koi bhi active account
        if account is None:
            account = (
                BrokerAccount.objects
                .filter(broker="fyers", is_active=True, is_verified=True)
                .exclude(access_token__isnull=True)
                .exclude(access_token="")
                .order_by("-updated_at")
                .first()
            )

        if account is None:
            return Response(
                {"error": "Fyers broker connect nahi hai. Admin se contact karo."},
                status=400,
            )

        # ── FyersAdapter se candles fetch karo ───────────────────
        try:
            adapter = FyersAdapter({
                "app_id":       account.app_id or master_app_id,
                "access_token": account.access_token,
            })

            today    = datetime.date.today()
            # ✅ FIX: Weekend pe extra days lo taaki Friday ka data aaye
            extra    = 3 if today.weekday() >= 5 else 0
            from_ts  = int(time.mktime(
                (today - datetime.timedelta(days=days_back + extra)).timetuple()
            ))
            to_ts    = int(time.mktime(
                (today + datetime.timedelta(days=1)).timetuple()
            ))

            candle_bars = adapter.get_candles(
                symbol     = fyers_symbol,
                resolution = resolution,
                from_ts    = from_ts,
                to_ts      = to_ts,
            )

            def _is_market_time(ts: int) -> bool:
                dt = datetime.datetime.fromtimestamp(ts)
                t  = dt.time()
                return datetime.time(9, 15) <= t <= datetime.time(15, 30)

            candles = [
                {
                    "ts":     c.timestamp,
                    "open":   c.open,
                    "high":   c.high,
                    "low":    c.low,
                    "close":  c.close,
                    "volume": c.volume,
                }
                for c in candle_bars
                # ✅ FIX: Market time filter completely hata diya.
                # Fyers API khud sirf valid session bars return karta hai.
                # Pehle ka filter weekend/holiday pe chart blank karta tha.
                # Daily bars ke liye bhi no filter needed.
            ]

            logger.info(
                "CandleDataView: Fyers adapter | symbol=%s | tf=%s | "
                "bars=%d | account=%s",
                fyers_symbol, resolution, len(candles), account.id,
            )
            return Response({"candles": candles[-limit:], "source": "fyers"})

        except Exception as exc:
            # Token expired check — FyersAdapter ke andar requests.HTTPError
            err_str = str(exc).lower()
            if any(k in err_str for k in ("401", "unauthorized", "token", "invalid")):
                logger.error(
                    "CandleDataView: Fyers token expired | account=%s | error=%s",
                    account.id, exc,
                )
                return Response(
                    {"error": "Fyers token expired ya invalid. App mein dobara login karo."},
                    status=401,
                )
            logger.exception("CandleDataView: Fyers candle error | %s", exc)
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


# ─────────────────────────────────────────────────────────────────
#  Public Ticker
#  GET /api/v1/market/ticker/
#
#  ✅ AllowAny — koi bhi user, koi bhi broker, ya koi broker nahi
#  ✅ Redis tick_snapshot se seedha read — Fyers + Dhan + Delta sab cover
#  ✅ App open hote hi NIFTY, BTC etc. turant dikhenge
#  ✅ WS connect hone ke baad WS data override kar deta hai
# ─────────────────────────────────────────────────────────────────
import json as _json

_TICKER_SYMBOLS = [
    "NSE:NIFTY50-INDEX",
    "NSE:NIFTYBANK-INDEX",
    "NSE:FINNIFTY-INDEX",
    "NSE:MIDCPNIFTY-INDEX",
    "BSE:SENSEX-INDEX",
    "BTC-USDT",
    "ETH-USDT",
    "SOL-USDT",
    "BNB-USDT",
    "XRP-USDT",
    "ADA-USDT",
    "DOGE-USDT",
]


def _ticker_empty(symbol):
    return {
        "symbol":     symbol,
        "ltp":        0.0,
        "change":     0.0,
        "change_pct": 0.0,
        "open":       0.0,
        "high":       0.0,
        "low":        0.0,
        "prev_close": 0.0,
        "volume":     0,
        "bid":        0.0,
        "ask":        0.0,
        "broker":     "unknown",
        "stale":      True,
    }


def _ticker_parse(raw):
    try:
        d = _json.loads(raw)
        return {
            "symbol":     d.get("symbol", ""),
            "ltp":        float(d.get("ltp") or 0),
            "change":     float(d.get("change") or 0),
            "change_pct": float(d.get("change_pct") or d.get("chp") or 0),
            "open":       float(d.get("open") or d.get("open_price") or 0),
            "high":       float(d.get("high") or d.get("high_price") or 0),
            "low":        float(d.get("low") or d.get("low_price") or 0),
            "prev_close": float(
                d.get("prev_close") or d.get("previous_close") or
                d.get("prevClose") or 0
            ),
            "volume":     int(d.get("volume") or 0),
            "bid":        float(d.get("bid") or 0),
            "ask":        float(d.get("ask") or 0),
            "broker":     d.get("broker", "unknown"),
            "stale":      False,
        }
    except Exception:
        return None


@api_view(["GET"])
@permission_classes([AllowAny])
def public_ticker(request):
    """
    Broker-independent snapshot of all default symbols.
    Source: Redis tick_snapshot (written by Fyers + Dhan + Delta feeds).
    No auth needed — works for any user regardless of broker.
    """
    result = []
    try:
        r = redis.Redis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        raw_values = r.hmget("tick_snapshot", _TICKER_SYMBOLS)
        for symbol, raw in zip(_TICKER_SYMBOLS, raw_values):
            if raw:
                entry = _ticker_parse(raw)
                result.append(entry if entry else _ticker_empty(symbol))
            else:
                result.append(_ticker_empty(symbol))
    except Exception as e:
        logger.error("public_ticker Redis error: %s", e)
        result = [_ticker_empty(s) for s in _TICKER_SYMBOLS]

    live_count = sum(1 for r in result if not r.get("stale"))
    return Response({
        "ticker":     result,
        "count":      len(result),
        "live_count": live_count,
        "source":     "redis_snapshot",
    })

# ─────────────────────────────────────────────────────────────────
#  Chart Pair API
#  GET /api/v1/market/chart-pair/
#
#  Teeno trade sources support karta hai:
#    PaperTrade  → apps/paper_trading/models.py
#    OptionTrade → apps/options/models.py
#    Position    → apps/orders/models.py
#
#  Query params:
#    ?mode=auto     (default) active trade dhundo — paper → options → live
#    ?mode=paper    sirf paper trades
#    ?mode=options  sirf option trades
#    ?mode=live     sirf live positions
#    ?symbol=BANKNIFTY  specific symbol ke liye override
#
#  Response hamesha same format — Flutter ek hi parser use kare.
# ─────────────────────────────────────────────────────────────────

# Underlying index map — options symbol se spot symbol nikalna
_UNDERLYING_MAP = {
    "NIFTY":      "NSE:NIFTY50-INDEX",
    "BANKNIFTY":  "NSE:NIFTYBANK-INDEX",
    "FINNIFTY":   "NSE:FINNIFTY-INDEX",
    "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX",
    "SENSEX":     "BSE:SENSEX-INDEX",
    "BANKEX":     "BSE:BANKEX-INDEX",
}


def _extract_underlying(fyers_symbol: str) -> str | None:
    """
    Options/Futures symbol se underlying index nikalo.
    NSE:BANKNIFTY26JUN57000CE → NSE:NIFTYBANK-INDEX
    NSE:NIFTY26JUN24500PE     → NSE:NIFTY50-INDEX
    NSE:NIFTY26MAYFUT         → NSE:NIFTY50-INDEX
    """
    clean = fyers_symbol.upper().replace("NSE:", "").replace("BSE:", "")
    for base, index_sym in _UNDERLYING_MAP.items():
        if clean.startswith(base):
            return index_sym
    return None


def _build_chart_pair(trade_symbol: str, trade_info: dict) -> dict:
    """
    Trade symbol se 2-chart pair build karo.

    Options/Futures:
      slot 1 → underlying index (spot context)
      slot 2 → trade symbol (actual position)

    Crypto:
      slot 1 → trade symbol (single chart — no underlying)

    Index/Equity:
      slot 1 → trade symbol
      slot 2 → None (single chart)
    """
    from apps.brokers.symbol_mapper import normalize_for_fyers
    from apps.market.delta_service import is_crypto_symbol

    # Crypto → single chart, type=crypto, no underlying
    if is_crypto_symbol(trade_symbol):
        clean = trade_symbol.upper().replace("DELTA:", "")
        return {
            "charts": [
                {
                    "slot":         1,
                    "symbol":       clean,
                    "display_name": clean,
                    "type":         "crypto",
                    "role":         "trade",
                }
            ],
            "active_trade": trade_info,
            "linked":       False,
        }

    fyers_symbol = normalize_for_fyers(trade_symbol)
    upper = fyers_symbol.upper()

    is_options = upper.endswith("CE") or upper.endswith("PE")
    is_futures = "FUT" in upper
    underlying = _extract_underlying(fyers_symbol)

    if (is_options or is_futures) and underlying:
        charts = [
            {
                "slot":         1,
                "symbol":       _to_candle_symbol(underlying),
                "display_name": underlying.split(":")[1].replace("-INDEX", ""),
                "type":         "index",
                "role":         "context",
            },
            {
                "slot":         2,
                "symbol":       fyers_symbol,
                "display_name": fyers_symbol.split(":")[1] if ":" in fyers_symbol else fyers_symbol,
                "type":         "options" if is_options else "futures",
                "role":         "trade",
            },
        ]
    else:
        charts = [
            {
                "slot":         1,
                "symbol":       fyers_symbol,
                "display_name": fyers_symbol.split(":")[1].replace("-INDEX", "").replace("-EQ", "")
                                if ":" in fyers_symbol else fyers_symbol,
                "type":         "index" if "-INDEX" in fyers_symbol else "equity",
                "role":         "trade",
            },
        ]

    return {
        "charts":       charts,
        "active_trade": trade_info,
        "linked":       len(charts) == 2,
    }


class ChartPairView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        mode   = request.query_params.get("mode", "auto").lower()
        symbol = request.query_params.get("symbol", "").upper().strip()

        # ── Symbol override — user ne specific symbol diya ──
        if symbol:
            return Response(self._from_symbol(symbol))

        # ── Auto / mode-based trade lookup ──────────────────
        result = None

        if mode in ("auto", "paper"):
            result = self._from_paper_trade(request.user)

        if result is None and mode in ("auto", "options"):
            result = self._from_option_trade(request.user)

        if result is None and mode in ("auto", "live"):
            result = self._from_live_position(request.user)

        # ── No active trade — default NIFTY spot ────────────
        if result is None:
            result = {
                "mode":   "default",
                "charts": [
                    {
                        "slot":         1,
                        "symbol":       "NSE:NIFTY50-INDEX",
                        "display_name": "NIFTY",
                        "type":         "index",
                        "role":         "context",
                    }
                ],
                "active_trade": None,
                "linked":       False,
            }

        return Response(result)

    # ── Paper trade ──────────────────────────────────────────
    def _from_paper_trade(self, user) -> dict | None:
        try:
            from apps.orders.models import Order
            trade = (
                Order.objects
                .filter(user=user, mode="paper", status=Order.Status.OPEN)
                .order_by("-created_at")
                .first()
            )
            if not trade:
                return None

            trade_info = {
                "source":      "paper",
                "id":          str(trade.id),
                "symbol":      trade.symbol,
                "side":        trade.side,
                "entry_price": float(trade.entry_price or 0),
                "current_price": float(trade.current_price or 0),
                "pnl":         float(trade.unrealized_pnl or 0),
            }
            return {
                "mode": "paper",
                **_build_chart_pair(trade.symbol, trade_info),
            }
        except Exception as e:
            logger.warning("ChartPairView._from_paper_trade: %s", e)
            return None

    # ── Option trade ─────────────────────────────────────────
    def _from_option_trade(self, user) -> dict | None:
        try:
            from apps.orders.models import Order
            trade = (
                Order.objects
                
                .filter(user=user, mode="live", status=Order.Status.OPEN)
                .order_by("-created_at")
                .first()
            )
            if not trade:
                return None

            fyers_sym = trade.contract.fyers_symbol
            trade_info = {
                "source":        "options",
                "id":            str(trade.id),
                "symbol":        fyers_sym,
                "side":          trade.side,
                "entry_price":   float(trade.entry_price or 0),
                "current_price": float(trade.current_price or 0),
                "pnl":           float(trade.realized_pnl or 0),
            }
            return {
                "mode": "options",
                **_build_chart_pair(fyers_sym, trade_info),
            }
        except Exception as e:
            logger.warning("ChartPairView._from_option_trade: %s", e)
            return None

    # ── Live position ─────────────────────────────────────────
    def _from_live_position(self, user) -> dict | None:
        try:
            from apps.orders.models import Position
            pos = (
                Position.objects
                .select_related("asset")
                .filter(user=user, status=Position.Status.OPEN)
                .order_by("-opened_at")
                .first()
            )
            if not pos:
                return None

            sym = pos.symbol
            trade_info = {
                "source":        "live",
                "id":            str(pos.id),
                "symbol":        sym,
                "side":          pos.side,
                "entry_price":   float(pos.avg_entry_price or 0),
                "current_price": float(pos.current_price or 0),
                "pnl":           float(pos.unrealized_pnl or 0),
            }
            return {
                "mode": "live",
                **_build_chart_pair(sym, trade_info),
            }
        except Exception as e:
            logger.warning("ChartPairView._from_live_position: %s", e)
            return None

    # ── Symbol override ───────────────────────────────────────
    def _from_symbol(self, symbol: str) -> dict:
        return {
            "mode": "manual",
            **_build_chart_pair(symbol, {"source": "manual", "symbol": symbol}),
        }

# ── Candle-API compatible symbol ─────────────────────────────
_FYERS_TO_CANDLE = {
    'NSE:NIFTY50-INDEX':    'NIFTY',
    'NSE:NIFTYBANK-INDEX':  'BANKNIFTY',
    'NSE:FINNIFTY-INDEX':   'FINNIFTY',
    'BSE:SENSEX-INDEX':     'SENSEX',
    'NSE:MIDCPNIFTY-INDEX': 'MIDCPNIFTY',
    'NSE:BANKEX-INDEX':     'BANKEX',
}

def _to_candle_symbol(fyers_sym: str) -> str:
    if fyers_sym in _FYERS_TO_CANDLE:
        return _FYERS_TO_CANDLE[fyers_sym]
    clean = fyers_sym.upper().replace('NSE:', '').replace('BSE:', '')
    for base in ('BANKNIFTY', 'MIDCPNIFTY', 'FINNIFTY', 'SENSEX', 'NIFTY'):
        if clean.startswith(base):
            return base
    return fyers_sym
