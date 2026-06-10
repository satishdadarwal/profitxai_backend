# apps/brokers/fill_handler.py


import logging
from decimal import Decimal
from typing import Optional

from celery import shared_task
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  Main Celery Task — Celery Beat se har 10s pe chalta hai
# ─────────────────────────────────────────────────────────────────────────────

@shared_task(
    bind=True,
    name="brokers.poll_broker_order_fills",
    queue="orders",
    max_retries=0,           # agar fail ho toh skip karo, next cycle mein dobara
    soft_time_limit=8,       # 8s limit — 10s schedule se pehle khatam ho
)
def poll_broker_order_fills(self):
    """
    Celery Beat task — har 10 second mein chalta hai.

    OPEN BrokerOrders dhundo → Fyers se fill status check karo
    → Fill mila? → process_broker_fill() call karo

    Sirf OPEN orders check karta hai (broker ne accept kiya, fill awaited).
    PENDING orders is task ka kaam nahi — woh place_broker_order task karta hai.
    """
    from .models import BrokerOrder

    # Sirf OPEN orders — broker ke paas gaye hain, fill awaited
    open_orders = (
        BrokerOrder.objects
        .filter(
            status=BrokerOrder.Status.OPEN,
            exchange_order_id__gt="",          # exchange ID hona chahiye
            order__isnull=False,               # naye flow ke orders sirf
        )
        .select_related("broker_account", "order__asset", "order__user")
        [:50]                                  # ek baar mein max 50 — timeout rokna
    )

    # ✅ FIX (BLOCKER #3): Paper orders ka alag path hai.
    # poll_broker_order_fills sirf LIVE orders process kare — paper orders
    # _simulate_paper_fill() se immediately fill ho jaate hain (no Fyers API call).
    paper_orders = (
        BrokerOrder.objects
        .filter(
            status=BrokerOrder.Status.OPEN,
            exchange_order_id__gt="",
            order__isnull=False,
            order__mode="paper",               # sirf paper orders
        )
        .select_related("broker_account", "order__asset", "order__user")
        [:50]
    )

    for paper_order in paper_orders:
        try:
            _simulate_paper_fill(paper_order)
        except _AlreadyProcessed:
            pass
        except Exception as e:
            logger.error(
                "poll_broker_order_fills [paper]: error | broker_order=%s | %s",
                paper_order.id, e,
            )

    if not open_orders:
        return {"checked": 0, "filled": 0, "errors": 0}

    filled = 0
    errors = 0

    for broker_order in open_orders:
        try:
            _check_and_process_single_order(broker_order)
            filled += 1
        except _AlreadyProcessed:
            pass   # dusre worker ne pehle process kar diya — theek hai
        except Exception as e:
            errors += 1
            logger.error(
                "poll_broker_order_fills: error | broker_order=%s | %s",
                broker_order.id, e,
            )

    logger.info(
        "poll_broker_order_fills: checked=%d filled=%d errors=%d",
        len(open_orders), filled, errors,
    )
    return {"checked": len(open_orders), "filled": filled, "errors": errors}


# ─────────────────────────────────────────────────────────────────────────────
#  Ek order check karo — Fyers API se status lo
# ─────────────────────────────────────────────────────────────────────────────

class _AlreadyProcessed(Exception):
    """Dusre worker ne pehle process kar diya — skip karo."""
    pass


# ─────────────────────────────────────────────────────────────────────────────
#  Paper Mode — Simulate fill without hitting Fyers API
# ─────────────────────────────────────────────────────────────────────────────

