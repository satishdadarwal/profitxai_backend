# apps/live_trading/views.py
# ✅ PRODUCTION FIX: Added symbol_selection endpoint for Go Live / Algo Trade screen
# Bug: Flutter ke "Go Live" screen pe symbol select karne ka koi API nahi tha
# Fix: New endpoints added:
#   GET  /api/live-trading/symbols/         → available symbols per broker
#   POST /api/live-trading/strategy/update/ → strategy symbol update
#   GET  /api/live-trading/strategy/<id>/   → single strategy detail

from django.shortcuts import render
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response


# ─────────────────────────────────────────────────────────────
#  1. Session Start / Stop
# ─────────────────────────────────────────────────────────────
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def start_session(request):
    """
    POST /api/live-trading/session/start/
    Body: { strategy_id, mode }
    """
    from .models import TradingMode, TradingSession

    strategy_id = request.data.get("strategy_id")
    mode        = request.data.get("mode", TradingMode.SEMI_AUTO)

    if mode not in TradingMode.values:
        return Response({"error": f"Invalid mode. Choose: {TradingMode.values}"}, status=400)

    TradingSession.objects.filter(user=request.user, is_active=True).update(
        is_active=False, ended_at=timezone.now()
    )

    session = TradingSession.objects.create(
        user        = request.user,
        strategy_id = strategy_id,
        mode        = mode,
    )

    return Response({
        "session_id":  session.id,
        "strategy_id": strategy_id,
        "mode":        mode,
        "started_at":  session.started_at.isoformat(),
        "message":     f"Session started in {mode.upper()} mode",
    })


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def stop_session(request):
    """POST /api/live-trading/session/stop/"""
    from .models import TradingSession
    from .tasks import close_session_summary_task

    session_id = request.data.get("session_id")
    try:
        session = TradingSession.objects.get(id=session_id, user=request.user, is_active=True)
    except TradingSession.DoesNotExist:
        return Response({"error": "Active session not found"}, status=404)

    session.is_active = False
    session.ended_at = timezone.now()
    session.save(update_fields=['is_active', 'ended_at'])
    close_session_summary_task.delay(session.id)
    return Response({"message": "Session stopped. Summary coming via WebSocket."})


# ─────────────────────────────────────────────────────────────
#  2. SEMI_AUTO: User Confirmation
# ─────────────────────────────────────────────────────────────
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def confirm_signal(request, signal_id: int):
    from .models import LiveSignal
    from .tasks import execute_trade_task

    try:
        signal = LiveSignal.objects.get(id=signal_id, user=request.user, status=LiveSignal.Status.PENDING)
    except LiveSignal.DoesNotExist:
        return Response({"error": "Signal not found or already acted upon"}, status=404)

    if signal.is_expired():
        signal.mark_expired()
        return Response({"error": "Signal expired", "status": "expired"}, status=410)

    execute_trade_task.apply_async(args=[signal.id, "semi_auto"], queue="orders")
    return Response({"message": "Executing trade...", "signal_id": signal_id})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def ignore_signal(request, signal_id: int):
    from .models import LiveSignal
    from .tasks import _log_activity

    try:
        signal = LiveSignal.objects.get(id=signal_id, user=request.user, status=LiveSignal.Status.PENDING)
    except LiveSignal.DoesNotExist:
        return Response({"error": "Signal not found"}, status=404)

    signal.mark_ignored()
    _log_activity(signal, "ignored", "User manually ignored")
    return Response({"message": "Signal ignored", "signal_id": signal_id})


