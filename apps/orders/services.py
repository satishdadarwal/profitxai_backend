# apps/orders/services.py
#
#  Pure business logic — no HTTP, no Celery here.
#  Views aur tasks dono yahan se call karte hain.
#
#  FIXES:
#  ✅ create_order() mein place_broker_order.delay() add kiya
#     → Ab Fyers pe actual order jayega (pehle sirf DB mein save hota tha)

import logging
from decimal import Decimal
from typing import Optional

from django.contrib.auth import get_user_model
from django.db import transaction as db_transaction
from django.utils import timezone

from apps.market.models import Asset
from apps.wallet.models import Transaction, Wallet
from apps.websocket.push import (
    push_balance_update,
    push_order_update,
    push_trade_update,
)

from .models import Order

User = get_user_model()
logger = logging.getLogger(__name__)

# Trading fee — 0.1 % maker/taker (override in settings as needed)
FEE_RATE = Decimal("0.001")


# ─────────────────────────────────────────────────────────────────
#  Exceptions
# ─────────────────────────────────────────────────────────────────
class OrderError(Exception):
    """Base class for all order-related errors."""


class InsufficientFundsError(OrderError):
    pass


class AssetDisabledError(OrderError):
    pass


class InvalidOrderError(OrderError):
    pass


# ─────────────────────────────────────────────────────────────────
#  1. Place Order
# ─────────────────────────────────────────────────────────────────
def place_order(
    *,
    user,
    asset_symbol: str,
    side: str,
    order_type: str,
    quantity: Decimal,
    limit_price: Optional[Decimal] = None,
    stop_price: Optional[Decimal] = None,
    mode: str = Order.Mode.LIVE,
    notes: str = "",
    strategy=None,
    broker_account=None,
) -> Order:
    """
    Order validate karke DB mein save karta hai.
    LIVE mode mein wallet balance check hota hai.
    Paper mode mein sirf DB record banta hai.

    Raises:
        AssetDisabledError     — asset trading band hai
        InvalidOrderError      — limit order mein price missing
        InsufficientFundsError — wallet mein paisa nahi
    """
    asset = _get_active_asset(asset_symbol)

    # ── Validation ───────────────────────────────────────────────
    if order_type == Order.OrderType.LIMIT and limit_price is None:
        raise InvalidOrderError("Limit order requires limit_price.")

    if order_type == Order.OrderType.STOP and stop_price is None:
        raise InvalidOrderError("Stop order requires stop_price.")

    if quantity <= 0:
        raise InvalidOrderError("Quantity must be positive.")

    with db_transaction.atomic():
        if mode == Order.Mode.LIVE:
            sym_upper = asset_symbol.upper()
            is_derivative = any(x in sym_upper for x in ['CE', 'PE', 'FUT', 'INDEX'])
            if not is_derivative:
                _check_and_lock_funds(
                    user=user,
                    asset=asset,
                    side=side,
                    quantity=quantity,
                    price=limit_price or asset.last_price,
                )

        order = Order.objects.create(
            user=user,
            asset=asset,
            side=side,
            order_type=order_type,
            status=Order.Status.OPEN,
            mode=mode,
            quantity=quantity,
            limit_price=limit_price,
            stop_price=stop_price,
            notes=notes,
            strategy=strategy,
            broker_account=broker_account,
            symbol_display=asset_symbol or "",
        )

    logger.info(
        "Order placed | id=%s | user=%s | %s %s %s",
        order.id,
        user.id,
        mode,
        side,
        asset_symbol,
    )

    # ── WebSocket push ───────────────────────────────────────────
    _push_order(order)

    # ── Trigger fill for market orders immediately ────────────────
    if order_type == Order.OrderType.MARKET:
        fill_order(order=order, fill_price=asset.last_price, fill_qty=quantity)

    return order