def _get_paper_fill_price(order_id: str) -> Decimal:
    """
    Paper order ke liye fill price decide karo.

    Priority:
    1. BrokerOrder.order.limit_price (user ne jo price set kiya)
    2. Redis LTP cache (symbol se)
    3. Asset.last_price (DB fallback)
    4. Hardcoded 100 (last resort — paper mode mein exact price matter nahi)

    Yeh function FakeFyersAdapter.get_order_status() bhi call karta hai.
    """
    try:
        from .models import BrokerOrder
        bo = BrokerOrder.objects.select_related("order__asset").get(
            exchange_order_id=order_id
        )
        order = bo.order

        # 1. limit_price from Order
        if order and order.limit_price and order.limit_price > 0:
            return Decimal(str(order.limit_price))

        # 2. Redis LTP
        if order and order.asset:
            try:
                from django.core.cache import cache
                ltp = cache.get(f"ltp:{order.asset.symbol}")
                if ltp:
                    return Decimal(str(ltp))
            except Exception:
                pass

        # 3. Asset last_price
        if order and order.asset and hasattr(order.asset, "last_price"):
            lp = order.asset.last_price
            if lp and lp > 0:
                return Decimal(str(lp))

    except Exception as e:
        logger.warning("_get_paper_fill_price: could not resolve price | order_id=%s | %s", order_id, e)

    # 4. Safe fallback
    return Decimal("100")


def _simulate_paper_fill(broker_order) -> None:
    """
    Paper BrokerOrder ko immediately COMPLETE mark karo — no Fyers API call.

    ✅ FIX (BLOCKER #3):
    ─────────────────────
    Pehle paper orders bhi Fyers API se fill status check karte the.
    Result: agar Fyers token expired / symbol wrong tha toh paper order
    bhi silently FAILED ho jaata tha — paper trading bhi kaam nahi karta tha.

    Ab: paper orders ka alag path hai. Fill price:
    - order.limit_price → Redis LTP → asset.last_price → Rs 100 fallback

    Yeh EXACT wahi code path exercise karta hai jo live mein hoga:
    process_broker_fill() → fill_order() → wallet settlement → WebSocket push
    Fark sirf itna hai ki fill price real market se nahi aata.
    """
    order = broker_order.order
    if not order:
        logger.error("_simulate_paper_fill: no linked Order | broker_order=%s", broker_order.id)
        return

    if order.mode != "paper":
        # Safety guard — kabhi live order simulate nahi karna
        logger.error(
            "_simulate_paper_fill: called on LIVE order! broker_order=%s | order=%s — BLOCKED",
            broker_order.id, order.id,
        )
        return

    fill_price = _get_paper_fill_price(broker_order.exchange_order_id)
    fill_qty   = order.remaining_qty or Decimal(str(broker_order.quantity or 1))

    logger.info(
        "📝 Paper fill | broker_order=%s | symbol=%s | qty=%s @ %s",
        broker_order.id, broker_order.symbol, fill_qty, fill_price,
    )

    try:
        process_broker_fill(
            broker_order_id = str(broker_order.id),
            fill_price       = fill_price,
            fill_qty         = fill_qty,
            broker_response  = {
                "source":     "paper_simulation",
                "fill_price": str(fill_price),
                "fill_qty":   str(fill_qty),
            },
        )
    except _AlreadyProcessed:
        # Dusra worker ne pehle se process kar diya — theek hai
        logger.debug(
            "_simulate_paper_fill: already processed | broker_order=%s",
            broker_order.id,
        )