# ─────────────────────────────────────────────────────────────
#  3. MANUAL Mode: FAB Order Placement
# ─────────────────────────────────────────────────────────────
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def place_manual_order(request):
    """
    POST /api/live-trading/manual-order/
    Body: { session_id, symbol, direction, lots, price?, stop_loss, take_profit, order_type }
    """
    from .models import ManualOrder, TradingSession
    from .tasks import manual_order_place_task

    try:
        session = TradingSession.objects.get(
            id=request.data.get("session_id"),
            user=request.user,
            is_active=True,
        )
    except TradingSession.DoesNotExist:
        return Response({"error": "No active session found"}, status=404)

    entry      = float(request.data.get("price") or 0)
    sl         = float(request.data.get("stop_loss") or 0)
    tp         = float(request.data.get("take_profit") or 0)
    lots       = float(request.data.get("lots", 1))
    direction  = request.data.get("direction", "buy")
    order_type = request.data.get("order_type", "MARKET")
    symbol     = request.data.get("symbol", "").strip()

    if not symbol:
        return Response({"error": "symbol is required"}, status=400)
    if lots <= 0:
        return Response({"error": "lots must be > 0"}, status=400)
    if direction not in ["buy", "sell"]:
        return Response({"error": "direction must be 'buy' or 'sell'"}, status=400)
    if order_type == "LIMIT" and entry <= 0:
        return Response({"error": "price required for LIMIT order"}, status=400)
    if sl and entry and direction == "buy" and sl >= entry:
        return Response({"error": "stop_loss must be below entry for BUY"}, status=400)
    if sl and entry and direction == "sell" and sl <= entry:
        return Response({"error": "stop_loss must be above entry for SELL"}, status=400)
    if tp and entry and direction == "buy" and tp <= entry:
        return Response({"error": "take_profit must be above entry for BUY"}, status=400)
    if tp and entry and direction == "sell" and tp >= entry:
        return Response({"error": "take_profit must be below entry for SELL"}, status=400)

    if entry and sl and tp:
        risk   = abs(entry - sl)
        reward = abs(tp - entry)
        rr     = round(reward / risk, 2) if risk else 0
        margin = lots * entry * 0.15
    else:
        rr, margin = 0, 0

    mo = ManualOrder.objects.create(
        session     = session,
        user        = request.user,
        symbol      = symbol,
        direction   = direction,
        order_type  = order_type,
        lots        = lots,
        price       = entry or None,
        stop_loss   = sl or None,
        take_profit = tp or None,
        rr_ratio    = rr,
        margin_req  = margin,
    )

    manual_order_place_task.apply_async(args=[mo.id], queue="orders")
    return Response({
        "manual_order_id": mo.id,
        "rr_ratio":        rr,
        "margin_required": margin,
        "message":         "Order queued for placement",
    }, status=201)


# ─────────────────────────────────────────────────────────────
#  4. Session Summary API
# ─────────────────────────────────────────────────────────────
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def session_summary(request, session_id: int):
    from .models import ActivityLog, LiveSignal, TradingSession

    try:
        session = TradingSession.objects.get(id=session_id, user=request.user)
    except TradingSession.DoesNotExist:
        return Response({"error": "Session not found"}, status=404)

    logs = ActivityLog.objects.filter(session=session).values(
        "symbol", "direction", "status", "mode", "pnl", "created_at", "note"
    )

    return Response({
        "session_id":      session.id,
        "strategy_id":     session.strategy_id,
        "mode":            session.mode,
        "started_at":      session.started_at.isoformat(),
        "ended_at":        session.ended_at.isoformat() if session.ended_at else None,
        "is_active":       session.is_active,
        "summary": {
            "total_trades":    session.total_trades,
            "winning_trades":  session.winning_trades,
            "win_rate":        round(session.win_rate, 1),
            "total_pnl":       float(session.total_pnl),
            "max_drawdown":    float(session.max_drawdown),
            "peak_equity":     float(session.peak_equity),
        },
        "activity_log":    list(logs),
    })


# ─────────────────────────────────────────────────────────────
#  5. Activity Log
# ─────────────────────────────────────────────────────────────
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def activity_log(request):
    from .models import ActivityLog

    session_id = request.query_params.get("session_id")
    qs = ActivityLog.objects.filter(user=request.user)
    if session_id:
        qs = qs.filter(session_id=session_id)

    data = list(qs.values(
        "id", "symbol", "direction", "status", "mode",
        "pnl", "note", "created_at", "entry_price",
    )[:50])
    return Response({"activity": data})


