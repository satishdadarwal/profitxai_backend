import logging
from collections import defaultdict

from django.contrib.auth import get_user_model

from asgiref.sync import async_to_sync
from celery import shared_task
from channels.layers import get_channel_layer

from apps.brokers.models import BrokerAccount  # ✅ moved to top-level import
from broker_adapters.fyers.adapter import FyersAdapter

from .models import OptionTrade
from .services import check_sltp_for_trade, close_trade, estimate_premium, update_trailing_sl

logger = logging.getLogger(__name__)
User = get_user_model()


@shared_task
def update_spot_and_check_sltp():
    """
    Celery Beat har 10 second pe chalayega.
    Sabhi users ke open trades ek saath check honge.
    """
    open_trades = OptionTrade.objects.filter(
        status="open", mode__in=["paper", "live"]
    ).select_related("user", "symbol", "contract")

    if not open_trades.exists():
        return

    # Group trades by user
    trades_by_user: dict[int, list] = defaultdict(list)
    for trade in open_trades:
        trades_by_user[trade.user.pk].append(trade)  # ✅ use pk (int), not user object

    channel_layer = get_channel_layer()

    for user_pk, trades in trades_by_user.items():
        # Get this user's Fyers credentials
        try:
            broker_account = BrokerAccount.objects.get(
                user_id=user_pk,           # ✅ user_id (int FK column) — no Pylance issue
                broker="fyers",
                is_active=True,
            )
        except BrokerAccount.DoesNotExist:
            logger.warning(f"No active Fyers account for user_id={user_pk}")
            continue
        except Exception as e:
            logger.warning(f"BrokerAccount lookup failed for user_id={user_pk}: {e}")
            continue

        # ✅ Build credentials dict from actual model fields (no .credentials attr)
        credentials = {
            "app_id": broker_account.app_id,
            "access_token": broker_account.access_token or "",
            "refresh_token": broker_account.refresh_token or "",
            "app_secret": broker_account.secret_key,
        }

        client = FyersAdapter(credentials=credentials)

        # Collect symbols for this user's trades
        symbols_needed = list({t.symbol.fyers_symbol for t in trades})
        spot_map: dict[str, float] = {}

        try:
            spot_map = {
                sym: data["ltp"]
                for sym, data in client.get_bulk_quotes(symbols_needed).items()
                if data.get("ltp")
            }
        except Exception as e:
            logger.warning(f"Bulk quote fetch failed for user_id={user_pk}: {e}")

        for trade in trades:
            spot = spot_map.get(trade.symbol.fyers_symbol)
            if not spot:
                continue

            current_premium = estimate_premium(
                spot=spot,
                strike=trade.contract.strike,
                option_type=trade.contract.option_type,
                entry_spot=trade.entry_spot,
                entry_premium=trade.entry_price,
            )

            trade.current_price = current_premium
            trade.current_spot = spot
            trade.save(update_fields=["current_price", "current_spot"])
            
            update_trailing_sl(trade, current_premium)
            result = check_sltp_for_trade(trade, current_premium)

            if not channel_layer:
                logger.warning("Channel layer not configured — skipping WebSocket push.")
                continue

            user_group = f"user_{trade.user.pk}"  # ✅ pk instead of .id

            if result:
                pnl = close_trade(trade, result["exit_price"], result["reason"])
                async_to_sync(channel_layer.group_send)(
                    user_group,
                    {
                        "type": "trade.closed",
                        "trade_id": str(trade.pk),   # ✅ pk
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
                        "trade_id": str(trade.pk),   # ✅ pk
                        "current_price": current_premium,
                        "current_spot": spot,
                    },
                )


@shared_task(bind=True, max_retries=3)
def run_backtest_task(self, run_id: str):
    """
    BacktestRun ko async process karo.
    views.py: run_backtest_task.delay(str(run.id))
    """
    from django.utils import timezone
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

        # BacktestRun ke actual fields mein save karo
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
        BacktestRun.objects.filter(pk=run_id).update(
            status=BacktestRun.FAILED,
            error_message=str(exc)[:500],
        )
        raise self.retry(exc=exc, countdown=10)
    
@shared_task(bind=True, max_retries=3)
def place_broker_order(self, order_data: dict):
    """
    Async broker order placement
    """

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
