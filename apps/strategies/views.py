# apps/strategies/views.py

import logging

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
#  Mixin — always scoped to request.user
# ─────────────────────────────────────────────────────────────────
class StrategyOwnerMixin:
    def get_strategy(self, strategy_id) -> Strategy:
        return get_object_or_404(
            Strategy,
            pk=strategy_id,
            user=self.request.user,
            is_active=True,
        )


# ─────────────────────────────────────────────────────────────────
#  1. List + Create
# ─────────────────────────────────────────────────────────────────
class StrategyListCreateView(StrategyOwnerMixin, APIView):
    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAuthenticated(), CanAddStrategy()]
        return [IsAuthenticated()]

    def get(self, request):
        qs = Strategy.objects.filter(
            user=request.user,
            is_active=True,
        ).select_related("broker")

        if state := request.query_params.get("state"):
            qs = qs.filter(state=state)
        if mode := request.query_params.get("mode"):
            qs = qs.filter(mode=mode)

        return Response({"strategies": StrategySerializer(qs, many=True).data})

    def post(self, request):
        ser = StrategyWriteSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)

        if ser.validated_data.get("mode") == Strategy.Mode.LIVE:
            perm = CanLiveTrade()
            if not perm.has_permission(request, self):
                return Response(
                    {"error": perm.message}, status=status.HTTP_403_FORBIDDEN
                )

        strategy = ser.save(user=request.user)
        logger.info("Strategy created | id=%s | user=%s", strategy.id, request.user.id)

        return Response(
            StrategySerializer(strategy).data,
            status=status.HTTP_201_CREATED,
        )


# ─────────────────────────────────────────────────────────────────
#  2. Retrieve + Update + Soft-delete
# ─────────────────────────────────────────────────────────────────
class StrategyDetailView(StrategyOwnerMixin, APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, strategy_id):
        strategy = self.get_strategy(strategy_id)
        return Response(StrategySerializer(strategy).data)

    def patch(self, request, strategy_id):
        strategy = self.get_strategy(strategy_id)

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
        return Response(StrategySerializer(updated).data)

    def delete(self, request, strategy_id):
        strategy = self.get_strategy(strategy_id)

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
                "strategy": StrategySerializer(updated).data,
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
                "strategy": StrategySerializer(updated).data,
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
                "strategy": StrategySerializer(updated).data,
            }
        )


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
        """
        PaperTrade/OptionTrade → apna status/is_open hota hai
        Trade (live fill)      → parent Order ka status use karo
        """
        # PaperTrade / OptionTrade — is_open property hoti hai
        if hasattr(trade, "is_open") and not callable(getattr(type(trade), "is_open", None)):
            return "open" if trade.is_open else "closed"

        # PaperTrade ka apna status field hota hai
        s = getattr(trade, "status", None) or getattr(trade, "state", None)
        if s is not None:
            val = str(s).lower()
            if val in ("open", "active", "running"):
                return "open"
            if val in ("closed", "complete", "completed", "filled", "cancelled", "expired"):
                return "closed"
            return val

        # Live Trade (fill record) — parent Order se derive karo
        order = getattr(trade, "order", None)
        if order is not None:
            order_status = str(getattr(order, "status", "")).lower()
            if order_status in ("open", "active"):
                return "open"
            return "closed"

        return "unknown"

    def _get_pnl(self, trade):
        """Return numeric PnL — checks pnl then realized_pnl (Trade model)."""
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
        """
        PaperTrade/OptionTrade → entry_price
        Trade (live)           → price (fill price)
        """
        return (
            getattr(trade, "entry_price", None)
            or getattr(trade, "entry_rate", None)
            or getattr(trade, "entry", None)
            or getattr(trade, "price", None)    # ← Trade model fill price
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
        """
        PaperTrade  → opened_at
        OptionTrade → entry_time
        Trade       → created_at
        """
        return (
            getattr(trade, "opened_at", None)
            or getattr(trade, "entry_time", None)
            or getattr(trade, "created_at", None)   # ← Trade model
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

        today_start = None
        if date_filter == "today":
            today_start = timezone.now().replace(
                hour=0, minute=0, second=0, microsecond=0
            )

        strategies = Strategy.objects.filter(
            user=request.user
        ).order_by("-created_at")

        activity_data = []
        total_realized = 0.0
        total_unrealized = 0.0
        total_open = 0
        total_closed = 0

        for strategy in strategies:
            all_trades = []

            if strategy.mode.lower() == "paper":
                # ── Paper trades ──────────────────────────────────
                paper_qs = PaperTrade.objects.filter(strategy_id=strategy.id)
                if today_start:
                    paper_qs = paper_qs.filter(opened_at__gte=today_start)
                paper_qs = paper_qs.order_by("-opened_at")

                # ── Option trades (paper mode) ────────────────────
                option_qs = OptionTrade.objects.filter(
                    user=request.user,
                    mode="paper",
                    strategy_id=strategy.id,
                )
                if today_start:
                    option_qs = option_qs.filter(entry_time__gte=today_start)
                option_qs = option_qs.order_by("-entry_time")

                all_trades = list(paper_qs) + list(option_qs)

            else:
                # ── Live trades — Order FK ke through filter karo ─
                # Trade model mein strategy_id nahi hai
                # Trade → Order → Strategy chain use karo
                try:
                    live_qs = Trade.objects.filter(
                        user=request.user,
                        order__strategy_id=strategy.id,   # ✅ FIX
                    ).select_related("order", "asset")

                    if today_start:
                        live_qs = live_qs.filter(
                            created_at__gte=today_start   # ✅ Trade model mein created_at hai
                        )

                    live_qs = live_qs.order_by("-created_at")
                    all_trades = list(live_qs)

                except Exception as e:
                    logger.warning(
                        "Error fetching live trades for strategy %s: %s",
                        strategy.id, e,
                    )
                    all_trades = []

            # ── Sort all trades by time ───────────────────────────
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

            # Unrealized PnL — open trades
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

            # ── Build trade list (max 50) ─────────────────────────
            trade_list = []
            for t in all_trades[:50]:
                symbol_obj = getattr(t, "symbol", None)

                # Live Trade — symbol comes from asset FK
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

                side       = self._get_side(t)
                stored_pnl = self._get_pnl(t)
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

                # ── Live Trade extra fields ───────────────────────────
                order_obj  = getattr(t, "order", None)
                asset_obj  = getattr(t, "asset", None)

                # ✅ FIX: exchange field PaperTrade mein nahi hota
                # Symbol aur asset_type se derive karo taaki crypto sahi detect ho
                exchange_val = (
                    getattr(asset_obj, "exchange", None)
                    or getattr(order_obj, "exchange", None)
                    or ""
                )

                # Exchange empty hai → symbol/asset_type se detect karo
                if not exchange_val:
                    _sym_upper  = symbol.upper()
                    _asset_raw  = str(getattr(t, "asset_type", "") or "").lower()
                    _is_crypto  = (
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
            # ✅ FIX: activity_data mein append karo (yeh missing tha!)
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