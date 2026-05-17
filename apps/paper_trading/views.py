import logging
from decimal import Decimal,InvalidOperation
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from .models import PaperAccount, PaperTrade, normalize_symbol
from .serializers import PaperAccountSerializer, PaperTradeSerializer
from .services import close_trade, open_trade, reset_account, topup_account


logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# 💰 ACCOUNT VIEWS
# ─────────────────────────────────────────────
class PaperAccountView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        account, _ = PaperAccount.objects.get_or_create(user=request.user)
        return Response(PaperAccountSerializer(account).data)


class ResetAccountView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        capital = request.data.get("capital", 100000)
        account = reset_account(request.user, capital)
        return Response({
            "success": True,
            "account": PaperAccountSerializer(account).data
        })


class TopUpAccountView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            amount = request.data.get("amount")
            if not amount:
                return Response({"success": False, "message": "Amount required"}, status=400)

            account = topup_account(request.user, amount)
            return Response({
                "success": True,
                "balance": float(account.balance),
                "message": "Account topped up"
            })
        except Exception as e:
            logger.error(f"❌ Topup failed: {e}", exc_info=True)
            return Response({"success": False, "message": str(e)}, status=400)


# ─────────────────────────────────────────────
# 🎯 RISK MANAGEMENT SETTINGS
# ─────────────────────────────────────────────
class UpdateRiskSettingsView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        """
        Update risk management settings
        """
        try:
            account, _ = PaperAccount.objects.get_or_create(user=request.user)

            # ❌ Tier-based fields cannot be overridden
            restricted_fields = [
                "risk_per_trade_pct",
                "max_open_trades",
                "daily_loss_limit_pct",
            ]

            for field in restricted_fields:
                if field in request.data:
                    return Response(
                        {
                            "success": False,
                            "message": f"{field} is auto-managed by tier"
                        },
                        status=400
                    )

            # ✅ Validate daily_loss_limit_fixed
            if "daily_loss_limit_fixed" in request.data:
                try:
                    value = Decimal(str(request.data["daily_loss_limit_fixed"]))
                except (InvalidOperation, TypeError):
                    return Response(
                        {
                            "success": False,
                            "message": "Invalid number for daily_loss_limit_fixed"
                        },
                        status=400
                    )

                if value <= 0:
                    return Response(
                        {
                            "success": False,
                            "message": "daily_loss_limit_fixed must be greater than 0"
                        },
                        status=400
                    )

                # Optional upper limit safeguard
                if value > 1_000_000:
                    return Response(
                        {
                            "success": False,
                            "message": "daily_loss_limit_fixed too large"
                        },
                        status=400
                    )

                account.daily_loss_limit_fixed = value

            # ✅ Validate use_percentage_limit safely
            if "use_percentage_limit" in request.data:
                raw_value = request.data["use_percentage_limit"]

                if isinstance(raw_value, bool):
                    account.use_percentage_limit = raw_value

                elif isinstance(raw_value, str):
                    if raw_value.lower() in ["true", "1"]:
                        account.use_percentage_limit = True
                    elif raw_value.lower() in ["false", "0"]:
                        account.use_percentage_limit = False
                    else:
                        return Response(
                            {
                                "success": False,
                                "message": "Invalid string for use_percentage_limit (use true/false)"
                            },
                            status=400
                        )
                else:
                    return Response(
                        {
                            "success": False,
                            "message": "Invalid value for use_percentage_limit"
                        },
                        status=400
                    )

            account.save()

            return Response({
                "success": True,
                "settings": {
                    "daily_loss_limit_pct": float(account.daily_loss_limit_pct),
                    "daily_loss_limit_fixed": (
                        float(account.daily_loss_limit_fixed)
                        if account.daily_loss_limit_fixed else None
                    ),
                    "use_percentage_limit": account.use_percentage_limit,
                    "daily_loss_limit_amount": float(account.daily_loss_limit_amount),
                }
            })

        except Exception as e:
            logger.error(f"❌ Risk settings update failed: {e}", exc_info=True)
            return Response(
                {"success": False, "message": "Something went wrong"},
                status=500
            )

class GetRiskStatusView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            account, _ = PaperAccount.objects.get_or_create(user=request.user)

            open_count = account.trades.filter(status="open").count()
            can_trade, reason = account.can_open_new_trade(asset_type="crypto")

            total = account.balance + account.margin_used

            return Response({
                "can_trade": can_trade,
                "reason": reason,
                "daily_pnl": float(account.todays_realized_pnl),
                "daily_loss_limit_pct": float(account.daily_loss_limit_pct),
                "daily_loss_limit_amount": float(account.daily_loss_limit_amount),
                "use_percentage_limit": account.use_percentage_limit,
                "daily_limit_hit": account.is_daily_loss_limit_hit,
                "open_positions": open_count,
                "max_positions": account.max_open_trades,
                "available_balance": float(account.available_balance),
                "margin_used": float(account.margin_used),
                "margin_used_pct": float(account.margin_used / total * 100) if total > 0 else 0,
            })
        except Exception as e:
            logger.error(f"❌ Get risk status failed: {e}", exc_info=True)
            return Response({"error": str(e)}, status=500)


