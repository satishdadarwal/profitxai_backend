# apps/options/views.py
# ─────────────────────────────────────────────────────────────────────────────
#  COMPLETE OPTION TRADING VIEWS
#  ✅ Live Trading - Real broker integration (Fyers/Zerodha)
#  ✅ Paper Trading - Risk-free practice trading
#  ✅ Option Chain - Live NSE/Fyers option chain data
#  ✅ Backtest - Historical strategy testing
#  ✅ Position Management - Open/Close trades, PnL tracking
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import logging
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.options.models import (
    BacktestRun,
    OptionContract,
    OptionSnapshot,
    OptionSymbol,
)
from apps.paper_trading.models import PaperAccount
from apps.orders.models import Order
from apps.market.models import Asset
from apps.brokers.models import BrokerAccount, BrokerOrder
from broker_adapters.factory import BrokerAdapterFactory

from .serializers import BacktestRunSerializer
from .services import (
    build_option_chain,
    close_trade,
    format_fyers_symbol,
    get_atm_contract,
    get_or_create_option_symbol,
    nearest_expiry,
    place_live_option_trade,
)
from .tasks import run_backtest_task, place_broker_order
from .nse_fetcher import fetch_nse_option_chain

logger = logging.getLogger(__name__)


def _order_success(resp) -> bool:
    """FyersAdapter returns OrderResult dataclass; fallback dict ke liye bhi kaam karta hai."""
    if hasattr(resp, "success"):
        return bool(resp.success)
    return bool(resp.get("success")) if isinstance(resp, dict) else False


def _order_id(resp) -> str | None:
    if hasattr(resp, "order_id"):
        return resp.order_id
    return resp.get("order_id") if isinstance(resp, dict) else None


def _order_message(resp) -> str:
    if hasattr(resp, "message"):
        return resp.message or ""
    return resp.get("message", "Unknown error") if isinstance(resp, dict) else "Unknown error"


# ─────────────────────────────────────────────────────────────────────────────
#  OPTION CHAIN - Live Market Data
# ─────────────────────────────────────────────────────────────────────────────