# ─────────────────────────────────────────────────────────────
#  6. Strategy List API
# ─────────────────────────────────────────────────────────────
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_strategies(request):
    """GET /api/live-trading/strategies/"""
    from apps.strategies.models import Strategy

    try:
        strategies = Strategy.objects.filter(
            user=request.user,
            is_active=True
        ).select_related('broker').order_by('-created_at')

        strategy_list = []
        for s in strategies:
            strategy_list.append({
                'id': str(s.id),
                'name': s.name,
                'algo_name': s.algo_name,
                'symbol': s.symbol,
                'symbols': s.symbols,
                'timeframe': s.timeframe,
                'default_lots': s.default_lots,
                'instrument_type': s.instrument_type,
                'mode': s.mode,
                'state': s.state,
                'broker': s.broker.broker if s.broker else None,
                'broker_id': s.broker.id if s.broker else None,
                'broker_name': s.broker.broker if s.broker else None,
                'risk_config': s.risk_config,
                'is_running': s.is_running,
                'is_active': s.is_active,
                'created_at': s.created_at.isoformat() if s.created_at else None,
                'updated_at': s.updated_at.isoformat() if s.updated_at else None
            })

        return Response({
            "success": True,
            "strategies": strategy_list,
            "count": len(strategy_list),
            "message": f"Found {len(strategy_list)} active strategies"
        })

    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Error fetching strategies: {str(e)}")
        return Response({"success": False, "strategies": [], "count": 0, "error": str(e)}, status=500)


# ─────────────────────────────────────────────────────────────
#  7. Active Sessions List
# ─────────────────────────────────────────────────────────────
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_active_sessions(request):
    """GET /api/live-trading/sessions/"""
    from .models import TradingSession
    from apps.strategies.models import Strategy

    try:
        sessions = TradingSession.objects.filter(
            user=request.user, is_active=True
        ).select_related('user').order_by('-started_at')

        strategy_ids = [s.strategy_id for s in sessions]
        strategies_map = {
            str(s.id): s
            for s in Strategy.objects.filter(
                id__in=strategy_ids, user=request.user
            ).select_related("broker")
        }

        session_list = []
        for session in sessions:
            strategy = strategies_map.get(str(session.strategy_id))
            session_list.append({
                'session_id': session.id,
                'strategy_id': str(session.strategy_id),
                'strategy_name': strategy.name if strategy else str(session.strategy_id),
                'symbol': strategy.symbol if strategy else "Unknown",
                'broker': strategy.broker.broker if strategy and strategy.broker else None,
                'mode': session.mode,
                'started_at': session.started_at.isoformat(),
                'is_active': session.is_active,
                'total_trades': session.total_trades,
                'winning_trades': session.winning_trades,
                'win_rate': float(session.win_rate),
                'total_pnl': float(session.total_pnl)
            })

        return Response({"success": True, "sessions": session_list, "count": len(session_list)})

    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Error fetching sessions: {str(e)}")
        return Response({"success": False, "sessions": [], "error": str(e)}, status=500)


# ─────────────────────────────────────────────────────────────
#  8. Dashboard Statistics
# ─────────────────────────────────────────────────────────────
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def dashboard_stats(request):
    """GET /api/live-trading/dashboard/"""
    from .models import TradingSession, LiveSignal
    from apps.strategies.models import Strategy
    from django.db.models import Sum

    try:
        total_strategies = Strategy.objects.filter(user=request.user, is_active=True).count()
        running_strategies = Strategy.objects.filter(user=request.user, is_active=True, state='running').count()
        active_sessions = TradingSession.objects.filter(user=request.user, is_active=True).count()
        total_sessions = TradingSession.objects.filter(user=request.user).count()
        today = timezone.now().date()
        signals_today = LiveSignal.objects.filter(user=request.user, detected_at__date=today).count()
        signals_pending = LiveSignal.objects.filter(user=request.user, status='pending').count()
        signals_executed = LiveSignal.objects.filter(user=request.user, status='executed').count()
        total_pnl = TradingSession.objects.filter(user=request.user).aggregate(total=Sum('total_pnl'))['total'] or 0
        total_trades = TradingSession.objects.filter(user=request.user).aggregate(
            total=Sum('total_trades'), winning=Sum('winning_trades')
        )
        overall_win_rate = 0
        if total_trades['total'] and total_trades['total'] > 0:
            overall_win_rate = (total_trades['winning'] / total_trades['total']) * 100

        return Response({
            "success": True,
            "stats": {
                "strategies": {"total": total_strategies, "running": running_strategies, "idle": total_strategies - running_strategies},
                "sessions": {"active": active_sessions, "total": total_sessions},
                "signals": {"today": signals_today, "pending": signals_pending, "executed": signals_executed},
                "performance": {
                    "total_pnl": float(total_pnl),
                    "total_trades": total_trades['total'] or 0,
                    "winning_trades": total_trades['winning'] or 0,
                    "win_rate": round(overall_win_rate, 2)
                }
            }
        })

    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Error fetching dashboard stats: {str(e)}")
        return Response({"success": False, "error": str(e)}, status=500)