def _check_and_process_single_order(broker_order) -> None:
    """
    Ek BrokerOrder ke liye Fyers se status fetch karo.
    Fill mila toh process_broker_fill() call karo.
    """
    from .utils import get_adapter_for_account

    try:
        adapter = get_adapter_for_account(broker_order.broker_account)
    except Exception as e:
        logger.error(
            "get_adapter error | broker_order=%s | %s", broker_order.id, e
        )
        return

    # Fyers se order status fetch karo
    try:
        status_result = adapter.get_order_status(
            order_id=broker_order.exchange_order_id
        )
    except AttributeError:
        # Adapter mein get_order_status nahi hai — fallback: get_orders use karo
        status_result = _fallback_get_order_status(adapter, broker_order.exchange_order_id)
    except Exception as e:
        logger.error(
            "get_order_status error | exchange_id=%s | %s",
            broker_order.exchange_order_id, e,
        )
        return

    if not status_result:
        return

    # ── All adapters (Dhan, Fyers, Zerodha, Delta) dict return karte hain ────
    # DhanAdapter.get_order_status() → {success, status, filled_qty, avg_price, ...}
    if True:
        # ── Dict format (all adapters) ───────────────────────────────────────
        broker_status = status_result.get("status", "").upper()
        filled_qty    = Decimal(str(status_result.get("filled_qty", 0) or 0))
        fill_price    = Decimal(str(status_result.get("avg_price",  0) or 0))

        # Fyers status codes:
        # 1=Cancelled, 2=Traded(filled), 3=For future use, 4=Transit, 5=Rejected, 6=Pending
        is_filled = (
            broker_status in ("TRADED", "FILLED", "COMPLETE", "EXECUTED", "2")
            or status_result.get("status_code") == 2
        )
        is_rejected = (
            broker_status in ("REJECTED", "CANCELLED", "1", "5")
            or status_result.get("status_code") in (1, 5)
        )

    if is_filled and fill_price > 0 and filled_qty > 0:
        process_broker_fill(
            broker_order_id=str(broker_order.id),
            fill_price=fill_price,
            fill_qty=filled_qty,
            broker_response=status_result,
        )
    elif is_rejected:
        _handle_broker_rejection(broker_order, status_result)


def _fallback_get_order_status(adapter, exchange_order_id: str) -> Optional[dict]:
    """
    Agar adapter mein get_order_status nahi hai toh
    get_orders() se dhundo (purane adapters ke liye).
    """
    try:
        orders = adapter.get_orders(status="all")
        for o in orders:
            oid = str(o.get("id") or o.get("orderNumber") or o.get("order_id", ""))
            if oid == str(exchange_order_id):
                return o
    except Exception as e:
        logger.error("fallback_get_order_status error: %s", e)
    return None


# ─────────────────────────────────────────────────────────────────────────────
#  Fill Process karo — Trade banao, Flutter ko push karo
# ─────────────────────────────────────────────────────────────────────────────