# ─────────────────────────────────────────────────────────────────
#  create_order — used by strategies/services.py and signal_router.py
# ─────────────────────────────────────────────────────────────────
def create_order(
    *,
    strategy,
    symbol: str,
    side: str,
    quantity: int,
    price: Decimal,
    sl_price: Optional[Decimal] = None,
    target_price: Optional[Decimal] = None,
    instrument_type: str = "equity",
    option_type: str = "",
    broker=None,
    exchange_order_id: str = "",
    broker_response: dict = None,
    mode: str = "paper",
    client_order_id: Optional[str] = None,
    user=None,  # override strategy.user — subscriber ke liye
) -> Order:
    """
    Strategy-friendly wrapper around place_order().
    signal_router.py aur strategies/services.py dono yahan se call karte hain.

    ✅ FIX #1: Idempotency — agar same client_order_id dobara aaye (Flutter retry)
               toh naya order nahi banega. Pehle wala order return hoga.
    ✅ FIX #2: LIVE mode mein BrokerOrder create karke
               place_broker_order.delay() trigger karta hai
               → Fyers pe actual order jata hai ab
    """
    # ── ✅ IDEMPOTENCY CHECK ─────────────────────────────────────
    # Flutter retry pe duplicate order mat banao.
    # Agar same client_order_id dobara aaye toh pehle wala return karo.
    if client_order_id:
        existing = Order.objects.filter(client_order_id=client_order_id).first()
        if existing:
            logger.warning(
                "⚠️  Duplicate order blocked | client_order_id=%s | "
                "existing=%s | user=%s",
                client_order_id, existing.id, strategy.user.id,
            )
            return existing

    order = place_order(
        user=user or strategy.user,
        asset_symbol=symbol,
        side=side,
        order_type=Order.OrderType.MARKET,
        quantity=Decimal(str(quantity)),
        limit_price=price,
        mode=Order.Mode.LIVE if mode == "live" else Order.Mode.PAPER,
        notes=f"strategy={strategy.id} | instrument={instrument_type}",
        strategy=strategy,
        broker_account=broker,
    )

    # ── Save idempotency key on the newly created order ─────────
    if client_order_id:
        order.client_order_id = client_order_id
        order.save(update_fields=["client_order_id"])

    # ── Store SL / TP ─────────────────────────────────────────────
    update_fields = []
    if sl_price is not None:
        order.sl_price = sl_price
        update_fields.append("sl_price")
    if target_price is not None:
        order.target_price = target_price
        update_fields.append("target_price")
    if instrument_type:
        order.instrument_type = instrument_type
        update_fields.append("instrument_type")
    if option_type:
        order.option_type = option_type
        update_fields.append("option_type")

    # ── Broker execution result (agar caller ne already place kiya ho) ──
    if exchange_order_id:
        order.exchange_order_id = exchange_order_id
        order.execution_status = Order.ExecutionStatus.ACCEPTED
        update_fields += ["exchange_order_id", "execution_status"]

    if broker_response is not None:
        order.broker_response = broker_response
        update_fields.append("broker_response")

    if update_fields:
        order.save(update_fields=update_fields + ["updated_at"])

    # ── ✅ FIX: BrokerOrder + Celery task — Fyers pe order bhejo ──
    if broker is not None and mode == "live":
        broker_order = _create_broker_order(
            order=order,
            broker=broker,
            exchange_order_id=exchange_order_id,
            broker_response=broker_response,
        )

        # ✅ Yahi missing tha — Celery task trigger karo
        # broker_order.id → tasks.py → adapter.place_order() → Fyers API
        if broker_order is not None:
            try:
                from apps.brokers.tasks import place_broker_order
                place_broker_order.delay(str(broker_order.id))
                logger.info(
                    "✅ Celery task dispatched | broker_order=%s | symbol=%s | side=%s",
                    broker_order.id, symbol, side,
                )
            except Exception as e:
                logger.error(
                    "❌ Celery dispatch failed | broker_order=%s | err=%s",
                    broker_order.id if broker_order else "N/A", e,
                )

    logger.info(
        "create_order | strategy=%s | %s %s @ %s | sl=%s tp=%s | mode=%s",
        strategy.id,
        side.upper(),
        symbol,
        price,
        sl_price,
        target_price,
        mode,
    )
    return order


