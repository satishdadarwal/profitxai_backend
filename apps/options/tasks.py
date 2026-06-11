import logging
from collections import defaultdict
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.utils import timezone

from asgiref.sync import async_to_sync
from celery import shared_task
from channels.layers import get_channel_layer

from apps.brokers.models import BrokerAccount
from broker_adapters.fyers.adapter import FyersAdapter

from .services import estimate_premium

logger = logging.getLogger(__name__)
User = get_user_model()


@shared_task
def update_spot_and_check_sltp():
    """
    Celery Beat har 10 second pe chalayega.
    Sabhi users ke open option orders ek saath check honge.
    Uses Order model (canonical).
    """
    from apps.orders.models import Order

    open_orders = Order.objects.filter(
        status="open", instrument_type="options"
    ).select_related("user", "asset")

    if not open_orders.exists():
        return

    # Group orders by user
    orders_by_user: dict[int, list] = defaultdict(list)
    for order in open_orders:
        orders_by_user[order.user.pk].append(order)

    channel_layer = get_channel_layer()

    for user_pk, orders in orders_by_user.items():
        # Get this user's Fyers credentials
        try:
            broker_account = BrokerAccount.objects.get(
                user_id=user_pk,
                broker="fyers",
                is_active=True,
            )
        except BrokerAccount.DoesNotExist:
            logger.warning(f"No active Fyers account for user_id={user_pk}")
            continue
        except Exception as e:
            logger.warning(f"BrokerAccount lookup failed for user_id={user_pk}: {e}")
            continue

        credentials = {
            "app_id": broker_account.app_id,
            "access_token": broker_account.access_token or "",
            "refresh_token": broker_account.refresh_token or "",
            "app_secret": broker_account.secret_key,
        }

        client = FyersAdapter(credentials=credentials)

        # Collect symbols for this user's orders
        symbols_needed = list({o.symbol_display for o in orders if o.symbol_display})
        spot_map: dict[str, float] = {}

        try:
            spot_map = {
                sym: data["ltp"]
                for sym, data in client.get_bulk_quotes(symbols_needed).items()
                if data.get("ltp")
            }
        except Exception as e:
            logger.warning(f"Bulk quote fetch failed for user_id={user_pk}: {e}")

        for order in orders:
            symbol_key = order.symbol_display
            current_price = spot_map.get(symbol_key)
            if not current_price:
                continue

            # Update current price
            order.current_price = Decimal(str(current_price))
            order.save(update_fields=["current_price", "updated_at"])

            # Check SL/TP
            sl = order.sl_price
            tp = order.target_price
            side = order.side
            entry = order.entry_price

            result = None
            if side == "buy":
                if sl and current_price <= float(sl):
                    result = {"reason": "SL", "exit_price": float(sl)}
                elif tp and current_price >= float(tp):
                    result = {"reason": "TP", "exit_price": float(tp)}
            else:  # sell
                if sl and current_price >= float(sl):
                    result = {"reason": "SL", "exit_price": float(sl)}
                elif tp and current_price <= float(tp):
                    result = {"reason": "TP", "exit_price": float(tp)}

            if not channel_layer:
                logger.warning("Channel layer not configured — skipping WebSocket push.")
                if result:
                    _close_order(order, result["exit_price"], result["reason"])
                continue

            user_group = f"user_{order.user.pk}"

            if result:
                pnl = _close_order(order, result["exit_price"], result["reason"])
                async_to_sync(channel_layer.group_send)(
                    user_group,
                    {
                        "type": "trade.closed",
                        "trade_id": str(order.pk),
                        "reason": result["reason"],
                        "exit_price": result["exit_price"],
                        "pnl": pnl,
                    },
                )
            else:
                async_to_sync(channel_layer.group_send)(
                    user_group,
                    {
                        "type": "trade.price_update",
                        "trade_id": str(order.pk),
                        "current_price": current_price,
                    },
                )