# ─────────────────────────────────────────────
# 📊 TRADE VIEWS
# ─────────────────────────────────────────────
class OpenTradeView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            if "entry_price" not in request.data:
                return Response({"success": False, "message": "entry_price required"}, status=400)

            trade = open_trade(request.user, request.data)
            return Response({
                "success": True,
                "trade": PaperTradeSerializer(trade).data,
                "message": "Trade opened"
            })
        except Exception as e:
            logger.error(f"❌ Open trade failed: {e}", exc_info=True)
            return Response(
                {"success": False, "message": str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )


class CloseTradeView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, trade_id):
        try:
            try:
                trade_obj = PaperTrade.objects.get(
                    id=trade_id,
                    account__user=request.user,
                )
            except PaperTrade.DoesNotExist:
                return Response(
                    {"success": False, "message": "Trade not found"},
                    status=404
                )

            if trade_obj.status == "closed":
                return Response({
                    "success": True,
                    "pnl": float(trade_obj.pnl or 0),
                    "message": "Already closed"
                })

            exit_price = request.data.get("exit_price")
            if exit_price is None:
                exit_price = trade_obj.current_price or trade_obj.entry_price

            exit_price = Decimal(str(exit_price))
            trade = close_trade(trade_id=trade_id, exit_price=exit_price, reason="manual")

            return Response({
                "success": True,
                "trade": PaperTradeSerializer(trade).data,
                "pnl": float(trade.pnl or 0),
                "message": "Trade closed",
            })
        except Exception as e:
            logger.error(f"❌ Close trade failed [{trade_id}]: {e}", exc_info=True)
            return Response({"success": False, "message": str(e)}, status=400)


class CloseAllTradesView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            trades = list(PaperTrade.objects.filter(
                account__user=request.user, status="open"
            ))
            if not trades:
                return Response({
                    "closed": 0,
                    "total_pnl": 0.0,
                    "message": "No open trades"
                })

            closed_count = 0
            total_pnl = 0.0
            errors = []

            for trade in trades:
                try:
                    ep = trade.current_price or trade.entry_price
                    closed = close_trade(
                        trade_id=trade.id,
                        exit_price=Decimal(str(ep)),
                        reason="manual",
                    )
                    total_pnl += float(closed.pnl or 0)
                    closed_count += 1
                except Exception as e:
                    logger.error(f"❌ Close all error [{trade.id}]: {e}", exc_info=True)
                    errors.append(str(trade.id))

            return Response({
                "closed": closed_count,
                "total_pnl": round(total_pnl, 2),
                "errors": errors,
            })
        except Exception as e:
            logger.error(f"❌ Close all trades failed: {e}", exc_info=True)
            return Response({"error": str(e)}, status=500)


class UpdateTradePriceView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            raw_symbol = request.data.get("symbol", "")
            current_price = request.data.get("current_price")

            if not raw_symbol or current_price is None:
                return Response(
                    {"error": "symbol and current_price required"},
                    status=400
                )

            symbol = normalize_symbol(raw_symbol)
            current_price = Decimal(str(current_price))

            updated = PaperTrade.objects.filter(
                account__user=request.user,
                symbol=symbol,
                status="open",
            ).update(current_price=current_price)

            return Response({
                "success": True,
                "symbol": symbol,
                "updated": updated
            })

        except Exception as e:
            logger.error(f"❌ Price update failed: {e}", exc_info=True)
            return Response({"error": str(e)}, status=500)


class TradeListView(APIView): 
    permission_classes = [IsAuthenticated]

    def get(self, request):
        account, _ = PaperAccount.objects.get_or_create(user=request.user)
        trade_status = request.query_params.get("status", "all")
        trades = account.trades.all().order_by("-opened_at")

        if trade_status == "open":
            trades = trades.filter(status="open")
        elif trade_status == "closed":
            trades = trades.filter(status="closed")

        strategy_id = request.query_params.get("strategy_id")
        if strategy_id:
            trades = trades.filter(strategy_id=strategy_id)

        # ✅ पहले total count लो
        total_count = trades.count()

        # फिर limit apply करो
        limit = int(request.query_params.get("limit", 100))
        trades = trades[:limit]

        return Response({
            "trades": PaperTradeSerializer(trades, many=True).data,
            "count": total_count,
            "status_filter": trade_status,
        })

# ─────────────────────────────────────────────
# 🏆 TIER INFO
# ─────────────────────────────────────────────
class GetTierInfoView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        account, _ = PaperAccount.objects.get_or_create(user=request.user)

        total = account.balance + account.margin_used

        return Response({
            "capital": float(account.initial_capital),
            "tier": account.risk_tier,
            "tier_name": {
                "tier_1": "Retail Trader (≤ ₹5L)",
                "tier_2": "Serious Trader (₹5L - ₹25L)",
                "tier_3": "Semi-Pro (₹25L - ₹1Cr)",
                "tier_4": "Professional (> ₹1Cr)",
            }.get(account.risk_tier),

            "limits": {
                "daily_loss_pct": float(account.daily_loss_limit_pct),
                "daily_loss_amount": float(account.daily_loss_limit_amount),
                "risk_per_trade_pct": float(account.risk_per_trade_pct),
                "max_open_trades": account.max_open_trades,

                "max_crypto_positions": account.max_crypto_positions,
                "max_leverage_crypto": account.max_leverage_crypto,
                "min_margin_buffer_pct": float(account.min_margin_buffer_pct),

                "max_position_size_crypto": float(account.get_max_position_size("crypto")),
                "max_position_size_options": float(account.get_max_position_size("option")),
                "max_position_size_futures": float(account.get_max_position_size("futures")),
            },

            "current_usage": {
                "open_trades": account.trades.filter(status="open").count(),
                "crypto_positions": account.current_crypto_positions,
                "margin_used": float(account.margin_used),
                "margin_used_pct": float(account.margin_used / total * 100) if total > 0 else 0,
            }
        })