# ─────────────────────────────────────────────────────────────────
#  Private: BrokerOrder create karo
# ─────────────────────────────────────────────────────────────────
def _create_broker_order(
    *,
    order: Order,
    broker,
    exchange_order_id: str = "",
    broker_response: dict = None,
):
    """
    BrokerOrder DB record banao.
    Celery task isko pick karke Fyers API pe bhejta hai.
    """
    try:
        from apps.brokers.models import BrokerOrder

        broker_order = BrokerOrder.objects.create(
            broker_account=broker,
            order=order,
            order_type=BrokerOrder.OrderType.ENTRY,
            status=(
                BrokerOrder.Status.OPEN      # already placed by caller
                if exchange_order_id
                else BrokerOrder.Status.PENDING  # Celery bhejega
            ),
            exchange_order_id=exchange_order_id or "",
            broker_response=broker_response or {},
        )
        logger.info(
            "BrokerOrder created | id=%s | status=%s | order=%s",
            broker_order.id, broker_order.status, order.id,
        )
        return broker_order

    except Exception as e:
        logger.error(
            "BrokerOrder creation failed | order=%s | err=%s", order.id, e
        )
        return None


# ─────────────────────────────────────────────────────────────────
#  2. Fill Order  (called by matching engine / Celery task)
# ─────────────────────────────────────────────────────────────────
def fill_order(
    *, order: Order, fill_price: Decimal, fill_qty: Optional[Decimal] = None
) -> Order:
    """
    Order ko fully ya partially fill karta hai aur Order record update karta hai.

    realized_pnl logic:
    - BUY fill  → entry trade hai, PnL abhi realize nahi hua → None
    - SELL fill → exit trade hai, entry price se PnL calculate karo:
        pnl = (sell_price - avg_entry_price) * qty - fees
    """
    fill_qty = fill_qty or order.remaining_qty

    if fill_qty <= 0 or fill_qty > order.remaining_qty:
        raise InvalidOrderError(
            f"Invalid fill_qty={fill_qty}, remaining={order.remaining_qty}"
        )

    amount = (fill_qty * fill_price).quantize(Decimal("0.00000001"))
    fee = (amount * FEE_RATE).quantize(Decimal("0.00000001"))

    realized_pnl = _calculate_realized_pnl(
        order=order,
        fill_price=fill_price,
        fill_qty=fill_qty,
        fee=fee,
    )

    with db_transaction.atomic():
        order.filled_qty += fill_qty
        order.avg_fill_price = _weighted_avg_price(order, fill_price, fill_qty)
        if realized_pnl is not None:
            order.realized_pnl = realized_pnl

        if order.filled_qty >= order.quantity:
            order.status = Order.Status.FILLED
        else:
            order.status = Order.Status.PARTIAL

        order.save(
            update_fields=["filled_qty", "avg_fill_price", "status", "realized_pnl", "updated_at"]
        )

        if order.mode == Order.Mode.LIVE:
            _settle_wallet(order=order, amount=amount, fee=fee)

        if realized_pnl is not None:
            from django.core.cache import cache
            cache.delete(f"daily_pnl:{order.user_id}:{order.updated_at.date()}")

    logger.info(
        "Order filled | order=%s | qty=%s @ %s | realized_pnl=%s",
        order.id, fill_qty, fill_price, realized_pnl,
    )

    _push_order(order)
    _push_order_fill(order=order, fill_price=fill_price, amount=amount, fee=fee)

    return order


