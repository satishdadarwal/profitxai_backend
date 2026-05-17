# apps/strategies/tasks.py

import logging
from typing import Optional

from django.utils import timezone

from celery import group, shared_task

logger = logging.getLogger(__name__)


@shared_task
def run_all_active_strategies():
    from apps.strategies.models import Strategy

    active_strategies = Strategy.objects.filter(
        state=Strategy.State.RUNNING
    ).values_list('id', flat=True)

    if not active_strategies:
        logger.info("No active strategies to run")

        # ✅ FIX: Strategies jo unexpectedly idle ho gayi hain unhe detect karo
        # Yeh tab hota hai jab strategy cycle mein error aata hai ya WS disconnect hota hai
        # Log karo taaki admin ko pata chale
        idle_strategies = Strategy.objects.filter(
            state=Strategy.State.IDLE,
        ).values_list('id', 'name', flat=False)
        if idle_strategies:
            logger.warning(
                "⚠️ %d idle strateg(y/ies) found — not running: %s "
                "(restart them from the dashboard to resume)",
                len(idle_strategies),
                [f"{sid}:{name}" for sid, name in idle_strategies],
            )
        return

    logger.info(f"🚀 Starting {len(active_strategies)} strategies in parallel")

    job = group(
        run_strategy_cycle.s(str(sid))
        for sid in active_strategies
    )
    result = job.apply_async()

    logger.info(f"✅ Dispatched {len(active_strategies)} strategy tasks")

    return {
        "total_strategies": len(active_strategies),
        "task_ids": [str(r.id) for r in result.results] if hasattr(result, 'results') else []
    }

 



# ─────────────────────────────────────────────────────────────────
#  1. run_strategy_cycle
# ─────────────────────────────────────────────────────────────────
@shared_task(
    bind=True,
    name="strategies.run_strategy_cycle",
    queue="strategies",
    max_retries=3,
    default_retry_delay=30,
    soft_time_limit=55,
    time_limit=60,
    acks_late=True,
)
def run_strategy_cycle(self, strategy_id: str):
    from .models import Strategy
    from .services import (
        StrategyError,
        StrategyNotRunningError,
        execute_cycle,
        stop_strategy,
    )

    try:
        strategy = Strategy.objects.select_related("user", "broker").get(pk=strategy_id)
    except Strategy.DoesNotExist:
        logger.error("Strategy %s not found — cleaning up task", strategy_id)
        _cleanup_orphan_task(self.request.hostname, strategy_id)
        return

    if not strategy.is_running:
        # ✅ FIX: Clear log — user ko dashboard se restart karne ki guidance do
        logger.warning(
            "Strategy %s is %s — skipping cycle. "
            "Reason: %s. Restart from dashboard to resume.",
            strategy_id,
            strategy.state,
            getattr(strategy, 'error_msg', 'none') or 'unknown',
        )
        return

    sub = getattr(strategy.user, "subscription", None)
    if sub and not sub.is_access_granted:
        logger.warning(
            "Subscription expired for user %s — stopping strategy %s",
            # FIX: strategy.user_id → strategy.user.pk
            # Pylance doesn't recognise auto-generated `_id` attrs on UUID-pk models
            strategy.user.pk,
            strategy_id,
        )
        try:
            stop_strategy(strategy, reason="Subscription expired")
        except StrategyError:
            pass
        return

    symbols = strategy.symbols if strategy.symbols else [strategy.symbol]

    for symbol in symbols:
        if not strategy.is_running:
            break

        try:
            signal = execute_cycle(strategy, symbol=symbol)
            logger.debug(
                "Cycle done | strategy=%s | symbol=%s | signal=%s | result=%s",
                strategy_id,
                symbol,
                signal.signal_type,
                signal.result,
            )

        except StrategyNotRunningError:
            logger.info(
                "Strategy %s stopped mid-cycle at symbol=%s", strategy_id, symbol
            )
            break

        except Exception as exc:
            logger.exception(
                "Cycle exception | strategy=%s | symbol=%s | attempt=%d | %s",
                strategy_id,
                symbol,
                self.request.retries + 1,
                exc,
            )
            try:
                raise self.retry(exc=exc, countdown=30 * (self.request.retries + 1))
            except self.MaxRetriesExceededError:
                logger.error(
                    "Max retries exceeded for strategy %s — marking as error",
                    strategy_id,
                )
                Strategy.objects.filter(pk=strategy_id).update(
                    state=Strategy.State.ERROR,
                    error_msg=f"Max retries exceeded: {exc}",
                    updated_at=timezone.now(),
                )
                return