def process_broker_fill(
    *,
    broker_order_id: str,
    fill_price: Decimal,
    fill_qty: Decimal,
    broker_response: dict = None,
) -> None:
    """
    Fill notification aa gayi — ab:
    1. BrokerOrder → COMPLETE mark karo
    2. fill_order() → Trade record DB mein banao
    3. Flutter ko WebSocket push karo (trade_update + order_update)

    Idempotent: agar pehle se COMPLETE hai toh safely skip karo.
    Race condition safe: select_for_update() use karo.
    """
    from .models import BrokerOrder
    from apps.orders.services import fill_order, InvalidOrderError
    from apps.websocket.push import push_trade_update, push_order_update, push_notification

    try:
        with transaction.atomic():
            # ✅ Row-level lock — 2 workers ek saath process na karein
            try:
                # ✅ FIX: select_for_update(of=("self",)) — sirf BrokerOrder row lock karo.
                # select_for_update() bina of= ke poore JOIN pe lock lagata hai.
                # PostgreSQL: "FOR UPDATE cannot be applied to nullable side of outer join"
                # order, order__user, order__asset — ye sab nullable FKs hain (SET_NULL).
                # of=("self",) se sirf brokers_brokerorder table lock hota hai — joins free.
                broker_order = (
                    BrokerOrder.objects
                    .select_for_update(nowait=True, of=("self",))
                    .select_related("order__user", "order__asset")
                    .get(id=broker_order_id)
                )
            except BrokerOrder.DoesNotExist:
                logger.error("process_broker_fill: BrokerOrder %s not found", broker_order_id)
                return
            except Exception as lock_err:
                # nowait=True: lock nahi mila toh OperationalError aata hai
                # (dusra worker process kar raha hai) — skip karo
                logger.debug(
                    "process_broker_fill: lock failed | broker_order=%s | %s",
                    broker_order_id, lock_err,
                )
                raise _AlreadyProcessed()

            # ✅ Idempotency check — pehle se process ho chuka hai?
            if broker_order.status == BrokerOrder.Status.COMPLETE:
                logger.info(
                    "process_broker_fill: already COMPLETE | broker_order=%s — skipping",
                    broker_order_id,
                )
                return

            if broker_order.status != BrokerOrder.Status.OPEN:
                logger.warning(
                    "process_broker_fill: unexpected status=%s | broker_order=%s",
                    broker_order.status, broker_order_id,
                )
                return

            order = broker_order.order
            if not order:
                logger.error(
                    "process_broker_fill: no linked Order | broker_order=%s",
                    broker_order_id,
                )
                return

            # ── Step 1: BrokerOrder COMPLETE mark karo ────────────────────
            broker_order.mark_complete(broker_response=broker_response or {})
            logger.info(
                "✅ BrokerOrder COMPLETE | broker_order=%s | exchange_id=%s | "
                "fill_price=%s | fill_qty=%s",
                broker_order_id,
                broker_order.exchange_order_id,
                fill_price,
                fill_qty,
            )

            # ── Step 2: Trade record banao ────────────────────────────────
            try:
                trade = fill_order(
                    order=order,
                    fill_price=fill_price,
                    fill_qty=fill_qty,
                )
                logger.info(
                    "✅ Trade created | trade=%s | order=%s | price=%s | qty=%s | pnl=%s",
                    trade.id, order.id, fill_price, fill_qty, trade.realized_pnl,
                )

                # ── Step 2b: BrokerOrder pe bhi realized_pnl copy karo ───
                # close_session_summary_task is field se direct query karega
                if trade.realized_pnl is not None:
                    broker_order.realized_pnl = trade.realized_pnl
                    broker_order.avg_fill_price = fill_price
                    broker_order.save(update_fields=["realized_pnl", "avg_fill_price"])

                # ── Order model pe realized_pnl sync karo ──
                try:
                    from apps.orders.models import Order
                    linked_order = Order.objects.filter(
                        exchange_order_id=broker_order.exchange_order_id
                    ).first()
                    if linked_order and fill_price and fill_qty:
                        entry = float(linked_order.avg_fill_price or 0)
                        exit_p = float(fill_price)
                        qty = float(fill_qty)
                        if entry > 0 and exit_p > 0 and qty > 0:
                            from decimal import Decimal
                            pnl = Decimal(str(round((exit_p - entry) * qty, 2)))
                            linked_order.realized_pnl = pnl
                            linked_order.status = 'closed'
                            linked_order.exit_price = Decimal(str(exit_p))
                            linked_order.save(update_fields=[
                                'realized_pnl', 'status', 'exit_price', 'updated_at'
                            ])
                            logger.info("Order pnl synced | order=%s | pnl=%s",
                                        linked_order.id, pnl)
                except Exception as e:
                    logger.error("Order pnl sync error | %s", e)

            except InvalidOrderError as e:
                logger.error(
                    "fill_order failed | order=%s | %s", order.id, e
                )
                return

        # ── Step 3: Flutter ko push karo (transaction ke bahar) ───────────
        # ✅ FIX: WebSocket push gracefully fail karo — channel layer nahi mila
        # (test environment, management command, ya Redis down) toh fill complete
        # ho chuka hai, sirf notification nahi jayega. Fill rollback NAHI hoga.
        user_id = order.user.id
        symbol  = order.asset.symbol if order.asset_id else "?"

        try:
            # Trade update — position list mein naya row aayega
            push_trade_update(
                user_id=user_id,
                data={
                    "trade_id":   str(trade.id),
                    "order_id":   str(order.id),
                    "symbol":     symbol,
                    "side":       trade.side,
                    "fill_price": str(fill_price),
                    "fill_qty":   str(fill_qty),
                    "amount":     str(trade.amount),
                    "fee":        str(trade.fee),
                    "mode":       trade.mode,
                    "status":     "filled",
                    "filled_at":  timezone.now().isoformat(),
                },
            )

            # Order update — order card ka status update hoga
            push_order_update(
                user_id=user_id,
                data={
                    "order_id":       str(order.id),
                    "status":         order.status,
                    "filled_qty":     str(order.filled_qty),
                    "avg_fill_price": str(order.avg_fill_price or ""),
                    "mode":           order.mode,
                },
            )

            # In-app notification
            side_label = "BUY" if order.side == "buy" else "SELL"
            push_notification(
                user_id=user_id,
                level="info",
                title=f"✅ Order Filled — {symbol}",
                body=(
                    f"{side_label} {float(fill_qty):.2f} {symbol} "
                    f"@ ₹{float(fill_price):,.2f}"
                ),
            )

            logger.info(
                "✅ Flutter notified | user=%s | symbol=%s | fill=%s@%s",
                user_id, symbol, fill_qty, fill_price,
            )

        except Exception as push_err:
            # WebSocket push failure fill ko cancel nahi karta.
            # Channel layer None (test/management command) ya Redis down — normal hai.
            logger.warning(
                "WebSocket push failed (fill already saved) | user=%s | symbol=%s | err=%s",
                user_id, symbol, push_err,
            )

    except _AlreadyProcessed:
        raise
    except Exception as e:
        logger.error(
            "process_broker_fill FAILED | broker_order=%s | %s",
            broker_order_id, e,
        )
        raise