class OptionChainView(APIView):
    """
    GET /api/options/option-chain/?symbol=NIFTY&expiry=2025-05-29
    
    Returns live option chain from NSE/Fyers with:
    - Current spot price
    - Available expiries
    - Complete CE/PE chain with LTP, OI, IV, Greeks
    - ATM strike marking
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        symbol = request.query_params.get("symbol", "NIFTY").upper()
        expiry = request.query_params.get("expiry", None)

        try:
            data = fetch_nse_option_chain(symbol, user=request.user)
        except Exception as e:
            logger.error("OptionChainView: fetch failed | symbol=%s | %s", symbol, e)
            return Response({
                "error": f"Failed to fetch option chain: {str(e)}"
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # Filter by specific expiry if requested
        if expiry:
            try:
                entry = next(
                    (e for e in data.get("raw_expiry_data", []) if e["date"] == expiry),
                    None,
                )
                if entry:
                    data = fetch_nse_option_chain(
                        symbol, 
                        expiry_ts=entry["expiry"], 
                        user=request.user
                    )
            except Exception as e:
                logger.warning("Expiry filter failed: %s", e)

        chain = data.get("chain", [])
        spot = data.get("spot", 0)

        # Calculate ATM strike
        strike_step = _get_strike_step(symbol)
        atm_strike = round(spot / strike_step) * strike_step if spot else 0

        # Format chain data with moneyness indicator
        formatted_chain = [
            {
                "strike": row.get("strike"),
                "expiry": row.get("expiry"),
                "is_atm": row.get("strike") == atm_strike,
                "is_itm_call": row.get("strike") < spot if spot else False,
                "is_itm_put": row.get("strike") > spot if spot else False,
                
                # Call Option Data
                "call_ltp": row.get("CE", {}).get("ltp", 0),
                "call_oi": row.get("CE", {}).get("oi", 0),
                "call_volume": row.get("CE", {}).get("volume", 0),
                "call_iv": row.get("CE", {}).get("iv", 0),
                "call_bid": row.get("CE", {}).get("bid", 0),
                "call_ask": row.get("CE", {}).get("ask", 0),
                "call_change": row.get("CE", {}).get("ltpch", 0),
                "call_change_percent": row.get("CE", {}).get("ltpchp", 0),
                "call_oich": row.get("CE", {}).get("oich", 0),
                
                # Put Option Data
                "put_ltp": row.get("PE", {}).get("ltp", 0),
                "put_oi": row.get("PE", {}).get("oi", 0),
                "put_volume": row.get("PE", {}).get("volume", 0),
                "put_iv": row.get("PE", {}).get("iv", 0),
                "put_bid": row.get("PE", {}).get("bid", 0),
                "put_ask": row.get("PE", {}).get("ask", 0),
                "put_change": row.get("PE", {}).get("ltpch", 0),
                "put_change_percent": row.get("PE", {}).get("ltpchp", 0),
                "put_oich": row.get("PE", {}).get("oich", 0),
            }
            for row in chain
        ]

        return Response({
            "success": True,
            "symbol": symbol,
            "spot": spot,
            "atm_strike": atm_strike,
            "strike_step": strike_step,
            "expiries": data.get("expiries", []),
            "chain": formatted_chain,
            "total_strikes": len(formatted_chain),
        })


def _get_strike_step(symbol_name: str) -> int:
    """Get strike price interval for different symbols"""
    STRIKE_STEPS = {
        "NIFTY": 50,
        "BANKNIFTY": 100,
        "FINNIFTY": 50,
        "MIDCPNIFTY": 25,
        "SENSEX": 100,
        "BANKEX": 100,
    }
    return STRIKE_STEPS.get(symbol_name.upper(), 50)


# ─────────────────────────────────────────────────────────────────────────────
#  LIVE OPTION TRADING - Real Broker Orders
# ─────────────────────────────────────────────────────────────────────────────

class LiveOptionTradeView(APIView):
    """
    POST /api/options/live-trade/
    GET  /api/options/live-trade/
    
    Place and manage live option trades via connected broker (Fyers/Zerodha)
    """
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        """List open and recent live trades"""
        status_filter = request.query_params.get("status", "open")
        limit = min(int(request.query_params.get("limit", 20)), 100)

        qs = Order.objects.filter(
            user=request.user,
            mode="live",
            instrument_type="options",
        ).select_related("asset").order_by("-entry_time", "-created_at")

        if status_filter != "all":
            qs = qs.filter(status=status_filter)

        trades_data = []
        for order in qs[:limit]:
            ep = float(order.entry_price or 0)
            cp = float(order.current_price or order.entry_price or 0)
            qty = float(order.quantity or 0)
            side = order.side or "buy"

            if side == "buy":
                unrealized_pnl = (cp - ep) * qty
            else:
                unrealized_pnl = (ep - cp) * qty

            trades_data.append({
                "id": str(order.id),
                "symbol": order.symbol_display or (order.asset.symbol if order.asset else ""),
                "option_type": order.option_type,
                "trade_type": side,
                "quantity": qty,
                "lots": order.lots,
                "entry_price": ep,
                "current_price": cp,
                "unrealized_pnl": round(unrealized_pnl, 2),
                "stop_loss": float(order.sl_price) if order.sl_price else None,
                "target_price": float(order.target_price) if order.target_price else None,
                "entry_time": order.entry_time.isoformat() if order.entry_time else order.created_at.isoformat(),
                "exit_time": order.exit_time.isoformat() if order.exit_time else None,
                "exit_price": float(order.exit_price) if order.exit_price else None,
                "realized_pnl": float(order.realized_pnl) if order.realized_pnl else None,
                "status": order.status,
                "broker_order_id": _get_broker_order_id_from_order(order),
            })

        return Response({
            "success": True,
            "trades": trades_data,
            "count": len(trades_data),
        })
    
    def post(self, request):
        """
        Place live option trade via broker
        
        Request Body:
        {
            "symbol": "NIFTY",              // Base symbol (required)
            "expiry": "2025-05-29",         // Expiry date (optional, defaults to nearest)
            "strike": 24500,                // Strike price (optional, defaults to ATM)
            "option_type": "CE",            // CE or PE (required)
            "trade_type": "buy",            // buy or sell (required)
            "action": "buy",                // Alternative to trade_type
            "quantity": 50,                 // Quantity (required if no lots)
            "lots": 1,                      // Number of lots (required if no quantity)
            "entry_price": 150.50,          // Entry price (optional, market if not provided)
            "stop_loss": 140.00,            // Stop loss price (required)
            "take_profit": 180.00,          // Take profit price (optional)
            "target_price": 180.00,         // Alternative to take_profit
            "spot": 24500.0,                // Current spot price (required)
            "use_trailing_sl": false,       // Enable trailing stop loss (optional)
            "trailing_sl_percent": 2.0,     // Trailing SL percentage (optional)
            "setup_type": "ICT_OrderBlock", // Trading setup type (optional)
            "monthly": false                // Use monthly expiry (optional)
        }
        """
        try:
            # ── Extract and validate request data ─────────────────────────────
            data = request.data
            
            symbol_name = data.get("symbol")
            option_type = data.get("option_type", "").upper()
            trade_type = data.get("trade_type") or data.get("action", "").lower()
            spot = data.get("spot")
            stop_loss = data.get("stop_loss")
            
            # Validate required fields
            if not all([symbol_name, option_type, trade_type, spot, stop_loss]):
                return Response({
                    "error": "Missing required fields: symbol, option_type, trade_type/action, spot, stop_loss"
                }, status=status.HTTP_400_BAD_REQUEST)
            
            if option_type not in ["CE", "PE"]:
                return Response({
                    "error": "option_type must be 'CE' or 'PE'"
                }, status=status.HTTP_400_BAD_REQUEST)
            
            if trade_type not in ["buy", "sell"]:
                return Response({
                    "error": "trade_type/action must be 'buy' or 'sell'"
                }, status=status.HTTP_400_BAD_REQUEST)
            
            # Extract numeric values
            try:
                spot = float(spot)
                stop_loss = float(stop_loss)
                
                # Quantity or lots (must provide one)
                if "quantity" in data:
                    quantity = int(data["quantity"])
                    lots = None
                elif "lots" in data:
                    lots = int(data["lots"])
                    quantity = None
                else:
                    return Response({
                        "error": "Either 'quantity' or 'lots' is required"
                    }, status=status.HTTP_400_BAD_REQUEST)
                
                entry_price = float(data["entry_price"]) if data.get("entry_price") else None
                target_price = float(data.get("target_price") or data.get("take_profit", 0)) or None
                strike = float(data["strike"]) if data.get("strike") else None
                trailing_sl_percent = float(data.get("trailing_sl_percent", 0)) if data.get("use_trailing_sl") else None
                
            except (ValueError, TypeError) as e:
                return Response({
                    "error": f"Invalid numeric value: {str(e)}"
                }, status=status.HTTP_400_BAD_REQUEST)
            
            # Validate numeric ranges
            if spot <= 0:
                return Response({"error": "spot must be > 0"}, status=status.HTTP_400_BAD_REQUEST)
            
            if quantity and quantity <= 0:
                return Response({"error": "quantity must be > 0"}, status=status.HTTP_400_BAD_REQUEST)
            
            if lots and lots <= 0:
                return Response({"error": "lots must be > 0"}, status=status.HTTP_400_BAD_REQUEST)
            
            if entry_price and entry_price <= 0:
                return Response({"error": "entry_price must be > 0"}, status=status.HTTP_400_BAD_REQUEST)
            
            # Validate SL/TP logic for premium-based options
            if entry_price:
                if trade_type == "buy":
                    if stop_loss >= entry_price:
                        return Response({
                            "error": "stop_loss must be below entry_price for BUY trades"
                        }, status=status.HTTP_400_BAD_REQUEST)
                    if target_price and target_price <= entry_price:
                        return Response({
                            "error": "target_price must be above entry_price for BUY trades"
                        }, status=status.HTTP_400_BAD_REQUEST)
                else:  # sell
                    if stop_loss <= entry_price:
                        return Response({
                            "error": "stop_loss must be above entry_price for SELL trades"
                        }, status=status.HTTP_400_BAD_REQUEST)
                    if target_price and target_price >= entry_price:
                        return Response({
                            "error": "target_price must be below entry_price for SELL trades"
                        }, status=status.HTTP_400_BAD_REQUEST)
            
            # ── Get or create symbol and contract ────────────────────────────
            try:
                symbol = OptionSymbol.objects.get(name=symbol_name.upper())
            except OptionSymbol.DoesNotExist:
                symbol = get_or_create_option_symbol(symbol_name.upper())
            
            # Calculate quantity if lots provided
            if lots and not quantity:
                quantity = lots * symbol.lot_size
            
            # Determine expiry
            expiry_date = None
            if data.get("expiry"):
                from datetime import date as _date
                try:
                    expiry_date = _date.fromisoformat(str(data["expiry"]))
                except ValueError:
                    return Response({
                        "error": "expiry must be in YYYY-MM-DD format"
                    }, status=status.HTTP_400_BAD_REQUEST)
            else:
                expiry_date = nearest_expiry(monthly=bool(data.get("monthly", False)))
            
            # Determine strike (ATM if not provided)
            if not strike:
                strike_step = _get_strike_step(symbol_name)
                strike = round(spot / strike_step) * strike_step
            
            # Get or create option contract
            fyers_symbol = format_fyers_symbol(symbol_name, expiry_date, int(strike), option_type)
            option_contract, _ = OptionContract.objects.get_or_create(
                symbol=symbol,
                strike=Decimal(str(strike)),
                option_type=option_type,
                expiry=expiry_date,
                defaults={"fyers_symbol": fyers_symbol}
            )
            
            # If entry_price not provided, use contract LTP
            if not entry_price:
                entry_price = float(option_contract.ltp or 0)
                if entry_price <= 0:
                    return Response({
                        "error": "Cannot determine entry price - LTP not available. Please provide entry_price."
                    }, status=status.HTTP_400_BAD_REQUEST)
            
            # ── Get broker adapter ────────────────────────────────────────────
            try:
                adapter = BrokerAdapterFactory.get_adapter(request.user)
            except Exception as e:
                logger.error("Broker adapter failed for user %s: %s", request.user.id, e)
                return Response({
                    "error": f"Broker connection failed: {str(e)}"
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
            
            # ── Place order via broker ────────────────────────────────────────
            order_type = "market" if data.get("entry_price") is None else "limit"
            limit_price = entry_price if order_type == "limit" else None
            
            with transaction.atomic():
                # Place the order
                order_response = adapter.place_order(
                    symbol=fyers_symbol,
                    side=trade_type,
                    qty=quantity,
                    order_type=order_type,
                    price=limit_price or 0,
                    product_type="INTRADAY",
                )
                
                if not _order_success(order_response):
                    logger.error("Broker order failed: %s", _order_message(order_response))
                    return Response({
                        "error": f"Order placement failed: {_order_message(order_response)}"
                    }, status=status.HTTP_400_BAD_REQUEST)
                
                # Get or create Asset
                symbol_str = f"{symbol_name}{int(strike)}{option_type}"
                asset, _ = Asset.objects.get_or_create(
                    symbol=symbol_str,
                    defaults={
                        "name": symbol_str,
                        "asset_type": "options",
                        "exchange": "NSE",
                        "currency": "INR",
                        "is_active": True,
                    },
                )

                # Create Order record (canonical)
                order_obj = Order.objects.create(
                    user=request.user,
                    asset=asset,
                    mode=Order.Mode.LIVE,
                    side=trade_type,
                    order_type=Order.OrderType.MARKET,
                    status=Order.Status.OPEN,
                    quantity=Decimal(str(quantity)),
                    filled_qty=Decimal(str(quantity)),
                    entry_price=Decimal(str(entry_price)),
                    current_price=Decimal(str(entry_price)),
                    sl_price=Decimal(str(stop_loss)) if stop_loss else None,
                    target_price=Decimal(str(target_price)) if target_price else None,
                    entry_time=timezone.now(),
                    option_type=option_type,
                    lots=lots or (quantity // symbol.lot_size),
                    position_size=quantity,
                    symbol_display=symbol_str,
                    instrument_type="options",
                    notes=data.get("setup_type", "Manual"),
                )

                # Create BrokerOrder record
                broker_order = BrokerOrder.objects.create(
                    broker_account=getattr(adapter, 'broker_account', None),
                    order=order_obj,
                    broker_order_id=_order_id(order_response),
                    symbol=fyers_symbol,
                    quantity=quantity,
                    side=trade_type,
                    order_type=BrokerOrder.OrderType.ENTRY,
                    price=limit_price,
                    status=BrokerOrder.Status.PLACED,
                    notes=f"option_type={option_type} | strike={strike} | expiry={expiry_date.isoformat()} | spot={spot}",
                )

                logger.info(
                    "Live trade placed: user=%s, symbol=%s, strike=%s, type=%s, qty=%s, order_id=%s",
                    request.user.id, symbol_name, strike, option_type, quantity, broker_order.broker_order_id
                )

                return Response({
                    "success": True,
                    "trade_id": str(order_obj.id),
                    "broker_order_id": broker_order.broker_order_id,
                    "symbol": fyers_symbol,
                    "strike": float(strike),
                    "option_type": option_type,
                    "quantity": quantity,
                    "lots": order_obj.lots,
                    "entry_price": float(entry_price),
                    "stop_loss": float(stop_loss),
                    "target_price": float(target_price) if target_price else None,
                    "message": "Live option trade placed successfully"
                }, status=status.HTTP_201_CREATED)
        
        except Exception as e:
            logger.exception("Unexpected error in LiveOptionTradeView.post: %s", e)
            return Response({
                "error": f"Unexpected error: {str(e)}"
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class LiveOptionTradeCloseView(APIView):
    """
    POST /api/options/live-trade/<trade_id>/close/
    
    Manually close an open live trade by placing reverse order
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, trade_id):
        """
        Close a live trade

        Body: { "exit_price": 150.0 }  (optional - uses current_price if not provided)
        """
        try:
            order = Order.objects.get(
                id=trade_id, user=request.user, mode="live", status="open"
            )
        except Order.DoesNotExist:
            return Response({
                "error": "Live trade not found or already closed"
            }, status=status.HTTP_404_NOT_FOUND)

        exit_price = request.data.get("exit_price")

        if exit_price is None:
            exit_price = float(order.current_price or order.entry_price or 0)
        else:
            try:
                exit_price = float(exit_price)
            except (ValueError, TypeError):
                return Response({
                    "error": "exit_price must be a valid number"
                }, status=status.HTTP_400_BAD_REQUEST)

        if exit_price <= 0:
            return Response({
                "error": "exit_price must be > 0"
            }, status=status.HTTP_400_BAD_REQUEST)

        # Retrieve fyers_symbol from the contract if symbol_display matches
        fyers_sym = order.symbol_display or (order.asset.symbol if order.asset else "")

        try:
            adapter = BrokerAdapterFactory.get_adapter(request.user)
            exit_side = "sell" if order.side == "buy" else "buy"

            with transaction.atomic():
                exit_response = adapter.place_order(
                    symbol=fyers_sym,
                    side=exit_side,
                    qty=int(order.quantity),
                    order_type="market",
                    price=0,
                    product_type="INTRADAY",
                )

                if not _order_success(exit_response):
                    logger.error("Exit order failed: %s", _order_message(exit_response))
                    return Response({
                        "error": f"Exit order failed: {_order_message(exit_response)}"
                    }, status=status.HTTP_400_BAD_REQUEST)

                broker_account = getattr(adapter, 'broker_account', None)
                if broker_account:
                    exit_broker_order = BrokerOrder.objects.create(
                        broker_account=broker_account,
                        order=order,
                        broker_order_id=_order_id(exit_response) or "",
                        symbol=fyers_sym,
                        quantity=int(order.quantity),
                        side=exit_side,
                        order_type=BrokerOrder.OrderType.EXIT,
                        price=Decimal(str(exit_price)),
                        status=BrokerOrder.Status.PLACED,
                        notes="manual_exit",
                    )
                    exit_order_id = exit_broker_order.broker_order_id
                else:
                    exit_order_id = _order_id(exit_response) or ""

                # Close Order in DB
                result = close_trade(order, exit_price, "Manual")
                pnl = result.get("realized_pnl", 0) if isinstance(result, dict) else float(result)

                logger.info(
                    "Live trade closed: trade_id=%s, exit_price=%s, pnl=%s",
                    trade_id, exit_price, pnl
                )

                return Response({
                    "success": True,
                    "message": "Trade closed successfully",
                    "trade_id": str(trade_id),
                    "exit_price": exit_price,
                    "pnl": float(pnl),
                    "exit_order_id": exit_order_id,
                })

        except Exception as e:
            logger.exception("Error closing live trade %s: %s", trade_id, e)
            result = close_trade(order, exit_price, "Manual")
            pnl = result.get("realized_pnl", 0) if isinstance(result, dict) else 0

            return Response({
                "success": True,
                "message": "Trade closed in database (broker order may have failed)",
                "trade_id": str(trade_id),
                "exit_price": exit_price,
                "pnl": float(pnl),
                "warning": f"Broker communication error: {str(e)}"
            }, status=status.HTTP_200_OK)