# ─────────────────────────────────────────────────────────────────
#  2. refresh_all_broker_tokens
# ─────────────────────────────────────────────────────────────────
@shared_task(
    name="strategies.refresh_all_broker_tokens",
    queue="default",
    bind=True,
    max_retries=1,
    soft_time_limit=120,
)
def refresh_all_broker_tokens(self):
    refreshed = 0
    failed = 0
    paused_strategies = 0

    try:
        # FIX: `Broker` is not in apps.brokers — BrokerAccount is the correct model
        # "Broker" import error → use BrokerAccount which definitely exists
        from apps.brokers.models import BrokerAccount
    except ImportError:
        logger.warning("apps.brokers not found — skipping token refresh")
        return {"status": "skipped", "reason": "brokers app not found"}

    accounts = BrokerAccount.objects.filter(
        is_active=True, is_verified=True
    ).select_related("user")

    for account in accounts:
        try:
            if hasattr(account, "refresh_token"):
                account.refresh_token()  # type: ignore[operator]
                refreshed += 1
                logger.debug(
                    "Token refreshed | account=%s | user=%s",
                    account.pk,
                    account.user.pk,  # FIX: .user.pk instead of .user_id
                )
        except Exception as exc:
            failed += 1
            logger.error("Token refresh failed | account=%s | %s", account.pk, exc)

            BrokerAccount.objects.filter(pk=account.pk).update(is_active=False)

            from .models import Strategy
            from .services import stop_strategy

            for strategy in Strategy.objects.filter(
                broker=account, state=Strategy.State.RUNNING
            ):
                try:
                    stop_strategy(
                        strategy, reason=f"Broker token refresh failed: {exc}"
                    )
                    paused_strategies += 1
                except Exception as stop_exc:
                    logger.error(
                        "Could not stop strategy %s: %s", strategy.id, stop_exc
                    )

            try:
                from apps.notifications.tasks import send_notification_task

                # FIX: send_notification_task.delay() is a Celery task — it IS callable
                # but Pylance sees `list[str]` return from @shared_task decorator stubs.
                # Using cast or type:ignore is the right fix here.
                send_notification_task.delay(  # type: ignore[operator]
                    user_id=account.user.pk,
                    channel="both",
                    title="Broker Connection Lost",
                    body=(
                        f"Could not refresh token for broker '{account.broker}'. "
                        "Please reconnect your broker to resume trading."
                    ),
                    level="error",
                    category="broker",
                    metadata={"broker_id": str(account.pk)},
                )
            except Exception:
                pass

    logger.info(
        "Token refresh complete | refreshed=%d | failed=%d | paused=%d",
        refreshed,
        failed,
        paused_strategies,
    )
    return {
        "refreshed": refreshed,
        "failed": failed,
        "paused_strategies": paused_strategies,
    }


# ─────────────────────────────────────────────────────────────────
#  3. sync_all_open_orders
# ─────────────────────────────────────────────────────────────────
@shared_task(
    name="strategies.sync_all_open_orders",
    queue="orders",
    bind=True,
    max_retries=2,
    soft_time_limit=180,
)
def sync_all_open_orders(self):
    import datetime
    from decimal import Decimal

    from apps.orders.models import Order
    from apps.orders.services import InvalidOrderError, cancel_order, fill_order

    synced = 0
    filled = 0
    cancelled = 0
    errors = 0

    open_orders = (
        Order.objects.filter(
            status__in=[Order.Status.OPEN, Order.Status.PARTIAL],
            mode=Order.Mode.LIVE,
        )
        .select_related("user", "asset", "strategy")
        .order_by("created_at")
    )

    stale_cutoff = timezone.now() - datetime.timedelta(hours=24)

    for order in open_orders:
        try:
            broker = _get_order_broker(order)

            if broker is None:
                if order.created_at < stale_cutoff:
                    cancel_order(order=order, reason="Stale order: no broker linked")
                    cancelled += 1
                continue

            exchange_status = _fetch_order_from_exchange(broker, order)

            if exchange_status is None:
                cancel_order(order=order, reason="Order not found on exchange")
                cancelled += 1
                continue

            ex_state = exchange_status.get("status")
            fill_price = exchange_status.get("fill_price")
            fill_qty = exchange_status.get("fill_qty")

            if ex_state == "filled" and fill_price:
                fill_order(
                    order=order,
                    fill_price=Decimal(str(fill_price)),
                    fill_qty=Decimal(str(fill_qty)) if fill_qty else None,
                )
                filled += 1
            elif ex_state == "cancelled":
                cancel_order(order=order, reason="Cancelled on exchange")
                cancelled += 1

            synced += 1

        except InvalidOrderError as exc:
            logger.warning("Invalid order %s | %s", order.id, exc)
        except Exception as exc:
            errors += 1
            logger.error("Error on order %s | %s", order.id, exc)

    logger.info(
        "Order sync | synced=%d | filled=%d | cancelled=%d | errors=%d",
        synced,
        filled,
        cancelled,
        errors,
    )
    return {"synced": synced, "filled": filled, "cancelled": cancelled, "errors": errors}


