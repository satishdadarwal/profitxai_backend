# apps/strategies/views.py
#
# CHANGES:
#   [Global Strategy] StrategyOwnerMixin.get_strategy() → apni + global strategies support
#   [Global Strategy] StrategyListCreateView.get() → own + global strategies return karo
#   [Global Strategy] StrategyDetailView.patch/delete → global strategy edit/delete block karo
#   [Global Strategy] AdminGlobalStrategyCreateView → naya admin-only endpoint
#   [Existing]  ManualOrderView, CapitalWarningView, StrategyActivityLogView — unchanged

import logging

from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from datetime import datetime

from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.generics import ListAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from apps.orders.models import Trade
from django.db.models import Sum

from apps.subscriptions.permissions import CanAddStrategy, CanLiveTrade

# ✅ Model imports
from apps.paper_trading.models import PaperTrade
from apps.options.models import OptionTrade

from .models import Strategy, StrategyPerformanceSnapshot, StrategySignal
from .serializers import (
    StrategyPerformanceSnapshotSerializer,
    StrategySerializer,
    StrategySignalSerializer,
    StrategyWriteSerializer,
)
from .services import (
    AlgoNotFoundError,
    LiveTradingNotAllowedError,
    StrategyAlreadyRunningError,
    StrategyError,
    StrategyNotRunningError,
    build_performance,
    start_strategy,
    stop_strategy,
    toggle_mode,
)

logger = logging.getLogger(__name__)




# ─────────────────────────────────────────────────────────────────
#  Helper: User ka active plan name fetch karo (Subscription se)
#  user.plan field reliable nahi — directly Subscription check karo
# ─────────────────────────────────────────────────────────────────
def _get_active_plan_name(user) -> str | None:
    """
    User ka currently active plan name return karo.
    Returns: Plan.name string (e.g. "Basic", "Pro", "Elite")
             None agar free ya koi active subscription nahi

    Priority order:
    1. user.plan field — primary source (directly set by admin/payment)
    2. Active Subscription → plan.name use karo (agar subscription exist kare)

    NOTE: Kai users ka Subscription record nahi hota (admin ne manually
    user.plan set kiya hoga). Dono cases handle karo.
    """
    # ── Priority 1: user.plan field directly check karo ──────────
    plan_val = getattr(user, 'plan', 'free') or 'free'
    if plan_val != 'free':
        return plan_val.capitalize()  # "elite" → "Elite", "pro" → "Pro"

    # ── Priority 2: Subscription table se check karo ─────────────
    try:
        from apps.subscriptions.models import Subscription, Plan
        # .subscription direct access crash karta hai agar record nahi —
        # isliye filter() use karo jo empty queryset deta hai
        sub = Subscription.objects.filter(
            user=user
        ).select_related('plan').first()

        if sub and sub.is_access_granted and sub.plan.tier > Plan.Tier.FREE:
            return sub.plan.name  # e.g. "Basic", "Pro", "Elite"

    except Exception:
        pass

    return None

# ─────────────────────────────────────────────────────────────────
#  Mixin — apni strategy ya accessible global strategy
# ─────────────────────────────────────────────────────────────────
class StrategyOwnerMixin:
    def get_strategy(self, strategy_id) -> Strategy:
        """
        Strategy fetch karo:
        - User ki apni strategy  → full access (edit/delete/start/stop)
        - Global strategy        → start/stop allowed, edit/delete blocked (view mein enforce hota hai)
        - Dusre user ki private  → 404
        """
        user = self.request.user

        strategy = Strategy.objects.filter(
            pk=strategy_id,
            is_active=True,
        ).select_related("broker").first()

        if not strategy:
            from django.http import Http404
            raise Http404("Strategy not found.")

        # is_visible_to_user() model method use karo (models.py mein defined hai)
        if not strategy.is_visible_to_user(user):
            from django.http import Http404
            raise Http404("Strategy not found.")

        return strategy


# ─────────────────────────────────────────────────────────────────
#  1. List + Create
# ─────────────────────────────────────────────────────────────────
class StrategyListCreateView(StrategyOwnerMixin, APIView):
    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAuthenticated(), CanAddStrategy()]
        return [IsAuthenticated()]

    def get(self, request):
        user = request.user

        # Apni strategies
        own_q = Q(user=user, is_active=True)

        # #BUG-FIX: user.plan lowercase 'elite' vs DB 'Elite' — case mismatch fix
        plan_val = getattr(user, 'plan', None) or 'free'
        if plan_val != 'free':
            plan_variants = list({plan_val, plan_val.capitalize(), plan_val.upper()})
            plan_q = Q(allowed_plans=[])
            for v in plan_variants:
                plan_q |= Q(allowed_plans__contains=[v])
            global_q = Q(is_global=True, is_active=True) & plan_q
        else:
            global_q = Q(is_global=True, is_active=True, allowed_plans=[])

        qs = Strategy.objects.filter(own_q | global_q).select_related("broker").distinct()

        # #NEW: Broker-based instrument filter
        # Fyers/Zerodha/Dhan → perp/crypto exclude
        # Delta/Binance → options/futures/equity exclude
        # Dono hain → sab dikhao
        from apps.brokers.models import BrokerAccount
        user_brokers = set(BrokerAccount.objects.filter(user=user, is_active=True).values_list('broker', flat=True))
        indian = {'fyers','zerodha','dhan','upstox','angel','iifl'}
        crypto = {'delta','binance','bybit'}
        has_indian = bool(user_brokers & indian)
        has_crypto = bool(user_brokers & crypto)
        if has_indian and not has_crypto:
            qs = qs.exclude(instrument_type__in=['perp','crypto'])
        elif has_crypto and not has_indian:
            qs = qs.exclude(instrument_type__in=['options','futures','equity'])

        if state := request.query_params.get("state"):
            qs = qs.filter(state=state)
        if mode := request.query_params.get("mode"):
            qs = qs.filter(mode=mode)

        return Response({
            "strategies": StrategySerializer(qs, many=True, context={"request": request}).data
        })

    def post(self, request):
        # ✅ ADMIN-ONLY: Sirf admin strategies create kar sakta hai
        # Regular users global strategies use karte hain
        if not request.user.is_staff:
            return Response(
                {
                    "error": "Strategy create karne ki permission nahi hai.",
                    "hint": "Admin se contact karo — global strategies use karo.",
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        ser = StrategyWriteSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)

        if ser.validated_data.get("mode") == Strategy.Mode.LIVE:
            perm = CanLiveTrade()
            if not perm.has_permission(request, self):
                return Response(
                    {"error": perm.message}, status=status.HTTP_403_FORBIDDEN
                )

        # Admin-created strategy automatically global mark karo
        strategy = ser.save(
            user=request.user,
            is_global=True,
            created_by_admin=True,
        )
        logger.info("Strategy created by admin | id=%s | user=%s", strategy.id, request.user.id)

        return Response(
            StrategySerializer(strategy, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


# ─────────────────────────────────────────────────────────────────
#  2. Retrieve + Update + Soft-delete
# ─────────────────────────────────────────────────────────────────
class StrategyDetailView(StrategyOwnerMixin, APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, strategy_id):
        strategy = self.get_strategy(strategy_id)
        return Response(StrategySerializer(strategy, context={"request": request}).data)

    def patch(self, request, strategy_id):
        strategy = self.get_strategy(strategy_id)

        # ✅ Global strategy user edit nahi kar sakta
        if strategy.is_global and not request.user.is_staff:
            return Response(
                {"error": "Global strategy edit nahi ki ja sakti. Admin se contact karo."},
                status=status.HTTP_403_FORBIDDEN,
            )

        if strategy.is_running:
            return Response(
                {"error": "Stop the strategy before editing."},
                status=status.HTTP_409_CONFLICT,
            )

        ser = StrategyWriteSerializer(strategy, data=request.data, partial=True)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)

        new_mode = ser.validated_data.get("mode", strategy.mode)
        if new_mode == Strategy.Mode.LIVE and strategy.mode != Strategy.Mode.LIVE:
            perm = CanLiveTrade()
            if not perm.has_permission(request, self):
                return Response(
                    {"error": perm.message}, status=status.HTTP_403_FORBIDDEN
                )

        updated = ser.save()
        return Response(StrategySerializer(updated, context={"request": request}).data)

    def delete(self, request, strategy_id):
        strategy = self.get_strategy(strategy_id)

        # ✅ Global strategy user delete nahi kar sakta
        if strategy.is_global and not request.user.is_staff:
            return Response(
                {"error": "Global strategy delete nahi ki ja sakti."},
                status=status.HTTP_403_FORBIDDEN,
            )

        if strategy.is_running:
            stop_strategy(strategy, reason="Strategy deleted by user")

        strategy.is_active = False
        strategy.save(update_fields=["is_active", "updated_at"])

        return Response(status=status.HTTP_204_NO_CONTENT)


# ─────────────────────────────────────────────────────────────────
#  3. Start
# ─────────────────────────────────────────────────────────────────
class StrategyStartView(StrategyOwnerMixin, APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, strategy_id):
        strategy = self.get_strategy(strategy_id)

        try:
            updated = start_strategy(strategy, requested_by=request.user)
        except StrategyAlreadyRunningError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_409_CONFLICT)
        except LiveTradingNotAllowedError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except AlgoNotFoundError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except StrategyError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            {
                "message": f"Strategy '{updated.name}' started.",
                "strategy": StrategySerializer(updated, context={"request": request}).data,
            }
        )