# ─────────────────────────────────────────────────────────────────
#  3. Cancel Order
# ─────────────────────────────────────────────────────────────────
def cancel_order(*, order: Order, reason: str = "") -> Order:
    """
    Open/partial order cancel karta hai aur locked funds return karta hai.
    """
    if order.status not in (Order.Status.OPEN, Order.Status.PARTIAL):
        raise InvalidOrderError(f"Cannot cancel order with status={order.status}")

    with db_transaction.atomic():
        order.status = Order.Status.CANCELLED
        order.notes = f"Cancelled: {reason}" if reason else order.notes
        order.save(update_fields=["status", "notes", "updated_at"])

        if order.mode == Order.Mode.LIVE:
            _unlock_remaining_funds(order)

    logger.info("Order cancelled | id=%s | reason=%s", order.id, reason)
    _push_order(order)
    return order


# ─────────────────────────────────────────────────────────────────
#  Paper Trading  (no real wallet interaction)
# ─────────────────────────────────────────────────────────────────
def place_paper_order(
    *,
    user,
    asset_symbol: str,
    side: str,
    order_type: str,
    quantity: Decimal,
    limit_price: Optional[Decimal] = None,
) -> Order:
    """Shortcut — mode=PAPER force karta hai."""
    return place_order(
        user=user,
        asset_symbol=asset_symbol,
        side=side,
        order_type=order_type,
        quantity=quantity,
        limit_price=limit_price,
        mode=Order.Mode.PAPER,
    )


# ─────────────────────────────────────────────────────────────────
#  Private helpers
# ─────────────────────────────────────────────────────────────────
def _get_active_asset(symbol: str) -> Asset:
    try:
        asset = Asset.objects.get(symbol__iexact=symbol)
    except Asset.DoesNotExist:
        # Options/futures symbol auto-create karo
        from apps.market.models import Asset as MarketAsset
        asset, _ = MarketAsset.objects.get_or_create(
            symbol=symbol.upper(),
            defaults={
                "name": symbol.upper(),
                "asset_type": "option" if any(x in symbol.upper() for x in ["CE","PE"]) else "equity",
                "is_active": True,
            }
        )
    if not asset.is_active:
        raise AssetDisabledError(f"Trading for {symbol} is currently disabled.")
    return asset


def _check_and_lock_funds(
    *, user, asset: Asset, side: str, quantity: Decimal, price: Decimal
):
    """BUY: INR lock; SELL: asset lock."""
    wallet = Wallet.objects.select_for_update().get(user=user)

    if side == Order.Side.BUY:
        required = (quantity * price * (1 + FEE_RATE)).quantize(Decimal("0.01"))
        if wallet.available_balance < required:
            raise InsufficientFundsError(
                f"Need {required} INR, available {wallet.available_balance}."
            )
        wallet.available_balance -= required
        wallet.locked_balance += required
    else:  # SELL
        sym_upper = asset.symbol.upper()
        is_derivative = any(x in sym_upper for x in ['CE', 'PE', 'FUT'])
        if is_derivative:
            wallet.save(update_fields=['available_balance', 'locked_balance', 'updated_at'])
            return
        asset_wallet = Wallet.objects.select_for_update().get(
            user=user, currency=asset.symbol
        )
        if asset_wallet.available_balance < quantity:
            raise InsufficientFundsError(
                f"Need {quantity} {asset.symbol}, available {asset_wallet.available_balance}."
            )
        asset_wallet.available_balance -= quantity
        asset_wallet.locked_balance += quantity
        asset_wallet.save(update_fields=["available_balance", "locked_balance"])
        return

    wallet.save(update_fields=["available_balance", "locked_balance"])


def _settle_wallet(*, order: Order, amount: Decimal, fee: Decimal):
    wallet = Wallet.objects.select_for_update().get(user=order.user)

    if order.side == Order.Side.BUY:
        wallet.locked_balance -= amount + fee
    else:
        wallet.available_balance += amount - fee
        wallet.locked_balance -= amount

    wallet.save(update_fields=["available_balance", "locked_balance"])

    Transaction.objects.create(
        wallet=wallet,
        transaction_type="trade_settlement",
        amount=amount,
        fee=fee,
        reference=str(order.id),
    )