# ─────────────────────────────────────────────────────────────────
#  4. take_performance_snapshots
# ─────────────────────────────────────────────────────────────────
@shared_task(
    name="strategies.take_performance_snapshots",
    queue="default",
    soft_time_limit=120,
)
def take_performance_snapshots():
    import datetime

    from django.db.models import Count, Q, Sum

    from apps.orders.models import Trade

    from .models import Strategy, StrategyPerformanceSnapshot

    now = timezone.now().replace(minute=0, second=0, microsecond=0)
    saved = 0

    for strategy in Strategy.objects.filter(state=Strategy.State.RUNNING):
        try:
            since = now - datetime.timedelta(hours=1)

            # FIX: strategy.signals → StrategySignal reverse relation
            # The related_name on StrategySignal.strategy FK determines this.
            # If related_name="signals" is not set, use strategy.strategysignal_set
            # We use getattr to be safe if the related_name differs.
            signals_qs = getattr(strategy, "signals", None)
            if signals_qs is None:
                # fallback if related_name is not "signals"
                from .models import StrategySignal
                signals_qs = StrategySignal.objects.filter(
                    strategy=strategy,created_at__gte=since)
            else:
                signals_qs = signals_qs.filter(created_at__gte=since)  # type: ignore[union-attr]

            sig_agg = signals_qs.aggregate(
                total=Count("id"),
                executed=Count("id", filter=Q(result="executed")),
            )

            order_ids = signals_qs.exclude(order=None).values_list(
                "order_id", flat=True
            )
            trade_agg = Trade.objects.filter(order_id__in=order_ids).aggregate(
                total=Count("id"),
                wins=Count("id", filter=Q(realized_pnl__gt=0)),
                pnl=Sum("realized_pnl"),
                fees=Sum("fee"),
            )

            total_t = trade_agg["total"] or 0
            wins = trade_agg["wins"] or 0
            pnl = trade_agg["pnl"] or 0
            fees = trade_agg["fees"] or 0
            win_rate = (wins / total_t) if total_t else 0

            StrategyPerformanceSnapshot.objects.update_or_create(
                strategy=strategy,
                granularity=StrategyPerformanceSnapshot.Granularity.HOURLY,
                period_start=now,
                defaults={
                    "total_signals": sig_agg["total"] or 0,
                    "executed_signals": sig_agg["executed"] or 0,
                    "total_trades": total_t,
                    "win_trades": wins,
                    "loss_trades": total_t - wins,
                    "realized_pnl": pnl,
                    "total_fees": fees,
                    "net_pnl": pnl - fees,
                    "win_rate": win_rate,
                    "avg_trade_pnl": (pnl / total_t) if total_t else 0,
                },
            )
            saved += 1

        except Exception as exc:
            logger.error("Snapshot failed | strategy=%s | %s", strategy.id, exc)

    logger.info("Performance snapshots saved | count=%d", saved)
    return {"saved": saved}


# ─────────────────────────────────────────────────────────────────
#  Private helpers
# ─────────────────────────────────────────────────────────────────
def _get_order_broker(order):
    # FIX: old code used order.strategy_signals which doesn't exist as a reverse
    # relation. Order.strategy is now a direct FK — use that instead.
    try:
        if order.strategy and order.strategy.broker:
            return order.strategy.broker
    except Exception:
        pass
    return None


def _fetch_order_from_exchange(broker, order) -> Optional[dict]:
    if not order.exchange_order_id:
        return None
    try:
        if hasattr(broker, "fetch_order"):
            return broker.fetch_order(order.exchange_order_id)
    except Exception as exc:
        logger.warning(
            "fetch_order failed | order=%s | broker=%s | %s", order.id, broker.pk, exc
        )
    return None


def _cleanup_orphan_task(hostname: str, strategy_id: str):
    try:
        from django_celery_beat.models import PeriodicTask
        PeriodicTask.objects.filter(name=f"strategy_{strategy_id}").delete()
        logger.info("Orphan Beat task cleaned: strategy_%s", strategy_id)
    except Exception as exc:
        logger.warning("Orphan task cleanup failed: %s", exc)


@shared_task(name="strategies.run_ict_screener", queue="strategies")
def run_ict_screener():
    from apps.brokers.models import BrokerAccount
    from apps.ict_engine.screener import push_screener_signals

    accounts = BrokerAccount.objects.filter(
        broker="fyers", is_active=True, is_verified=True
    ).select_related("user")

    for account in accounts:
        try:
            push_screener_signals(account.user)
        except Exception as e:
            # FIX: account.user_id → account.user.pk
            logger.error("Screener error user=%s: %s", account.user.pk, e)