# ─────────────────────────────────────────────────────────────────
#  4. Stop
# ─────────────────────────────────────────────────────────────────
class StrategyStopView(StrategyOwnerMixin, APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, strategy_id):
        strategy = self.get_strategy(strategy_id)
        reason = request.data.get("reason", "Manual stop by user")

        try:
            updated = stop_strategy(strategy, reason=reason)
        except StrategyNotRunningError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_409_CONFLICT)

        return Response(
            {
                "message": f"Strategy '{updated.name}' stopped.",
                "strategy": StrategySerializer(updated, context={"request": request}).data,
            }
        )


# ─────────────────────────────────────────────────────────────────
#  5. Toggle mode
# ─────────────────────────────────────────────────────────────────
class StrategyToggleModeView(StrategyOwnerMixin, APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, strategy_id):
        strategy = self.get_strategy(strategy_id)

        try:
            updated = toggle_mode(strategy)
        except LiveTradingNotAllowedError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except StrategyError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_409_CONFLICT)

        return Response(
            {
                "message": f"Mode switched to {updated.mode.upper()}.",
                "strategy": StrategySerializer(updated, context={"request": request}).data,
            }
        )


# ─────────────────────────────────────────────────────────────────
#  5b. User Strategy Preference — Flutter app se mode choose karo
#
#  GET  /strategies/<id>/preference/  → current preference fetch karo
#  POST /strategies/<id>/preference/  → mode set karo (paper/live)
#                                       + start/stop toggle
#
#  Yeh global strategy ke liye hai jahan admin ne ek strategy banai
#  hai lekin har user apna mode (paper/live) choose kar sakta hai.
# ─────────────────────────────────────────────────────────────────
class UserStrategyPreferenceView(APIView):
    permission_classes = [IsAuthenticated]

    def _get_strategy(self, strategy_id):
        """Strategy fetch karo — global ya apni dono."""
        try:
            strategy = Strategy.objects.get(pk=strategy_id)
        except Strategy.DoesNotExist:
            return None
        # Visible check
        if not strategy.is_visible_to_user(self.request.user):
            return None
        return strategy

    def get(self, request, strategy_id):
        """Current user ka preference fetch karo."""
        from .models import UserStrategyPreference

        strategy = self._get_strategy(strategy_id)
        if not strategy:
            return Response({"error": "Strategy not found"}, status=404)

        pref, _ = UserStrategyPreference.objects.get_or_create(
            user=request.user,
            strategy=strategy,
            defaults={"preferred_mode": strategy.mode},
        )

        # Live trading allowed check
        from .services import _user_can_live_trade
        can_live = _user_can_live_trade(request.user)

        return Response({
            "strategy_id":     str(strategy.id),
            "strategy_name":   strategy.name,
            "master_mode":     strategy.mode,
            "preferred_mode":  pref.preferred_mode,
            "effective_mode":  pref.effective_mode(),
            "is_running":      pref.is_running,
            "can_live_trade":  can_live,
            "algo_name":       strategy.algo_name,
            "symbols":         strategy.symbols or [strategy.symbol],
            "exit_mode":       pref.exit_mode,
        })

    def post(self, request, strategy_id):
        """
        User apna mode set kare aur start/stop kare.

        Body:
        {
            "preferred_mode": "paper" | "live",   # optional
            "action": "start" | "stop"            # optional
        }
        """
        from .models import UserStrategyPreference
        from .services import _user_can_live_trade

        strategy = self._get_strategy(strategy_id)
        if not strategy:
            return Response({"error": "Strategy not found"}, status=404)

        pref, _ = UserStrategyPreference.objects.get_or_create(
            user=request.user,
            strategy=strategy,
            defaults={"preferred_mode": strategy.mode},
        )

        new_mode   = request.data.get("preferred_mode")
        action     = request.data.get("action")  # "start" | "stop"
        new_exit_mode = request.data.get("exit_mode")

        # ── Exit mode change ─────────────────────────────────────
        _valid_exit_modes = ('gtt_oco', 'smart_trail', 'both')
        if new_exit_mode is not None:
            if new_exit_mode not in _valid_exit_modes:
                return Response(
                    {"error": f"exit_mode must be one of: {', '.join(_valid_exit_modes)}"},
                    status=400,
                )
            pref.exit_mode = new_exit_mode

        # ── Mode change ──────────────────────────────────────────
        if new_mode:
            if new_mode not in ("paper", "live"):
                return Response(
                    {"error": "preferred_mode must be 'paper' or 'live'"},
                    status=400,
                )
            # Live trading permission check
            if new_mode == "live" and not _user_can_live_trade(request.user):
                return Response(
                    {"error": "Live trading ke liye Basic plan ya usse upar chahiye."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            pref.preferred_mode = new_mode

        # ── Start / Stop ─────────────────────────────────────────
        if action == "start":
            pref.is_running = True
            logger.info(
                "UserPref START | user=%s | strategy=%s | mode=%s",
                request.user.pk, strategy.id, pref.preferred_mode,
            )
        elif action == "stop":
            pref.is_running = False
            logger.info(
                "UserPref STOP | user=%s | strategy=%s",
                request.user.pk, strategy.id,
            )

        pref.save()

        return Response({
            "success":        True,
            "strategy_id":    str(strategy.id),
            "preferred_mode": pref.preferred_mode,
            "effective_mode": pref.effective_mode(),
            "is_running":     pref.is_running,
            "exit_mode":      pref.exit_mode,
            "message": (
                f"Mode: {pref.preferred_mode.upper()} | "
                f"{'▶ Running' if pref.is_running else '⏹ Stopped'}"
            ),
        })


# ─────────────────────────────────────────────────────────────────
#  6. Signals
# ─────────────────────────────────────────────────────────────────
class StrategySignalListView(StrategyOwnerMixin, APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, strategy_id):
        strategy = self.get_strategy(strategy_id)
        qs = strategy.signals.select_related("order")

        if sig_type := request.query_params.get("signal_type"):
            qs = qs.filter(signal_type=sig_type)
        if result := request.query_params.get("result"):
            qs = qs.filter(result=result)

        limit = min(int(request.query_params.get("limit", 50)), 500)
        qs = qs.order_by("-created_at")[:limit]

        return Response(
            {
                "strategy_id": str(strategy.id),
                "signals": StrategySignalSerializer(qs, many=True).data,
            }
        )


# ─────────────────────────────────────────────────────────────────
#  7. Performance
# ─────────────────────────────────────────────────────────────────
class StrategyPerformanceView(StrategyOwnerMixin, APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, strategy_id):
        strategy = self.get_strategy(strategy_id)

        try:
            days = max(1, min(int(request.query_params.get("days", 30)), 365))
        except (ValueError, TypeError):
            days = 30

        perf = build_performance(strategy, days=days)
        return Response(perf)


# ─────────────────────────────────────────────────────────────────
#  8. Performance snapshots
# ─────────────────────────────────────────────────────────────────
class StrategySnapshotListView(StrategyOwnerMixin, APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, strategy_id):
        strategy = self.get_strategy(strategy_id)
        granularity = request.query_params.get(
            "granularity", StrategyPerformanceSnapshot.Granularity.DAILY
        )
        limit = min(int(request.query_params.get("limit", 30)), 365)

        qs = strategy.performance_snapshots.filter(granularity=granularity).order_by(
            "-period_start"
        )[:limit]

        return Response(StrategyPerformanceSnapshotSerializer(qs, many=True).data)


# ─────────────────────────────────────────────────────────────────
#  9. Available algorithms
# ─────────────────────────────────────────────────────────────────
class AlgoListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.backtest.engine import _REGISTRY

        algos = [
            {"name": name, "class": cls.__name__} for name, cls in _REGISTRY.items()
        ]
        return Response({"algorithms": algos})


# ─────────────────────────────────────────────────────────────────
#  10. Backtest
# ─────────────────────────────────────────────────────────────────
class StrategyBacktestView(StrategyOwnerMixin, APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, strategy_id):
        strategy = self.get_strategy(strategy_id)
        from_date = request.data.get("from_date")
        to_date = request.data.get("to_date")
        timeframe = request.data.get("timeframe", "15")

        from .services import run_backtest

        try:
            result = run_backtest(strategy, from_date, to_date, timeframe)
            return Response(result)
        except Exception as e:
            return Response({"error": str(e)}, status=400)


# ─────────────────────────────────────────────────────────────────
#  11. Activity Log
# ─────────────────────────────────────────────────────────────────
class StrategyActivityLogView(APIView):
    permission_classes = [IsAuthenticated]

    # ── Helpers ───────────────────────────────────────────────────

    def _get_status(self, trade):
        if hasattr(trade, "is_open") and not callable(getattr(type(trade), "is_open", None)):
            return "open" if trade.is_open else "closed"

        s = getattr(trade, "status", None) or getattr(trade, "state", None)
        if s is not None:
            val = str(s).lower()
            if val in ("open", "active", "running"):
                return "open"
            if val in ("closed", "complete", "completed", "filled", "cancelled", "expired"):
                return "closed"
            return val

        order = getattr(trade, "order", None)
        if order is not None:
            order_status = str(getattr(order, "status", "")).lower()
            if order_status in ("open", "active"):
                return "open"
            return "closed"

        return "unknown"

    def _get_pnl(self, trade):
        val = getattr(trade, "pnl", None)
        if val is None:
            val = getattr(trade, "realized_pnl", None)
        try:
            return float(val or 0)
        except (TypeError, ValueError):
            return 0.0

    def _get_side(self, trade):
        side = getattr(trade, "side", None) or getattr(trade, "action", None)
        return str(side).lower() if side is not None else ""

    def _get_quantity(self, trade):
        try:
            return float(getattr(trade, "quantity", None) or 0)
        except (TypeError, ValueError):
            return 0.0

    def _get_lot_size(self, trade):
        try:
            return int(getattr(trade, "lot_size", None) or 1)
        except (TypeError, ValueError):
            return 1

    def _get_entry_price(self, trade):
        return (
            getattr(trade, "entry_price", None)
            or getattr(trade, "entry_rate", None)
            or getattr(trade, "entry", None)
            or getattr(trade, "price", None)
        )

    def _get_exit_price(self, trade):
        return (
            getattr(trade, "exit_price", None)
            or getattr(trade, "exit_rate", None)
            or getattr(trade, "exit", None)
        )

    def _get_current_price(self, trade):
        return getattr(trade, "current_price", None)

    def _get_opened_at(self, trade):
        return (
            getattr(trade, "opened_at", None)
            or getattr(trade, "entry_time", None)
            or getattr(trade, "created_at", None)
        )

    def _get_closed_at(self, trade):
        return (
            getattr(trade, "closed_at", None)
            or getattr(trade, "exit_time", None)
        )

    def _normalize_asset_type(self, raw, symbol):
        raw_str = str(raw or "").lower()
        symbol_str = str(symbol or "").upper()
        if "option" in raw_str:
            return "option"
        if "future" in raw_str or "fut" in raw_str:
            return "future"
        if (
            "crypto" in raw_str
            or symbol_str.endswith("USDT")
            or symbol_str.endswith("BTC")
        ):
            return "crypto"
        return "stock"

    # ── GET ───────────────────────────────────────────────────────

    def get(self, request):
        date_filter = request.GET.get("date_filter", "all")
        # #NEW: mode_filter — live screen sirf live trades dekhna chahti hai
        # paper screen sirf paper. None = sab dikhao (backward compatible)
        mode_filter = request.GET.get("mode_filter", None)  # "live" | "paper" | None

        today_start = None
        if date_filter == "today":
            today_start = timezone.now().replace(
                hour=0, minute=0, second=0, microsecond=0
            )

        # ── FIX: Global strategies bhi include karo ─────────────
        # Pehle sirf user=request.user tha — global strategies
        # algo_trading_screen pe nahi dikhti thi
        user = request.user
        # #BUG-FIX: plan case mismatch fix + broker filter
        plan_val = getattr(user, 'plan', 'free') or 'free'
        own_q = Q(user=user)
        if plan_val != 'free':
            plan_variants = list({plan_val, plan_val.capitalize(), plan_val.upper()})
            plan_q = Q(allowed_plans=[])
            for variant in plan_variants:
                plan_q |= Q(allowed_plans__contains=[variant])
            global_q = Q(is_global=True) & plan_q
        else:
            global_q = Q(is_global=True, allowed_plans=[])

        strategies_qs = Strategy.objects.filter(own_q | global_q).order_by("-created_at").distinct()

        from apps.brokers.models import BrokerAccount as _BA
        _ub = set(_BA.objects.filter(user=user, is_active=True).values_list('broker', flat=True))
        _ind = {'fyers','zerodha','dhan','upstox','angel','iifl'}
        _cry = {'delta','binance','bybit'}
        if bool(_ub & _ind) and not bool(_ub & _cry):
            strategies_qs = strategies_qs.exclude(instrument_type__in=['perp','crypto'])
        elif bool(_ub & _cry) and not bool(_ub & _ind):
            strategies_qs = strategies_qs.exclude(instrument_type__in=['options','futures','equity'])

        strategies = list(strategies_qs)

        activity_data = []
        total_realized = 0.0
        total_unrealized = 0.0
        total_open = 0
        total_closed = 0

        for strategy in strategies:
            all_trades = []

            # ✅ FIX: User ka preferred_mode use karo (agar set hai)
            # Global strategy = admin ne banai, user choose karta hai paper/live
            # Apni strategy = strategy.mode hi use hoga
            from .models import UserStrategyPreference
            try:
                pref = UserStrategyPreference.objects.get(
                    user=request.user, strategy=strategy
                )
                effective_mode = pref.preferred_mode
            except UserStrategyPreference.DoesNotExist:
                effective_mode = strategy.mode

            # #FIX: mode_filter se paper/live alag karo
            if mode_filter and effective_mode != mode_filter:
                continue

            if effective_mode == "paper":
                # ✅ FIX: account__user se user filter karo
                # PaperTrade mein direct user FK nahi — account→user chain se
                paper_qs = PaperTrade.objects.filter(
                    strategy_id=str(strategy.id),
                    account__user=request.user,   # ✅ user ka hi trade
                )
                if today_start:
                    paper_qs = paper_qs.filter(opened_at__gte=today_start)
                paper_qs = paper_qs.order_by("-opened_at")

                option_qs = OptionTrade.objects.filter(
                    user=request.user,
                    mode="paper",
                    strategy_id=str(strategy.id),
                )
                if today_start:
                    option_qs = option_qs.filter(entry_time__gte=today_start)
                option_qs = option_qs.order_by("-entry_time")

                all_trades = list(paper_qs) + list(option_qs)

            else:
                # ── LIVE MODE: sirf live Orders ke Trade records ──────────
                try:
                    live_qs = Trade.objects.filter(
                        user=request.user,
                        order__strategy_id=strategy.id,
                        order__mode="live",
                    ).select_related("order", "asset")

                    if today_start:
                        live_qs = live_qs.filter(created_at__gte=today_start)

                    live_qs = live_qs.order_by("-created_at")
                    live_trades = list(live_qs)
                except Exception as e:
                    logger.warning(
                        "Error fetching live trades for strategy %s: %s",
                        strategy.id, e,
                    )
                    live_trades = []

                # ── Live OptionTrades bhi include karo ────────────────────
                # ✅ FIX: sirf mode='live' wale — paper option trades exclude
                try:
                    live_opt_qs = OptionTrade.objects.filter(
                        user=request.user,
                        mode="live",
                        strategy_id=str(strategy.id),
                    )
                    if today_start:
                        live_opt_qs = live_opt_qs.filter(
                            entry_time__gte=today_start
                        )
                    live_opt_qs = live_opt_qs.order_by("-entry_time")
                    live_option_trades = list(live_opt_qs)
                except Exception:
                    live_option_trades = []

                all_trades = live_trades + live_option_trades

            all_trades.sort(
                key=lambda t: (
                    self._get_opened_at(t)
                    or self._get_closed_at(t)
                    or datetime.min
                ),
                reverse=True,
            )

            closed_trades = [t for t in all_trades if self._get_status(t) == "closed"]
            open_trades   = [t for t in all_trades if self._get_status(t) == "open"]

            realized_pnl = sum(self._get_pnl(t) for t in closed_trades)

            unrealized_pnl = 0.0
            for t in open_trades:
                current_price = self._get_current_price(t)
                entry_price   = self._get_entry_price(t)
                if (
                    current_price is not None
                    and entry_price is not None
                    and float(entry_price) != 0
                ):
                    diff     = float(current_price) - float(entry_price)
                    mult     = 1.0 if self._get_side(t) in ("long", "buy") else -1.0
                    quantity = self._get_quantity(t)
                    lot_size = self._get_lot_size(t)
                    unrealized_pnl += diff * mult * quantity * lot_size
                else:
                    unrealized_pnl += self._get_pnl(t)

            winning  = sum(1 for t in closed_trades if self._get_pnl(t) > 0)
            losing   = sum(1 for t in closed_trades if self._get_pnl(t) < 0)
            c_count  = len(closed_trades)
            win_rate = (winning / c_count * 100) if c_count > 0 else 0.0

            trade_list = []
            for t in all_trades[:50]:
                symbol_obj = getattr(t, "symbol", None)

                if hasattr(t, "asset") and t.asset is not None:
                    symbol_name = t.asset.symbol
                    lot_size    = 1
                elif hasattr(symbol_obj, "name"):
                    symbol_name = symbol_obj.name
                    lot_size    = getattr(symbol_obj, "lot_size", 1)
                else:
                    symbol_name = str(symbol_obj) if symbol_obj else ""
                    lot_size    = getattr(t, "lot_size", 1)

                contract = getattr(t, "contract", None)
                if contract and hasattr(contract, "fyers_symbol"):
                    raw_display = contract.fyers_symbol
                else:
                    raw_display = (
                        getattr(t, "display_name", symbol_name)
                        or symbol_name
                        or ""
                    )

                clean_display = (
                    str(raw_display)
                    .replace("NSE:", "")
                    .replace("BSE:", "")
                    .replace("DELTA:", "")
                    .replace("-INDEX", "")
                    .strip()
                )

                symbol = (
                    str(symbol_name)
                    .replace("NSE:", "")
                    .replace("BSE:", "")
                    .replace("-INDEX", "")
                    .strip()
                )

                asset_type_raw = getattr(t, "asset_type", "option")
                asset_type     = self._normalize_asset_type(asset_type_raw or "", symbol)

                side         = self._get_side(t)
                stored_pnl   = self._get_pnl(t)
                trade_status = self._get_status(t)

                if trade_status == "open":
                    current_price = self._get_current_price(t)
                    entry_price   = self._get_entry_price(t)
                    if current_price is not None and entry_price is not None:
                        diff     = float(current_price) - float(entry_price)
                        mult     = 1.0 if side in ("long", "buy") else -1.0
                        quantity = self._get_quantity(t)
                        live_pnl = diff * mult * quantity * int(lot_size)
                    else:
                        live_pnl = stored_pnl
                else:
                    live_pnl = stored_pnl

                order_obj = getattr(t, "order", None)
                asset_obj = getattr(t, "asset", None)

                exchange_val = (
                    getattr(asset_obj, "exchange", None)
                    or getattr(order_obj, "exchange", None)
                    or ""
                )

                if not exchange_val:
                    _sym_upper = symbol.upper()
                    _asset_raw = str(getattr(t, "asset_type", "") or "").lower()
                    _is_crypto = (
                        "crypto"  in _asset_raw
                        or _sym_upper.endswith("USDT")
                        or _sym_upper.endswith("-USDT")
                        or _sym_upper.endswith("BTC")
                        or _sym_upper.endswith("ETH")
                        or "-PERP" in _sym_upper
                        or "ETH"  in _sym_upper
                        or "BTC"  in _sym_upper
                    )
                    exchange_val = "DELTA" if _is_crypto else "NSE"

                opt_type = getattr(t, "option_type", None)
                if opt_type:
                    instrument_type_val = "options"
                else:
                    raw_asset_type = getattr(asset_obj, "asset_type", "") or ""
                    if "future" in str(raw_asset_type).lower():
                        instrument_type_val = "futures"
                    elif str(exchange_val).upper() in ("DELTA", "CRYPTO"):
                        instrument_type_val = "spot"
                    else:
                        instrument_type_val = "spot"

                trade_mode = getattr(t, "mode", None) or (
                    getattr(order_obj, "mode", "live") if order_obj else "live"
                )

                trade_list.append(
                    {
                        "id":              str(t.id),
                        "symbol":          symbol,
                        "display_name":    clean_display,
                        "side":            side,
                        "asset_type":      asset_type,
                        "quantity":        float(self._get_quantity(t)),
                        "lot_size":        int(lot_size),
                        "entry_price":     float(self._get_entry_price(t) or 0),
                        "exit_price":      float(self._get_exit_price(t))
                                           if self._get_exit_price(t) is not None else None,
                        "current_price":   float(self._get_current_price(t))
                                           if self._get_current_price(t) is not None else 0.0,
                        "stop_loss":       float(getattr(t, "stop_loss", None))
                                           if getattr(t, "stop_loss", None) is not None else None,
                        "target_price":    float(getattr(t, "target_price", None))
                                           if getattr(t, "target_price", None) is not None else None,
                        "pnl":             live_pnl,
                        "realized_pnl":    stored_pnl,
                        "status":          trade_status,
                        "exit_reason":     getattr(t, "exit_reason", None),
                        "option_type":     opt_type or None,
                        "strike_price":    float(getattr(t, "strike", None))
                                           if getattr(t, "strike", None) is not None else None,
                        "opened_at":       self._get_opened_at(t),
                        "closed_at":       self._get_closed_at(t),
                        "setup_type":      getattr(t, "setup_type", ""),
                        "exchange":        str(exchange_val),
                        "instrument_type": instrument_type_val,
                        "executed_by":     "AUTO",
                        "signal_strength": getattr(t, "signal_strength", None),
                        "indicators":      getattr(t, "indicators", []) or [],
                        "mode":            str(trade_mode),
                    }
                )

            # ✅ FIX: Signals bhi mode ke hisaab se fetch karo
            # Sirf us strategy ke signals jo us mode mein generate hue
            try:
                sig_qs = StrategySignal.objects.filter(
                    strategy=strategy,
                ).order_by("-created_at")
                if today_start:
                    sig_qs = sig_qs.filter(created_at__gte=today_start)
                signal_count     = sig_qs.count()
                executed_signals = sig_qs.filter(result="executed").count()
                skipped_signals  = sig_qs.filter(result="skipped").count()
            except Exception:
                signal_count = executed_signals = skipped_signals = 0

            activity_data.append(
                {
                    "strategy_id":    str(strategy.id),
                    "strategy_name":  strategy.name,
                    "algo_name":      strategy.algo_name or "",
                    "state":          strategy.state,
                    "mode":           strategy.mode,
                    "total_trades":   len(all_trades),
                    "open_trades":    len(open_trades),
                    "closed_trades":  c_count,
                    "winning_trades": winning,
                    "losing_trades":  losing,
                    "win_rate":       round(win_rate, 2),
                    "realized_pnl":   round(realized_pnl, 2),
                    "unrealized_pnl": round(unrealized_pnl, 2),
                    "started_at":     strategy.started_at.isoformat() if getattr(strategy, "started_at", None) else None,
                    "trades":         trade_list,
                    # ✅ Signals count — mode ke hisaab se
                    "total_signals":     signal_count,
                    "executed_signals":  executed_signals,
                    "skipped_signals":   skipped_signals,
                    # ✅ User ka effective mode
                    "effective_mode":    effective_mode,
                    "master_mode":       strategy.mode,
                }
            )

            total_realized   += realized_pnl
            total_unrealized += unrealized_pnl
            total_open       += len(open_trades)
            total_closed     += c_count

        return Response(
            {
                "strategies":       activity_data,
                "total_strategies": len(activity_data),
                "date_filter":      date_filter,
                "summary": {
                    "total_realized_pnl":   round(float(total_realized), 2),
                    "total_unrealized_pnl": round(float(total_unrealized), 2),
                    "total_open_trades":    total_open,
                    "total_closed_trades":  total_closed,
                },
            }
        )


# ─────────────────────────────────────────────────────────────────
#  12. Manual Order
# ─────────────────────────────────────────────────────────────────
class ManualOrderView(StrategyOwnerMixin, APIView):
    """
    Flutter se manual / semi-auto order place karo.

    POST body:
    {
        "signal_type":     "buy" | "sell",
        "symbol":          "NIFTY",         # optional override
        "price":           22500.0,          # optional, market se fallback
        "lots":            2,                # optional, strategy default se fallback
        "instrument_type": "options"         # optional, strategy default se fallback
    }
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, strategy_id):
        from decimal import Decimal
        from apps.market.models import Asset
        from .signal_router import route_and_place_order

        strategy = self.get_strategy(strategy_id)

        signal_type       = request.data.get("signal_type", "buy").lower()
        symbol            = request.data.get("symbol") or strategy.symbol
        price_raw         = request.data.get("price")
        lots_raw          = request.data.get("lots")
        instrument_override = request.data.get("instrument_type")

        if signal_type not in ("buy", "sell"):
            return Response(
                {"error": "signal_type 'buy' ya 'sell' hona chahiye"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Resolve price
        if price_raw:
            try:
                price = float(price_raw)
            except (TypeError, ValueError):
                return Response(
                    {"error": "price valid number hona chahiye"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        else:
            try:
                asset = Asset.objects.filter(
                    symbol__icontains=symbol
                ).order_by("-updated_at").first()
                price = float(asset.last_price) if asset else 0.0
            except Exception:
                price = 0.0

        if price <= 0:
            return Response(
                {"error": f"'{symbol}' ka price fetch nahi hua — price manually bhejo"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Resolve lots
        if lots_raw is not None:
            try:
                lots = int(lots_raw)
                if lots < 1:
                    raise ValueError
            except (TypeError, ValueError):
                return Response(
                    {"error": "lots valid positive integer hona chahiye"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            strategy.risk_config = {**strategy.risk_config, "qty": lots}
        else:
            lots = int(strategy.risk_config.get("qty", 1))

        # Instrument type override
        if instrument_override:
            valid_instruments = [c[0] for c in strategy.InstrumentType.choices]
            if instrument_override not in valid_instruments:
                return Response(
                    {"error": f"instrument_type invalid. Allowed: {valid_instruments}"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            strategy.instrument_type = instrument_override

        # Capital warning
        warning = _check_capital_warning(strategy, price, lots)

        # Build synthetic signal
        signal = StrategySignal(
            strategy=strategy,
            signal_type=signal_type,
            symbol=symbol.upper(),
            price=Decimal(str(price)),
            reason=f"Manual order by user | mode={strategy.mode}",
            metadata={
                "source": "manual",
                "lots": lots,
                "instrument_type": strategy.instrument_type,
            },
        )
        signal.save()

        logger.info(
            "Manual order | strategy=%s | signal=%s | symbol=%s | price=%.2f | lots=%d | instrument=%s | mode=%s",
            strategy.id, signal_type, symbol, price, lots,
            strategy.instrument_type, strategy.mode,
        )

        order = route_and_place_order(strategy, signal)

        if order is None:
            signal.result = "failed"
            signal.save(update_fields=["result"])

            if strategy.mode == "paper":
                fail_reason = (
                    "Paper trade place nahi hua — "
                    "balance check karo (insufficient funds ya risk too high)"
                )
            elif not strategy.broker:
                fail_reason = "Koi broker connected nahi hai — Settings > Broker mein connect karo"
            else:
                broker_name = strategy.broker.broker
                _req_user = getattr(strategy, '_request_user', strategy.user)
                from apps.brokers.models import BrokerAccount
                _acct = BrokerAccount.objects.filter(
                    user=_req_user, broker=broker_name, is_active=True,
                ).first()
                if not _acct:
                    fail_reason = (
                        f"Aapka {broker_name.capitalize()} account connected nahi hai — "
                        f"Settings > Broker mein connect karo."
                    )
                elif not _acct.access_token:
                    fail_reason = (
                        f"{broker_name.capitalize()} token missing — "
                        f"Settings > Broker > {broker_name.capitalize()} mein reconnect karo."
                    )
                elif broker_name == "fyers" and not (
                    getattr(_acct, 'fyers_client_id', '') and
                    (getattr(_acct, 'totp_secret', '') or getattr(_acct, 'fyers_pin', ''))
                ):
                    fail_reason = (
                        "Fyers token expire ho gaya aur auto-refresh nahi ho saka. "
                        "Settings > Broker > Fyers mein jaake Client ID + TOTP/PIN save karo."
                    )
                else:
                    fail_reason = (
                        f"Order place nahi hua — {broker_name} broker error "
                        f"(token expired ya API issue). Logs check karo."
                    )

            return Response(
                {"error": fail_reason, "warning": warning},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        signal.result = "executed"
        if hasattr(order, "id"):
            try:
                from apps.orders.models import Order
                if isinstance(order, Order):
                    signal.order = order
            except Exception:
                pass
        signal.save(update_fields=["result", "order"])

        order_id    = str(getattr(order, "id", ""))
        symbol_used = str(getattr(order, "symbol", signal.symbol))

        return Response(
            {
                "order_id":    order_id,
                "instrument":  strategy.instrument_type,
                "symbol_used": symbol_used,
                "lots":        lots,
                "mode":        strategy.mode,
                "signal_type": signal_type,
                "price":       price,
                "warning":     warning,
            },
            status=status.HTTP_201_CREATED,
        )


# ─────────────────────────────────────────────────────────────────
#  13. Capital Warning
# ─────────────────────────────────────────────────────────────────
class CapitalWarningView(StrategyOwnerMixin, APIView):
    """
    Capital risk check — Flutter se lots enter karne par real-time warn karo.

    GET ?lots=3&price=23547&instrument_type=options
        &option_premium=150        ← actual CE/PE market price (optional)
        &strike_price=23500        ← strike (optional, display only)
        &option_type=CE            ← CE ya PE (optional, display only)

    NOTE:
    - `price`          = underlying index price (NIFTY/BANKNIFTY spot)
    - `option_premium` = actual option LTP (e.g. 150.50).
                         Agar pass nahi kiya toh NSE option chain se nearest ATM
                         strike ka premium fetch karne ki koshish hogi.
                         Dono fail ho jaayein toh estimated fallback use hoga.
    - `strike_price`   = display ke liye — warning mein show hoga
    - `option_type`    = CE / PE — display ke liye
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, strategy_id):
        strategy = self.get_strategy(strategy_id)
        # ✅ FIX: request.user pass karo taaki global strategy pe bhi sahi balance mile
        strategy._request_user = request.user

        try:
            lots             = int(request.query_params.get("lots", 1))
            price            = float(request.query_params.get("price", 0))
            instrument       = request.query_params.get("instrument_type", strategy.instrument_type)
            option_premium_q = request.query_params.get("option_premium")   # actual LTP
            strike_price_q   = request.query_params.get("strike_price")     # display
            option_type_q    = request.query_params.get("option_type", "").upper()  # CE/PE
        except (TypeError, ValueError):
            return Response({"error": "Invalid params"}, status=400)

        from .fyers_utils import LOT_SIZES, _clean_symbol
        DELTA_CONTRACT_SIZE = 1

        symbol      = strategy.symbol.upper()
        base_symbol = _clean_symbol(symbol)
        lot_size    = LOT_SIZES.get(base_symbol, 25) if instrument in ("options", "futures") else DELTA_CONTRACT_SIZE

        # ── Premium resolution (options ke liye) ──────────────────────────
        actual_premium  = None   # real option LTP
        strike_display  = None   # strike price for response
        option_type_out = option_type_q or None
        premium_source  = None   # "user_provided" | "nse_chain" | "estimated"

        if instrument == "options":
            # 1) User ne directly option_premium bheja?
            if option_premium_q:
                try:
                    actual_premium = float(option_premium_q)
                    premium_source = "user_provided"
                except (TypeError, ValueError):
                    pass

            # 2) NSE option chain se fetch karo (agar user ne nahi bheja)
            if actual_premium is None:
                try:
                    from apps.options.nse_fetcher import fetch_nse_option_chain
                    chain_data = fetch_nse_option_chain(
                        symbol=base_symbol,
                        expiry_ts="",
                        user=request.user,
                    )
                    spot   = chain_data.get("spot", price)
                    chain  = chain_data.get("chain", [])

                    # ATM strike dhundho (spot ke sabse kareeb)
                    if chain:
                        atm = min(chain, key=lambda x: abs(x["strike"] - spot))
                        strike_display   = atm["strike"]
                        opt_key          = option_type_q if option_type_q in ("CE", "PE") else "CE"
                        option_type_out  = opt_key
                        actual_premium   = float(atm.get(opt_key, {}).get("ltp", 0) or 0)
                        if actual_premium > 0:
                            premium_source = "nse_chain"
                        else:
                            actual_premium = None
                except Exception:
                    pass  # fallback to estimate neeche

            # 3) Estimated fallback (0.4% of underlying, min ₹10)
            if actual_premium is None or actual_premium <= 0:
                actual_premium = max(price * 0.004, 10.0)
                premium_source = "estimated"

            if strike_price_q and strike_display is None:
                try:
                    strike_display = float(strike_price_q)
                except (TypeError, ValueError):
                    pass

            estimated_cost = actual_premium * lot_size * lots

        elif instrument == "futures":
            estimated_cost = price * lot_size * lots * 0.15
            premium_source = "margin_estimate"
        elif instrument == "perp":
            _contract_value = float(strategy.risk_config.get("contract_value", 0.01))
            _leverage = float(strategy.risk_config.get("leverage", 10))
            _usdt_margin = _contract_value * price * lots / _leverage
            estimated_cost = round(_usdt_margin * 84.0, 2)
            premium_source = "margin_estimate"
        else:
            estimated_cost = price * lots
            premium_source = "spot_price"

        available = _get_available_capital(strategy)

        if available <= 0:
            return Response({
                "lots":             lots,
                "lot_size":         lot_size,
                "estimated_cost":   round(estimated_cost, 2),
                "available":        0,
                "usage_pct":        0,
                "warning":          None,
                "danger":           False,
                "can_trade":        False,
                "rejection_reason": "Balance fetch nahi hua — broker reconnect karo",
                # options extra info
                "option_premium":   round(actual_premium, 2) if actual_premium else None,
                "strike_price":     strike_display,
                "option_type":      option_type_out,
                "premium_source":   premium_source,
            })

        usage_pct        = (estimated_cost / available) * 100
        warning          = None
        danger           = False
        can_trade        = True
        rejection_reason = None

        # Premium source note (agar estimated hai toh user ko bata do)
        premium_note = None
        if premium_source == "estimated":
            premium_note = (
                f"⚠ Actual option price nahi mila — estimated premium ₹{actual_premium:.0f} "
                f"use kiya gaya. Sahi calculation ke liye 'option_premium' param bhejo."
            )
        elif premium_source == "nse_chain":
            strike_info = f" (Strike: {strike_display} {option_type_out})" if strike_display else ""
            premium_note = f"📊 NSE chain se ATM premium{strike_info}: ₹{actual_premium:.2f}"

        if estimated_cost > available:
            can_trade        = False
            rejection_reason = (
                f"Insufficient funds — ₹{estimated_cost:,.0f} chahiye, "
                f"sirf ₹{available:,.0f} available hai"
            )
            danger  = True
            warning = f"🚨 Balance kam hai! ₹{available:,.0f} available, ₹{estimated_cost:,.0f} chahiye"
        elif usage_pct > 80:
            danger  = True
            warning = f"🚨 DANGER: Aap {usage_pct:.0f}% capital use kar rahe ho! Lots kam karo."
        elif usage_pct > 50:
            warning = f"⚠ High risk: {usage_pct:.0f}% capital ek trade mein lag raha hai."

        return Response({
            "lots":             lots,
            "lot_size":         lot_size,
            "estimated_cost":   round(estimated_cost, 2),
            "available":        round(available, 2),
            "usage_pct":        round(usage_pct, 2),
            "warning":          warning,
            "danger":           danger,
            "can_trade":        can_trade,
            "rejection_reason": rejection_reason,
            # ── Options specific fields ─────────────────────────────────
            "option_premium":   round(actual_premium, 2) if actual_premium else None,
            "strike_price":     strike_display,
            "option_type":      option_type_out,
            "premium_source":   premium_source,   # user_provided | nse_chain | estimated
            "premium_note":     premium_note,     # UI pe show kar sakte ho
        })


# ─────────────────────────────────────────────────────────────────
#  14. Admin — Global Strategy Create/List  (staff only)
# ─────────────────────────────────────────────────────────────────
class AdminGlobalStrategyCreateView(APIView):
    """
    Sirf staff/admin use kar sakta hai.
    POST  → nai global strategy banao
    GET   → sab global strategies list karo

    POST /api/strategies/admin/global/
    {
        "name":          "EMA Scalp BTC",
        "algo_name":     "ema_scalp",
        "symbol":        "BTCUSDT",
        "symbols":       ["BTCUSDT"],
        "mode":          "live",
        "allowed_plans": ["basic", "pro", "elite"],   // [] = sab plans
        "interval_seconds": 60
    }
    """

    def get_permissions(self):
        from rest_framework.permissions import IsAdminUser
        return [IsAuthenticated(), IsAdminUser()]

    def post(self, request):
        ser = StrategyWriteSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)

        allowed_plans = request.data.get("allowed_plans", [])
        if not isinstance(allowed_plans, list):
            return Response(
                {"error": "allowed_plans list hona chahiye. e.g. ['basic', 'pro', 'elite']"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        strategy = ser.save(
            user=None,              # Global strategy ka koi specific user nahi
            is_global=True,
            created_by_admin=True,
            allowed_plans=allowed_plans,
        )

        logger.info(
            "Global strategy created | id=%s | admin=%s | plans=%s",
            strategy.id, request.user.id, allowed_plans,
        )

        return Response(
            StrategySerializer(strategy, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )

    def get(self, request):
        """Sab global strategies list karo (admin view)"""
        qs = Strategy.objects.filter(
            is_global=True,
            is_active=True,
        ).select_related("broker").order_by("-created_at")

        return Response({
            "strategies": StrategySerializer(qs, many=True, context={"request": request}).data,
            "count": qs.count(),
        })


# ─────────────────────────────────────────────────────────────────
#  Private helpers (module-level)
# ─────────────────────────────────────────────────────────────────

def _check_capital_warning(strategy, price: float, lots: int) -> str | None:
    """Order place hone se pehle quick capital check. Returns warning ya None."""
    try:
        available = _get_available_capital(strategy)
        if available <= 0:
            return None

        from .fyers_utils import LOT_SIZES, _clean_symbol
        base_symbol = _clean_symbol(strategy.symbol.upper())
        instrument  = strategy.instrument_type

        lot_size = LOT_SIZES.get(base_symbol, 25) if instrument in ("options", "futures") else 1

        if instrument == "options":
            premium = max(price * 0.004, 10.0)
            cost    = premium * lot_size * lots
        elif instrument == "futures":
            cost = price * lot_size * lots * 0.15
        else:
            cost = price * lots

        usage_pct = (cost / available) * 100

        if usage_pct > 80:
            return f"🚨 {usage_pct:.0f}% capital use ho raha hai — bahut zyada risk!"
        if usage_pct > 50:
            return f"⚠ {usage_pct:.0f}% capital ek trade mein — lots kam karne ki salah"
        return None
    except Exception:
        return None


def _get_delta_balance(account) -> float:
    """Delta Exchange India wallet se available USDT balance fetch karo."""
    import hashlib
    import hmac
    import time
    import requests

    try:
        api_key    = account.api_key
        api_secret = account.api_secret
        if not api_key or not api_secret:
            logger.warning("Delta broker: api_key/secret missing — balance=0")
            return 0.0

        method    = "GET"
        path      = "/v2/wallet/balances"
        timestamp = str(int(time.time()))
        body_str  = ""

        signature_data = method + timestamp + path + body_str
        signature = hmac.new(
            key=api_secret.encode(),
            msg=signature_data.encode(),
            digestmod=hashlib.sha256,
        ).hexdigest()

        headers = {
            "api-key":      api_key,
            "timestamp":    timestamp,
            "signature":    signature,
            "Content-Type": "application/json",
            "User-Agent":   "python-rest-client",
        }

        resp = requests.get(
            "https://api.india.delta.exchange" + path,
            headers=headers,
            timeout=10,
        )
        data = resp.json()

        if data.get("success") is False or "result" not in data:
            logger.warning("Delta balance API error: %s", data.get("error", data))
            return 0.0

        for wallet in data.get("result", []):
            if wallet.get("asset_symbol") in ("USDT", "USD"):
                bal = float(wallet.get("available_balance", 0) or 0)
                logger.info("Delta wallet balance: %.4f USDT", bal)
                return bal

        logger.warning("Delta: USDT wallet not found in response")
        return 0.0

    except Exception as exc:
        logger.warning("Delta balance fetch failed: %s", exc)
        return 0.0


def _get_available_capital(strategy) -> float:
    """Strategy ke broker se available capital fetch karo."""
    try:
        if strategy.mode == "paper":
            from apps.paper_trading.models import PaperAccount
            acc = PaperAccount.objects.filter(user=strategy.user).first()
            return float(acc.balance) if acc else 0.0

        from apps.brokers.models import BrokerAccount

        # ✅ FIX: Global strategy ke liye REQUEST user ka broker use karo
        # strategy.broker = admin ka master account hota hai — wrong!
        # Har user ka apna balance hona chahiye
        _user = getattr(strategy, '_request_user', None)

        if _user and _user != strategy.user:
            # Global strategy — request user ka broker lo
            # ✅ FIX: Delta account pehle check karo (api_key based)
            _delta_acc = BrokerAccount.objects.filter(
                user=_user, broker="delta",
                is_active=True, is_verified=True,
            ).exclude(api_key__isnull=True).exclude(api_key="").first()
            if _delta_acc:
                usdt_bal = _get_delta_balance(_delta_acc)
                return round(usdt_bal * 84.0, 2)

            broker_account = (
                BrokerAccount.objects
                .filter(user=_user, is_active=True, is_verified=True)
                .exclude(access_token__isnull=True)
                .exclude(access_token="")
                .exclude(label="Master Account")
                .order_by("-updated_at")
                .first()
            )
            # Fallback: koi bhi active fyers account
            if not broker_account:
                broker_account = (
                    BrokerAccount.objects
                    .filter(user=_user, is_active=True, is_verified=True)
                    .exclude(access_token__isnull=True)
                    .exclude(access_token="")
                    .order_by("-updated_at")
                    .first()
                )
        else:
            # Own strategy — strategy.broker use karo (may still be None)
            broker_account = strategy.broker
            if not broker_account:
                _fallback_user = _user or strategy.user
                # ✅ FIX: Delta account check karo — global strategy mein subscriber ka account
                # strategy.user = creator (Chanchal), subscriber = Satish
                # Isliye sab active Delta accounts check karo
                from django.contrib.auth import get_user_model as _gum
                _User = _gum()
                _all_delta = BrokerAccount.objects.filter(
                    broker="delta", is_active=True, is_verified=True,
                ).exclude(api_key__isnull=True).exclude(api_key="")
                if _all_delta.exists():
                    _delta_acc = _all_delta.first()
                    usdt_bal = _get_delta_balance(_delta_acc)
                    return round(usdt_bal * 84.0, 2)
                broker_account = (
                    BrokerAccount.objects
                    .filter(user=_fallback_user, is_active=True, is_verified=True)
                    .exclude(access_token__isnull=True)
                    .exclude(access_token="")
                    .order_by("-updated_at")
                    .first()
                )

        if not broker_account:
            return 0.0

        broker_slug = broker_account.broker

        if broker_slug == "fyers":
            from fyers_apiv3 import fyersModel
            from django.conf import settings
            _app_id = broker_account.app_id or settings.FYERS_APP_ID
            fyers = fyersModel.FyersModel(
                client_id=_app_id,
                token=broker_account.access_token,
                log_path="",
                is_async=False,
            )
            funds_resp = fyers.funds()
            if funds_resp and funds_resp.get("s") == "ok":
                for item in funds_resp.get("fund_limit", []):
                    # ✅ FIX: "Available Balance" prefer karo
                    if item.get("title") == "Available Balance":
                        val = float(
                            item.get("equityAmount") or
                            item.get("amount") or 0
                        )
                        if val > 0:
                            return val
                # Fallback: Total Balance
                for item in funds_resp.get("fund_limit", []):
                    if item.get("title") == "Total Balance":
                        val = float(item.get("equityAmount") or item.get("amount") or 0)
                        if val > 0:
                            return val
        elif broker_slug == "delta":
            # ✅ FIX: USDT → INR convert karo
            usdt_bal = _get_delta_balance(broker_account)
            inr_rate = 84.0  # approximate USD→INR rate
            return usdt_bal * inr_rate

        # ✅ FIX: broker_slug None ho toh request user ka Delta account check karo
        if broker_account is None or broker_slug is None:
            from apps.brokers.models import BrokerAccount as _BA
            _check_user = _user or strategy.user
            _delta_acc = _BA.objects.filter(
                user=_check_user, broker="delta",
                is_active=True, is_verified=True,
            ).first()
            if _delta_acc:
                usdt_bal = _get_delta_balance(_delta_acc)
                return usdt_bal * 84.0

    except Exception as e:
        logger.warning("Capital fetch failed: %s", e)

    return 0.0