# ─────────────────────────────────────────────────────────────────────────────
#  Rejection handle karo
# ─────────────────────────────────────────────────────────────────────────────

def _handle_broker_rejection(broker_order, status_result: dict) -> None:
    """Broker ne order reject kar diya — mark karo aur Flutter ko batao."""
    from apps.websocket.push import push_notification, push_order_update

    reason = (
        status_result.get("message")
        or status_result.get("reason")
        or "Broker rejected the order"
    )

    # Pehle se rejected? Skip karo
    if broker_order.status == broker_order.Status.REJECTED:
        return

    broker_order.mark_rejected(reason=reason, broker_response=status_result)
    logger.warning(
        "BrokerOrder REJECTED | broker_order=%s | reason=%s",
        broker_order.id, reason,
    )

    if broker_order.order:
        user_id = broker_order.order.user_id
        symbol  = broker_order.order.asset.symbol if broker_order.order.asset_id else "?"

        try:
            push_order_update(
                user_id=user_id,
                data={
                    "order_id": str(broker_order.order.id),
                    "status":   "rejected",
                    "reason":   reason,
                },
            )
            push_notification(
                user_id=user_id,
                level="error",
                title=f"❌ Order Rejected — {symbol}",
                body=reason,
            )
        except Exception as push_err:
            logger.warning(
                "WebSocket push failed on rejection | user=%s | err=%s",
                user_id, push_err,
            )


# ─────────────────────────────────────────────────────────────────────────────
#  Celery task: ek specific order ka fill check karo (on-demand)
# ─────────────────────────────────────────────────────────────────────────────

@shared_task(
    bind=True,
    name="brokers.check_single_order_fill",
    queue="orders",
    max_retries=3,
    default_retry_delay=5,
)
def check_single_order_fill(self, broker_order_id: str):
    """
    place_broker_order success ke turant baad call karo.
    Market orders usually milliseconds mein fill ho jaate hain —
    yeh task 5s baad check karta hai.

    Usage (apps/brokers/tasks.py mein):
        check_single_order_fill.apply_async(
            args=[str(broker_order.id)],
            countdown=5,   # 5 second baad check karo
        )
    """
    from .models import BrokerOrder

    try:
        broker_order = (
            BrokerOrder.objects
            .select_related("broker_account", "order__asset", "order__user")
            .get(id=broker_order_id)
        )
    except BrokerOrder.DoesNotExist:
        logger.error("check_single_order_fill: not found | %s", broker_order_id)
        return

    if broker_order.status != BrokerOrder.Status.OPEN:
        # Already processed (COMPLETE/REJECTED/etc)
        return

    try:
        _check_and_process_single_order(broker_order)
    except _AlreadyProcessed:
        pass
    except Exception as e:
        logger.error(
            "check_single_order_fill error | %s | %s", broker_order_id, e
        )
        raise self.retry(exc=e)