def _close_order(order, exit_price: float, reason: str) -> float:
    """Close an Order record and calculate realized PnL."""
    from apps.orders.models import Order

    if order.status != "open":
        return 0.0

    entry = float(order.entry_price or 0)
    qty = float(order.quantity or 0)

    if order.side == "buy":
        pnl = (exit_price - entry) * qty
    else:
        pnl = (entry - exit_price) * qty

    order.status = Order.Status.FILLED
    order.exit_price = Decimal(str(exit_price))
    order.exit_time = timezone.now()
    order.exit_reason = reason
    order.realized_pnl = Decimal(str(round(pnl, 2)))
    order.save(update_fields=[
        "status", "exit_price", "exit_time", "exit_reason",
        "realized_pnl", "updated_at"
    ])

    logger.info(
        "Order closed | id=%s | mode=%s | reason=%s | pnl=%.2f",
        order.id, order.mode, reason, pnl,
    )
    return pnl


@shared_task(bind=True, max_retries=3)
def run_backtest_task(self, run_id: str):
    """
    BacktestRun ko async process karo.
    views.py: run_backtest_task.delay(str(run.id))
    """
    from apps.options.models import BacktestRun

    try:
        run = BacktestRun.objects.get(pk=run_id)
    except BacktestRun.DoesNotExist:
        logger.error("BacktestRun not found | run_id=%s", run_id)
        return

    try:
        run.status = BacktestRun.RUNNING
        run.save(update_fields=["status"])

        # ICT_MTF ya dusri strategy
        if run.strategy == "ICT_MTF":
            from apps.strategies.ict_integration import run_backtest_ict
            result = run_backtest_ict(
                strategy=None,
                from_date=str(run.from_date),
                to_date=str(run.to_date),
                timeframe="15m",
                symbol=run.symbol.name,
                capital=float(run.initial_capital),
            )
        else:
            result = {
                "total_trades": 0,
                "net_pnl": 0,
                "win_rate": 0,
                "final_balance": run.initial_capital,
                "max_drawdown": 0,
            }

        run.status = BacktestRun.COMPLETED
        run.completed_at = timezone.now()
        run.total_trades = result.get("total_trades", 0)
        run.total_pnl = result.get("net_pnl", 0)
        run.win_rate = result.get("win_rate", 0)
        run.final_capital = result.get("final_balance", run.initial_capital)
        run.max_drawdown = result.get("max_drawdown", 0)
        run.save(update_fields=[
            "status", "completed_at", "total_trades",
            "total_pnl", "win_rate", "final_capital", "max_drawdown",
        ])

        logger.info(
            "Backtest complete | run=%s | trades=%s | pnl=%s",
            run_id, run.total_trades, run.total_pnl,
        )

    except Exception as exc:
        logger.error("Backtest failed | run=%s | err=%s", run_id, exc)
        from apps.options.models import BacktestRun
        BacktestRun.objects.filter(pk=run_id).update(
            status=BacktestRun.FAILED,
            error_message=str(exc)[:500],
        )
        raise self.retry(exc=exc, countdown=10)


@shared_task(bind=True, max_retries=3)
def place_broker_order(self, order_data: dict):
    """Async broker order placement"""
    try:
        broker_name = order_data.get("broker", "fyers")
        credentials = order_data.get("credentials", {})

        from broker_adapters.factory import BrokerAdapterFactory

        adapter = BrokerAdapterFactory.get_adapter(
            broker_name,
            credentials,
        )

        result = adapter.place_order(
            symbol=order_data["symbol"],
            side=order_data["side"],
            qty=order_data["qty"],
            order_type=order_data.get("order_type", "market"),
            price=order_data.get("price", 0),
        )

        logger.info(
            "Broker order placed | broker=%s | success=%s",
            broker_name,
            result.success,
        )

        return {
            "success": result.success,
            "order_id": result.order_id,
            "message": result.message,
        }

    except Exception as exc:
        logger.error(f"place_broker_order failed: {exc}")
        raise self.retry(exc=exc, countdown=5)


@shared_task(name="options.generate_options_predictions", queue="default")
def generate_options_predictions():
    """Run every 30 min during market hours."""
    from apps.options.options_prediction import generate_options_prediction
    from django.contrib.auth import get_user_model
    User = get_user_model()

    user = User.objects.filter(is_staff=True).first() or User.objects.first()
    symbols = ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"]
    results = []

    for symbol in symbols:
        try:
            pred = generate_options_prediction(symbol_name=symbol, user=user)
            if pred:
                results.append({"symbol": symbol, "direction": pred.direction})
                logger.info("Options prediction | %s | %s", symbol, pred.direction)
        except Exception as e:
            logger.error("Options prediction failed | %s | %s", symbol, e)

    return results