# ─────────────────────────────────────────────────────────────
#  9. Pending Signals List
# ─────────────────────────────────────────────────────────────
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_pending_signals(request):
    """GET /api/live-trading/signals/pending/"""
    from .models import LiveSignal

    try:
        signals = LiveSignal.objects.filter(
            user=request.user, status='pending'
        ).select_related('session').order_by('-detected_at')[:20]

        signal_list = [{
            'signal_id': s.id,
            'session_id': s.session_id,
            'strategy_id': s.strategy_id,
            'symbol': s.symbol,
            'direction': s.direction,
            'signal_type': s.signal_type,
            'strength': s.strength,
            'entry_price': float(s.entry_price),
            'stop_loss': float(s.stop_loss),
            'take_profit': float(s.take_profit),
            'rr_ratio': float(s.rr_ratio),
            'lots': float(s.lots),
            'margin_req': float(s.margin_req),
            'mode': s.mode,
            'status': s.status,
            'detected_at': s.detected_at.isoformat(),
            'expires_at': s.expires_at.isoformat() if s.expires_at else None,
            'is_expired': s.is_expired()
        } for s in signals]

        return Response({"success": True, "signals": signal_list, "count": len(signal_list)})

    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Error fetching signals: {str(e)}")
        return Response({"success": False, "signals": [], "error": str(e)}, status=500)


# ─────────────────────────────────────────────────────────────
#  10. Recent Signals
# ─────────────────────────────────────────────────────────────
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_recent_signals(request):
    """GET /api/live-trading/signals/recent/"""
    from .models import LiveSignal

    try:
        limit = int(request.query_params.get('limit', 20))
        signal_status = request.query_params.get('status', None)
        queryset = LiveSignal.objects.filter(user=request.user)
        if signal_status:
            queryset = queryset.filter(status=signal_status)
        signals = queryset.select_related('session').order_by('-detected_at')[:limit]

        signal_list = [{
            'signal_id': s.id,
            'session_id': s.session_id,
            'strategy_id': s.strategy_id,
            'symbol': s.symbol,
            'direction': s.direction,
            'signal_type': s.signal_type,
            'strength': s.strength,
            'entry_price': float(s.entry_price),
            'stop_loss': float(s.stop_loss),
            'take_profit': float(s.take_profit),
            'rr_ratio': float(s.rr_ratio),
            'status': s.status,
            'mode': s.mode,
            'detected_at': s.detected_at.isoformat(),
            'acted_at': s.acted_at.isoformat() if s.acted_at else None
        } for s in signals]

        return Response({"success": True, "signals": signal_list, "count": len(signal_list)})

    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Error fetching recent signals: {str(e)}")
        return Response({"success": False, "signals": [], "error": str(e)}, status=500)


# ═══════════════════════════════════════════════════════════════════
#  ✅ NEW: Symbol Selection for Go Live / Algo Trade Screen
#  Flutter "Go Live" screen pe symbol select karne ka pura flow
# ═══════════════════════════════════════════════════════════════════

# Broker-wise available symbols
_FYERS_SYMBOLS = [
    # Indices
    {"symbol": "NSE:NIFTY50-INDEX",    "display": "NIFTY 50",       "type": "index",   "exchange": "NSE"},
    {"symbol": "NSE:NIFTYBANK-INDEX",  "display": "BANK NIFTY",     "type": "index",   "exchange": "NSE"},
    {"symbol": "NSE:FINNIFTY-INDEX",   "display": "FIN NIFTY",      "type": "index",   "exchange": "NSE"},
    {"symbol": "NSE:MIDCPNIFTY-INDEX", "display": "MIDCAP NIFTY",   "type": "index",   "exchange": "NSE"},
    {"symbol": "BSE:SENSEX-INDEX",     "display": "SENSEX",         "type": "index",   "exchange": "BSE"},
    # Top equity
    {"symbol": "NSE:RELIANCE-EQ",      "display": "Reliance",       "type": "equity",  "exchange": "NSE"},
    {"symbol": "NSE:TCS-EQ",           "display": "TCS",            "type": "equity",  "exchange": "NSE"},
    {"symbol": "NSE:HDFCBANK-EQ",      "display": "HDFC Bank",      "type": "equity",  "exchange": "NSE"},
    {"symbol": "NSE:INFY-EQ",          "display": "Infosys",        "type": "equity",  "exchange": "NSE"},
    {"symbol": "NSE:ICICIBANK-EQ",     "display": "ICICI Bank",     "type": "equity",  "exchange": "NSE"},
    {"symbol": "NSE:SBIN-EQ",          "display": "SBI",            "type": "equity",  "exchange": "NSE"},
    {"symbol": "NSE:BAJFINANCE-EQ",    "display": "Bajaj Finance",  "type": "equity",  "exchange": "NSE"},
    {"symbol": "NSE:ADANIENT-EQ",      "display": "Adani Ent.",     "type": "equity",  "exchange": "NSE"},
    {"symbol": "NSE:WIPRO-EQ",         "display": "Wipro",          "type": "equity",  "exchange": "NSE"},
    {"symbol": "NSE:AXISBANK-EQ",      "display": "Axis Bank",      "type": "equity",  "exchange": "NSE"},
]