def _unlock_remaining_funds(order: Order):
    """Cancelled order ke locked funds wapas karo."""
    wallet = Wallet.objects.select_for_update().get(user=order.user)
    remaining_value = order.remaining_qty * (order.limit_price or Decimal("0"))
    release = min(remaining_value, wallet.locked_balance)
    wallet.locked_balance -= release
    wallet.available_balance += release
    wallet.save(update_fields=["available_balance", "locked_balance"])


def _weighted_avg_price(order: Order, new_price: Decimal, new_qty: Decimal) -> Decimal:
    prev_value = (order.avg_fill_price or Decimal("0")) * order.filled_qty
    new_value = new_price * new_qty
    total_qty = order.filled_qty + new_qty
    if total_qty == 0:
        return new_price
    return ((prev_value + new_value) / total_qty).quantize(Decimal("0.00000001"))


def _calculate_realized_pnl(
    *, order: Order, fill_price: Decimal, fill_qty: Decimal, fee: Decimal
) -> Optional[Decimal]:
    """
    Trade ka realized PnL calculate karo.

    Rules:
    - BUY fill  → entry position hai, PnL realize nahi hua → None store karo.
    - SELL fill → exit fill hai, paired BUY ka avg_fill_price se PnL nikalo:
        pnl = (sell_price - avg_buy_price) * qty - fees

    Indian market (NSE futures/options) ke liye:
    - "buy" = long entry ya short exit
    - "sell" = short entry ya long exit
    - Hum sirf simplest case handle karte hain:
        SELL fill jab SAME user ke paas koi filled BUY order tha → realized PnL.
    - Complex cases (short entry, spread) → None (position model handle karega).

    Return: Decimal (PnL in INR) ya None agar calculate nahi ho saka.
    """
    try:
        # BUY fill = entry, PnL realize nahi hua abhi
        if order.side == Order.Side.BUY:
            return None

        # SELL fill = exit — same asset pe user ka most recent filled BUY dhundo
        # jo abhi bhi open/partially filled hai
        paired_buy = (
            Order.objects.filter(
                user=order.user,
                asset=order.asset,
                side=Order.Side.BUY,
                status__in=[Order.Status.FILLED, Order.Status.PARTIAL],
                mode=order.mode,
            )
            .exclude(avg_fill_price=None)
            .order_by("-updated_at")
            .first()
        )

        if not paired_buy or not paired_buy.avg_fill_price:
            # Paired buy nahi mila (e.g. fresh short) — PnL position model karega
            return None

        avg_entry = paired_buy.avg_fill_price
        gross_pnl = (fill_price - avg_entry) * fill_qty
        net_pnl = (gross_pnl - fee).quantize(Decimal("0.01"))

        logger.info(
            "_calculate_realized_pnl: sell=%s qty=%s | entry=%s exit=%s "
            "gross=%s fee=%s net=%s",
            order.id, fill_qty, avg_entry, fill_price, gross_pnl, fee, net_pnl,
        )
        return net_pnl

    except Exception as exc:
        logger.error(
            "_calculate_realized_pnl error | order=%s | %s", order.id, exc
        )
        return None  # fail gracefully — trade record banta rahega, PnL null hoga


# ── WebSocket helpers ────────────────────────────────────────────
def _push_order(order: Order):
    push_order_update(
        user_id=order.user.pk,
        data={
            "order_id": str(order.id),
            "status": order.status,
            "filled_qty": str(order.filled_qty),
            "avg_price": str(order.avg_fill_price or ""),
            "mode": order.mode,
        },
    )


def _push_order_fill(*, order: Order, fill_price: Decimal, amount: Decimal, fee: Decimal):
    push_trade_update(
        user_id=order.user.pk,
        data={
            "trade_id": str(order.id),
            "symbol": order.asset.symbol if order.asset_id else order.symbol_display,
            "side": order.side,
            "price": str(fill_price),
            "amount": str(amount),
            "fee": str(fee),
            "mode": order.mode,
        },
    )