# ─────────────────────────────────────────────────────────────────────────────
#  PAPER TRADING - Risk-Free Practice
# ─────────────────────────────────────────────────────────────────────────────

class PaperTradeView(APIView):
    """
    POST /api/options/paper-trade/
    GET  /api/options/paper-trade/
    DELETE /api/options/paper-trade/<trade_id>/
    
    Paper trading without real money - practice trading system
    """
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        """List paper trades"""
        status_filter = request.query_params.get("status", "open")

        qs = Order.objects.filter(
            user=request.user,
            mode="paper",
            instrument_type="options",
        ).select_related("asset").order_by("-entry_time", "-created_at")

        if status_filter != "all":
            qs = qs.filter(status=status_filter)

        trades_data = []
        for order in qs:
            ep = float(order.entry_price or 0)
            cp = float(order.current_price or order.entry_price or 0)
            qty = float(order.quantity or 0)
            side = order.side or "buy"

            if side == "buy":
                unrealized_pnl = (cp - ep) * qty
            else:
                unrealized_pnl = (ep - cp) * qty

            trades_data.append({
                "id": str(order.id),
                "symbol": order.symbol_display or (order.asset.symbol if order.asset else ""),
                "option_type": order.option_type,
                "trade_type": side,
                "quantity": qty,
                "lots": order.lots,
                "entry_price": ep,
                "current_price": cp,
                "unrealized_pnl": round(unrealized_pnl, 2),
                "stop_loss": float(order.sl_price) if order.sl_price else None,
                "target_price": float(order.target_price) if order.target_price else None,
                "entry_time": order.entry_time.isoformat() if order.entry_time else order.created_at.isoformat(),
                "exit_time": order.exit_time.isoformat() if order.exit_time else None,
                "exit_price": float(order.exit_price) if order.exit_price else None,
                "realized_pnl": float(order.realized_pnl) if order.realized_pnl else None,
                "status": order.status,
                "mode": order.mode,
            })

        return Response({
            "success": True,
            "trades": trades_data,
            "count": len(trades_data),
        })
    
    def post(self, request):
        """
        Place paper trade
        
        Same request body as LiveOptionTradeView
        """
        try:
            data = request.data
            
            # ── Extract and validate ──────────────────────────────────────────
            symbol_name = data.get("symbol")
            option_type = data.get("option_type", "").upper()
            trade_type = data.get("trade_type") or data.get("action", "").lower()
            
            if not all([symbol_name, option_type, trade_type]):
                return Response({
                    "error": "Missing required fields: symbol, option_type, trade_type/action"
                }, status=status.HTTP_400_BAD_REQUEST)
            
            if option_type not in ["CE", "PE"]:
                return Response({"error": "option_type must be 'CE' or 'PE'"}, status=status.HTTP_400_BAD_REQUEST)
            
            if trade_type not in ["buy", "sell"]:
                return Response({"error": "trade_type/action must be 'buy' or 'sell'"}, status=status.HTTP_400_BAD_REQUEST)
            
            try:
                # Quantity or lots
                if "quantity" in data:
                    quantity = int(data["quantity"])
                    lots = None
                elif "lots" in data:
                    lots = int(data["lots"])
                    quantity = None
                else:
                    return Response({"error": "Either 'quantity' or 'lots' required"}, status=status.HTTP_400_BAD_REQUEST)
                
                spot = float(data.get("spot", 0))
                entry_price = float(data.get("entry_price")) if data.get("entry_price") else None
                stop_loss = float(data.get("stop_loss"))
                target_price = float(data.get("target_price") or data.get("take_profit", 0)) or None
                strike = float(data["strike"]) if data.get("strike") else None
                
            except (ValueError, TypeError) as e:
                return Response({"error": f"Invalid numeric value: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)
            
            # Validate ranges
            if quantity and quantity <= 0:
                return Response({"error": "quantity must be > 0"}, status=status.HTTP_400_BAD_REQUEST)
            if lots and lots <= 0:
                return Response({"error": "lots must be > 0"}, status=status.HTTP_400_BAD_REQUEST)
            if entry_price and entry_price <= 0:
                return Response({"error": "entry_price must be > 0"}, status=status.HTTP_400_BAD_REQUEST)
            
            # SL/TP validation
            if entry_price:
                if trade_type == "buy":
                    if stop_loss >= entry_price:
                        return Response({"error": "stop_loss must be below entry_price for BUY"}, status=status.HTTP_400_BAD_REQUEST)
                    if target_price and target_price <= entry_price:
                        return Response({"error": "target_price must be above entry_price for BUY"}, status=status.HTTP_400_BAD_REQUEST)
                else:
                    if stop_loss <= entry_price:
                        return Response({"error": "stop_loss must be above entry_price for SELL"}, status=status.HTTP_400_BAD_REQUEST)
                    if target_price and target_price >= entry_price:
                        return Response({"error": "target_price must be below entry_price for SELL"}, status=status.HTTP_400_BAD_REQUEST)
            
            # ── Get symbol and contract ───────────────────────────────────────
            try:
                symbol = OptionSymbol.objects.get(name=symbol_name.upper())
            except OptionSymbol.DoesNotExist:
                symbol = get_or_create_option_symbol(symbol_name.upper())
            
            # Calculate quantity from lots
            if lots and not quantity:
                quantity = lots * symbol.lot_size
            
            # Determine expiry
            expiry_date = None
            if data.get("expiry"):
                from datetime import date as _date
                try:
                    expiry_date = _date.fromisoformat(str(data["expiry"]))
                except ValueError:
                    return Response({"error": "expiry must be YYYY-MM-DD"}, status=status.HTTP_400_BAD_REQUEST)
            else:
                expiry_date = nearest_expiry(monthly=bool(data.get("monthly", False)))
            
            # Determine strike
            if not strike and spot:
                strike_step = _get_strike_step(symbol_name)
                strike = round(spot / strike_step) * strike_step
            elif not strike:
                return Response({"error": "Either 'strike' or 'spot' is required"}, status=status.HTTP_400_BAD_REQUEST)
            
            # Get or create contract
            fyers_symbol = format_fyers_symbol(symbol_name, expiry_date, int(strike), option_type)
            option_contract, _ = OptionContract.objects.get_or_create(
                symbol=symbol,
                strike=Decimal(str(strike)),
                option_type=option_type,
                expiry=expiry_date,
                defaults={"fyers_symbol": fyers_symbol}
            )
            
            # Use contract LTP if entry_price not provided
            if not entry_price:
                entry_price = float(option_contract.ltp or 0)
                if entry_price <= 0:
                    return Response({"error": "Cannot determine entry_price - LTP unavailable"}, status=status.HTTP_400_BAD_REQUEST)
            
            # ── Check paper account balance ───────────────────────────────────
            paper_acc, _ = PaperAccount.objects.get_or_create(
                user=request.user,
                defaults={"balance": Decimal("100000.00")}
            )
            
            # Also try PaperAccount model (support both)
            if not paper_acc:
                paper_acc, _ = PaperAccount.objects.get_or_create(
                    user=request.user,
                    defaults={"balance": Decimal("100000.00")}
                )
            
            required_margin = Decimal(str(entry_price)) * quantity
            if paper_acc.balance < required_margin:
                return Response({
                    "error": f"Insufficient paper balance. Need ₹{required_margin:.2f}, have ₹{paper_acc.balance:.2f}"
                }, status=status.HTTP_400_BAD_REQUEST)
            
            # Daily limit check
            today_count = Order.objects.filter(
                user=request.user,
                mode="paper",
                instrument_type="options",
                created_at__date=timezone.now().date()
            ).count()
            if today_count >= 50:
                return Response({"error": "Daily paper trade limit reached (50/day)"}, status=status.HTTP_400_BAD_REQUEST)

            # Get or create Asset
            symbol_str = f"{symbol_name}{int(strike)}{option_type}"
            asset, _ = Asset.objects.get_or_create(
                symbol=symbol_str,
                defaults={
                    "name": symbol_str,
                    "asset_type": "options",
                    "exchange": "NSE",
                    "currency": "INR",
                    "is_active": True,
                },
            )

            # ── Create paper Order ────────────────────────────────────────────
            with transaction.atomic():
                order_obj = Order.objects.create(
                    user=request.user,
                    asset=asset,
                    mode=Order.Mode.PAPER,
                    side=trade_type,
                    order_type=Order.OrderType.MARKET,
                    status=Order.Status.OPEN,
                    quantity=Decimal(str(quantity)),
                    filled_qty=Decimal(str(quantity)),
                    entry_price=Decimal(str(entry_price)),
                    current_price=Decimal(str(entry_price)),
                    sl_price=Decimal(str(stop_loss)),
                    target_price=Decimal(str(target_price)) if target_price else None,
                    entry_time=timezone.now(),
                    option_type=option_type,
                    lots=lots or (quantity // symbol.lot_size),
                    position_size=quantity,
                    symbol_display=symbol_str,
                    instrument_type="options",
                    notes=data.get("setup_type", "Manual"),
                )

                # Deduct margin from paper account
                paper_acc.balance -= required_margin
                paper_acc.save()

                logger.info(
                    "Paper trade placed: user=%s, symbol=%s, strike=%s, qty=%s",
                    request.user.id, symbol_name, strike, quantity
                )

            return Response({
                "success": True,
                "trade_id": str(order_obj.id),
                "symbol": fyers_symbol,
                "strike": float(strike),
                "option_type": option_type,
                "quantity": quantity,
                "entry_price": float(entry_price),
                "stop_loss": float(stop_loss),
                "target_price": float(target_price) if target_price else None,
                "paper_balance": float(paper_acc.balance),
                "message": "Paper trade placed successfully"
            }, status=status.HTTP_201_CREATED)
        
        except Exception as e:
            logger.exception("Error in PaperTradeView.post: %s", e)
            return Response({
                "error": f"Unexpected error: {str(e)}"
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    def delete(self, request, trade_id):
        """Manually close paper trade"""
        try:
            order = Order.objects.get(
                id=trade_id,
                user=request.user,
                mode="paper",
                status="open",
            )
        except Order.DoesNotExist:
            return Response({
                "error": "Paper trade not found or already closed"
            }, status=status.HTTP_404_NOT_FOUND)

        exit_price = request.data.get("exit_price")
        if exit_price:
            exit_price = float(exit_price)
        else:
            exit_price = float(order.current_price or order.entry_price or 0)

        result = close_trade(order, exit_price, "Manual")
        pnl = result.get("realized_pnl", 0) if isinstance(result, dict) else 0

        return Response({
            "success": True,
            "message": "Paper trade closed",
            "trade_id": str(trade_id),
            "exit_price": exit_price,
            "pnl": float(pnl),
        })


class PaperAccountView(APIView):
    """
    GET /api/options/paper-account/
    
    Get paper trading account balance and statistics
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        # Try both account models
        paper_acc = None
        try:
            paper_acc = PaperAccount.objects.get(user=request.user)
        except PaperAccount.DoesNotExist:
            try:
                paper_acc = PaperAccount.objects.get(user=request.user)
            except PaperAccount.DoesNotExist:
                # Create new account
                paper_acc = PaperAccount.objects.create(
                    user=request.user,
                    balance=Decimal("100000.00")
                )

        # Calculate stats from Order model
        open_orders = Order.objects.filter(
            user=request.user,
            mode="paper",
            status="open",
            instrument_type="options",
        )

        margin_used = sum(
            float(o.entry_price or 0) * float(o.quantity or 0)
            for o in open_orders
        )

        # Unrealized PnL
        unrealized_pnl = 0.0
        for order in open_orders:
            if order.current_price and order.entry_price:
                cp = float(order.current_price)
                ep = float(order.entry_price)
                qty = float(order.quantity)

                if order.side == "buy":
                    unrealized_pnl += (cp - ep) * qty
                else:
                    unrealized_pnl += (ep - cp) * qty

        initial_capital = float(getattr(paper_acc, "initial_capital", 100000))
        current_balance = float(paper_acc.balance)
        realized_pnl = current_balance - initial_capital + margin_used

        return Response({
            "success": True,
            "balance": current_balance,
            "initial_capital": initial_capital,
            "margin_used": round(margin_used, 2),
            "available": current_balance,
            "realized_pnl": round(realized_pnl, 2),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "total_pnl": round(realized_pnl + unrealized_pnl, 2),
            "open_trades": open_orders.count(),
        })


# ─────────────────────────────────────────────────────────────────────────────
#  TRADE MANAGEMENT - Close & List
# ─────────────────────────────────────────────────────────────────────────────

class CloseTradeView(APIView):
    """
    POST /api/options/close-trade/
    
    Close any open trade (live or paper)
    """
    permission_classes = [IsAuthenticated]
    
    def post(self, request):
        """
        Request Body:
        {
            "trade_id": 123,
            "exit_price": 165.50  // Optional, uses LTP/current_price if not provided
        }
        """
        trade_id = request.data.get("trade_id")
        exit_price = request.data.get("exit_price")
        
        if not trade_id:
            return Response({
                "error": "trade_id is required"
            }, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            order = Order.objects.get(id=trade_id, user=request.user, status="open")

            if exit_price is None:
                exit_price = float(order.current_price or order.entry_price or 0)
            else:
                exit_price = float(exit_price)

            # For live orders, place broker exit order
            if order.mode == "live":
                try:
                    adapter = BrokerAdapterFactory.get_adapter(request.user)
                    exit_side = "sell" if order.side == "buy" else "buy"
                    fyers_sym = order.symbol_display or (order.asset.symbol if order.asset else "")

                    exit_response = adapter.place_order(
                        symbol=fyers_sym,
                        side=exit_side,
                        qty=int(order.quantity),
                        order_type="market",
                        price=0,
                        product_type="INTRADAY",
                    )

                    if _order_success(exit_response):
                        broker_acct = getattr(adapter, 'broker_account', None)
                        if broker_acct:
                            BrokerOrder.objects.create(
                                broker_account=broker_acct,
                                order=order,
                                broker_order_id=_order_id(exit_response) or "",
                                symbol=fyers_sym,
                                quantity=int(order.quantity),
                                side=exit_side,
                                order_type=BrokerOrder.OrderType.EXIT,
                                status=BrokerOrder.Status.PLACED,
                                notes="close_trade",
                            )
                except Exception as e:
                    logger.error("Broker exit order failed for order %s: %s", trade_id, e)

            # Close order in database
            result = close_trade(order, exit_price, "manual")
            pnl = result.get("realized_pnl", 0) if isinstance(result, dict) else 0

            return Response({
                "success": True,
                "message": "Trade closed successfully",
                "trade_id": str(trade_id),
                "exit_price": exit_price,
                "pnl": float(pnl),
                "mode": order.mode,
            })

        except Order.DoesNotExist:
            return Response({
                "error": "Trade not found or unauthorized"
            }, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            logger.exception("Error closing trade %s: %s", trade_id, e)
            return Response({
                "error": f"Failed to close trade: {str(e)}"
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class OpenTradesView(APIView):
    """
    GET /api/options/open-trades/?mode=live|paper|all
    
    Get all open trades for current user
    """
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        mode = request.query_params.get("mode", "all")
        
        qs = Order.objects.filter(
            user=request.user,
            status="open",
            instrument_type="options",
        ).select_related("asset")

        if mode != "all":
            qs = qs.filter(mode=mode)

        trades_data = []
        for order in qs:
            ep = float(order.entry_price or 0)
            cp = float(order.current_price or order.entry_price or 0)
            qty = float(order.quantity or 0)
            side = order.side or "buy"

            if side == "buy":
                unrealized_pnl = (cp - ep) * qty
            else:
                unrealized_pnl = (ep - cp) * qty

            trades_data.append({
                "id": str(order.id),
                "mode": order.mode,
                "symbol": order.symbol_display or (order.asset.symbol if order.asset else ""),
                "option_type": order.option_type,
                "trade_type": side,
                "quantity": qty,
                "lots": order.lots,
                "entry_price": ep,
                "current_price": cp,
                "unrealized_pnl": round(unrealized_pnl, 2),
                "unrealized_pnl_percent": round((unrealized_pnl / (ep * qty) * 100), 2) if ep > 0 else 0,
                "stop_loss": float(order.sl_price) if order.sl_price else None,
                "target_price": float(order.target_price) if order.target_price else None,
                "entry_time": order.entry_time.isoformat() if order.entry_time else order.created_at.isoformat(),
                "setup_type": order.notes or "Manual",
            })
        
        return Response({
            "success": True,
            "trades": trades_data,
            "count": len(trades_data),
        })


# ─────────────────────────────────────────────────────────────────────────────
#  BACKTEST - Historical Strategy Testing
# ─────────────────────────────────────────────────────────────────────────────

class BacktestRunView(APIView):
    """
    POST /api/options/backtest/
    GET  /api/options/backtest/<run_id>/
    
    Run and retrieve backtest results
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        """Start a backtest run"""
        try:
            symbol_name = request.data.get("symbol", "").upper()
            symbol = OptionSymbol.objects.get(name=symbol_name)
        except OptionSymbol.DoesNotExist:
            return Response({
                "error": f"Symbol '{symbol_name}' not found"
            }, status=status.HTTP_404_NOT_FOUND)

        try:
            run = BacktestRun.objects.create(
                user=request.user,
                symbol=symbol,
                from_date=request.data["from_date"],
                to_date=request.data["to_date"],
                strategy=request.data.get("strategy", "ICT_MTF"),
                initial_capital=float(request.data.get("capital", 500000)),
                status="pending",
            )
            
            # Trigger async backtest task
            run_backtest_task.delay(str(run.id))
            
            return Response({
                "success": True,
                "backtest_id": str(run.id),
                "status": "pending",
                "message": "Backtest queued for processing"
            }, status=status.HTTP_202_ACCEPTED)
        
        except KeyError as e:
            return Response({
                "error": f"Missing required field: {str(e)}"
            }, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.exception("Error creating backtest: %s", e)
            return Response({
                "error": f"Failed to create backtest: {str(e)}"
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def get(self, request, run_id):
        """Get backtest results"""
        try:
            run = BacktestRun.objects.get(id=run_id, user=request.user)
            return Response(BacktestRunSerializer(run).data)
        except BacktestRun.DoesNotExist:
            return Response({
                "error": "Backtest run not found"
            }, status=status.HTTP_404_NOT_FOUND)


# ─────────────────────────────────────────────────────────────────────────────
#  OPTION SNAPSHOTS - Historical Data
# ─────────────────────────────────────────────────────────────────────────────

class OptionSnapshotView(APIView):
    """
    GET /api/options/snapshots/<symbol_id>/
    GET /api/options/snapshots/?symbol=NIFTY
    
    Get historical option price snapshots
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, symbol_id=None):
        if symbol_id:
            try:
                symbol = OptionSymbol.objects.get(id=symbol_id)
            except OptionSymbol.DoesNotExist:
                return Response({
                    "error": "Symbol not found"
                }, status=status.HTTP_404_NOT_FOUND)
        else:
            symbol_name = request.query_params.get("symbol", "NIFTY").upper()
            try:
                symbol = OptionSymbol.objects.get(name=symbol_name)
            except OptionSymbol.DoesNotExist:
                return Response({
                    "error": f"Symbol '{symbol_name}' not found"
                }, status=status.HTTP_404_NOT_FOUND)

        limit = min(int(request.query_params.get("limit", 20)), 100)
        
        snapshots = (
            OptionSnapshot.objects
            .filter(contract__symbol=symbol)
            .select_related("contract")
            .order_by("-timestamp")[:limit]
        )
        
        from .serializers import OptionSnapshotSerializer
        return Response({
            "success": True,
            "symbol": symbol.name,
            "snapshots": OptionSnapshotSerializer(snapshots, many=True).data,
            "count": snapshots.count(),
        })


# ─────────────────────────────────────────────────────────────────────────────
#  HELPER FUNCTIONS
# ─────────────────────────────────────────────────────────────────────────────

def _calculate_unrealized_pnl(order) -> float:
    """Calculate unrealized PnL for an Order"""
    try:
        cp = float(order.current_price or order.entry_price or 0)
        ep = float(order.entry_price or 0)
        qty = float(order.quantity or 0)

        if order.side == "buy":
            return (cp - ep) * qty
        else:
            return (ep - cp) * qty
    except Exception:
        return 0.0


def _get_broker_order_id_from_order(order) -> str | None:
    """Get broker order ID for an Order"""
    try:
        bo = BrokerOrder.objects.filter(order=order).order_by("-created_at").first()
        return bo.broker_order_id if bo else None
    except Exception:
        return None

class OptionGreeksView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        symbol = request.query_params.get("symbol", "NIFTY")
        from apps.options.nse_fetcher import fetch_nse_option_chain
        from apps.options.black_scholes import greeks_from_chain

        try:
            data = fetch_nse_option_chain(symbol=symbol, user=request.user)
            spot = data.get("spot", 0)
            chain = data.get("chain", [])

            # ATM row dhundo
            atm_row = None
            min_diff = float("inf")
            for row in chain:
                diff = abs(row["strike"] - spot)
                if diff < min_diff:
                    min_diff = diff
                    atm_row = row

            if not atm_row:
                return Response({"error": "No ATM row found"}, status=400)

            expiries = data.get("expiries", [])
            expiry_str = expiries[0] if expiries else ""

            ce = atm_row.get("CE", {})
            pe = atm_row.get("PE", {})

            greeks = greeks_from_chain(
                spot=spot,
                strike=atm_row["strike"],
                expiry_str=expiry_str,
                ce_ltp=ce.get("ltp", 0),
                pe_ltp=pe.get("ltp", 0),
                ce_iv=ce.get("iv", 0),
                pe_iv=pe.get("iv", 0),
            )
            greeks["symbol"] = symbol
            greeks["spot"] = spot
            return Response({"success": True, "greeks": greeks})

        except Exception as e:
            logger.error("Greeks failed | %s | %s", symbol, e)
            return Response({"error": str(e)}, status=500)