_DELTA_SYMBOLS = [
    {"symbol": "BTCUSD",  "display": "Bitcoin (BTC)",   "type": "perp",    "exchange": "DELTA"},
    {"symbol": "ETHUSD",  "display": "Ethereum (ETH)",  "type": "perp",    "exchange": "DELTA"},
    {"symbol": "SOLUSD",  "display": "Solana (SOL)",    "type": "perp",    "exchange": "DELTA"},
    {"symbol": "BNBUSD",  "display": "BNB",             "type": "perp",    "exchange": "DELTA"},
    {"symbol": "XRPUSD",  "display": "XRP",             "type": "perp",    "exchange": "DELTA"},
    {"symbol": "ADAUSD",  "display": "Cardano (ADA)",   "type": "perp",    "exchange": "DELTA"},
    {"symbol": "DOGEUSD", "display": "Dogecoin",        "type": "perp",    "exchange": "DELTA"},
    {"symbol": "AVAXUSD", "display": "Avalanche (AVAX)","type": "perp",    "exchange": "DELTA"},
    {"symbol": "LTCUSD",  "display": "Litecoin (LTC)",  "type": "perp",    "exchange": "DELTA"},
]

_ZERODHA_SYMBOLS = [
    {"symbol": "NIFTY",      "display": "NIFTY 50",      "type": "index",  "exchange": "NSE"},
    {"symbol": "BANKNIFTY",  "display": "BANK NIFTY",    "type": "index",  "exchange": "NSE"},
    {"symbol": "RELIANCE",   "display": "Reliance",      "type": "equity", "exchange": "NSE"},
    {"symbol": "TCS",        "display": "TCS",           "type": "equity", "exchange": "NSE"},
    {"symbol": "HDFCBANK",   "display": "HDFC Bank",     "type": "equity", "exchange": "NSE"},
    {"symbol": "INFY",       "display": "Infosys",       "type": "equity", "exchange": "NSE"},
]

_BROKER_SYMBOL_MAP = {
    "fyers":   _FYERS_SYMBOLS,
    "delta":   _DELTA_SYMBOLS,
    "zerodha": _ZERODHA_SYMBOLS,
}


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_available_symbols(request):
    """
    ✅ NEW: GET /api/live-trading/symbols/

    Go Live screen pe symbol selection ke liye.
    Query params:
        broker   = fyers | delta | zerodha  (optional — filters by broker)
        strategy = <strategy_id>             (optional — auto-selects broker from strategy)
        type     = index | equity | perp     (optional — filter by instrument type)
        q        = search_query              (optional — search by symbol/display name)

    Response:
        {
          "success": true,
          "broker": "fyers",
          "symbols": [
            {"symbol": "NSE:NIFTY50-INDEX", "display": "NIFTY 50", "type": "index", "exchange": "NSE"}
          ],
          "count": 15
        }
    """
    from apps.strategies.models import Strategy
    from apps.brokers.models import BrokerAccount

    try:
        broker_slug = request.query_params.get("broker", "").lower()
        strategy_id = request.query_params.get("strategy", "")
        type_filter = request.query_params.get("type", "").lower()
        search_q    = request.query_params.get("q", "").lower().strip()

        # Strategy se broker auto-detect
        if strategy_id and not broker_slug:
            try:
                strategy = Strategy.objects.get(id=strategy_id, user=request.user)
                if strategy.broker:
                    broker_slug = strategy.broker.broker.lower()
            except Strategy.DoesNotExist:
                pass

        # User ke active broker se detect (fallback)
        if not broker_slug:
            account = BrokerAccount.objects.filter(
                user=request.user, is_active=True, is_verified=True
            ).first()
            if account:
                broker_slug = account.broker.lower()

        # Symbols fetch karo
        if broker_slug and broker_slug in _BROKER_SYMBOL_MAP:
            symbols = _BROKER_SYMBOL_MAP[broker_slug]
        else:
            # Saare broker ke symbols (broker unknown hai toh)
            symbols = []
            for b_symbols in _BROKER_SYMBOL_MAP.values():
                symbols.extend(b_symbols)

        # Filter by type
        if type_filter:
            symbols = [s for s in symbols if s["type"] == type_filter]

        # Search filter
        if search_q:
            symbols = [
                s for s in symbols
                if search_q in s["symbol"].lower() or search_q in s["display"].lower()
            ]

        return Response({
            "success": True,
            "broker": broker_slug or "all",
            "symbols": symbols,
            "count": len(symbols),
        })

    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"get_available_symbols error: {e}")
        return Response({"success": False, "symbols": [], "error": str(e)}, status=500)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def update_strategy_symbol(request):
    """
    ✅ NEW: POST /api/live-trading/strategy/update-symbol/

    Go Live screen pe user ne symbol change kiya.
    Body: { strategy_id, symbol, symbols? }

    Flutter flow:
        1. User opens Go Live screen
        2. GET /api/live-trading/symbols/?strategy=<id>   ← symbol list
        3. User selects symbol
        4. POST /api/live-trading/strategy/update-symbol/ ← save selection
        5. POST /api/live-trading/session/start/          ← start trading
    """
    from apps.strategies.models import Strategy

    strategy_id = request.data.get("strategy_id")
    symbol      = request.data.get("symbol", "").strip()
    symbols     = request.data.get("symbols", [])

    if not strategy_id:
        return Response({"error": "strategy_id required"}, status=400)
    if not symbol:
        return Response({"error": "symbol required"}, status=400)

    try:
        strategy = Strategy.objects.get(id=strategy_id, user=request.user)
    except Strategy.DoesNotExist:
        return Response({"error": "Strategy not found"}, status=404)

    strategy.symbol = symbol
    update_fields = ["symbol"]

    # Multi-symbol support
    if symbols and isinstance(symbols, list):
        strategy.symbols = symbols
        update_fields.append("symbols")

    strategy.save(update_fields=update_fields)

    return Response({
        "success": True,
        "strategy_id": str(strategy.id),
        "symbol": strategy.symbol,
        "symbols": strategy.symbols,
        "message": f"Symbol updated to {symbol}",
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_strategy_detail(request, strategy_id: str):
    """
    ✅ NEW: GET /api/live-trading/strategy/<strategy_id>/

    Go Live screen pe single strategy ka full detail.
    Flutter is data ko pre-fill karta hai order form mein.
    """
    from apps.strategies.models import Strategy

    try:
        strategy = Strategy.objects.select_related("broker").get(
            id=strategy_id, user=request.user
        )
    except Strategy.DoesNotExist:
        return Response({"error": "Strategy not found"}, status=404)

    # Broker ke available symbols
    broker_slug = strategy.broker.broker.lower() if strategy.broker else ""
    available_symbols = _BROKER_SYMBOL_MAP.get(broker_slug, [])

    return Response({
        "success": True,
        "strategy": {
            "id": str(strategy.id),
            "name": strategy.name,
            "algo_name": strategy.algo_name,
            "symbol": strategy.symbol,
            "symbols": strategy.symbols,
            "timeframe": strategy.timeframe,
            "default_lots": strategy.default_lots,
            "instrument_type": strategy.instrument_type,
            "mode": strategy.mode,
            "state": strategy.state,
            "risk_config": strategy.risk_config,
            "is_running": strategy.is_running,
            "broker": {
                "id": strategy.broker.id if strategy.broker else None,
                "name": strategy.broker.broker if strategy.broker else None,
                "is_active": strategy.broker.is_active if strategy.broker else False,
                "is_verified": strategy.broker.is_verified if strategy.broker else False,
            },
            "available_symbols": available_symbols,
        }
    })