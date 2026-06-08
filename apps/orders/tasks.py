from celery import shared_task
# apps/orders/tasks.py


import logging
from decimal import Decimal
from typing import Dict, Optional
from datetime import timedelta
 
from celery import Task, current_app as celery_app
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
 
logger = logging.getLogger(__name__)
 
 
class IdempotentOrderTask(Task):
    """
    Base task class for idempotent order operations
    Prevents duplicate order execution
    """
    
    def apply_async(self, args=None, kwargs=None, **options):
        order_id = kwargs.get('order_id')
        if not order_id:
            raise ValueError("order_id is required")
        
        cache_key = f'order_executing:{order_id}'
        
        # Check if already processing
        if cache.get(cache_key):
            logger.warning(f"Order {order_id} is already being processed")
            return None
        
        # Set lock for 60 seconds (should be enough for order execution)
        cache.set(cache_key, True, timeout=60)
        
        try:
            return super().apply_async(args, kwargs, **options)
        except Exception as e:
            # Release lock on error
            cache.delete(cache_key)
            raise
    
    def after_return(self, status, retval, task_id, args, kwargs, einfo):
        """Release idempotency lock after task completes"""
        order_id = kwargs.get('order_id')
        if order_id:
            cache_key = f'order_executing:{order_id}'
            cache.delete(cache_key)
 
 
@celery_app.task(
    bind=True,
    base=IdempotentOrderTask,
    max_retries=3,
    default_retry_delay=5,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=30,
    retry_jitter=True
)
def execute_order(
    self,
    order_id: int,
    user_id: int,
    broker_slug: Optional[str] = None
) -> Dict:
    """
    Execute order with comprehensive error handling
    
    Args:
        order_id: Order ID to execute
        user_id: User ID
        broker_slug: Broker to use (optional, will auto-detect)
    
    Returns:
        Dict with execution result
    
    Raises:
        Retry: On recoverable errors
        Exception: On fatal errors
    """
    from apps.orders.models import Order
    from apps.brokers.models import BrokerAccount
    from apps.risk.manager import RiskManager
    from broker_adapters.registry import BrokerRegistry
    
    logger.info(f"🚀 Executing order {order_id} for user {user_id}")
    
    try:
        # Get order
        order = Order.objects.select_for_update().get(
            id=order_id,
            user_id=user_id
        )
        
        # Check if already executed
        if order.status in ['placed', 'filled', 'cancelled']:
            logger.warning(f"Order {order_id} already in final state: {order.status}")
            return {
                'success': False,
                'message': f'Order already {order.status}'
            }
        
        # Pre-trade risk check
        risk_manager = RiskManager(order.user)
        
        can_trade, risk_reason = risk_manager.can_place_order(
            symbol=order.symbol,
            qty=order.quantity,
            price=order.price,
            stop_loss=getattr(order, 'stop_loss', None),
            take_profit=getattr(order, 'target_price', None),
            side=order.side
        )
        
        if not can_trade:
            logger.warning(f"Risk check failed for order {order_id}: {risk_reason}")
            
            order.status = 'rejected'
            order.error_message = f"Risk check failed: {risk_reason}"
            order.save(update_fields=['status', 'error_message', 'updated_at'])
            
            # Notify user
            _notify_order_failed(user_id, order_id, risk_reason)
            
            return {
                'success': False,
                'message': risk_reason
            }
        
        # Get broker adapter
        if not broker_slug:
            broker_slug = order.broker.broker if hasattr(order, 'broker') else 'fyers'
        
        broker_account = BrokerAccount.objects.filter(
            user_id=user_id,
            broker=broker_slug,
            is_active=True,
            is_verified=True
        ).first()
        
        if not broker_account:
            raise Exception(f"No active broker account found for {broker_slug}")
        
        adapter = BrokerRegistry.make(
            broker_slug,
            {
                'access_token': broker_account.access_token,
                'app_id': broker_account.app_id,
                'secret_key': broker_account.secret_key,
            }
        )
        
        # Place order via broker
        logger.info(f"Placing order {order_id} via {broker_slug}")
        
        result = adapter.place_order(
            symbol=order.symbol,
            side=order.side,
            qty=float(order.quantity),
            order_type=order.order_type.lower(),
            price=float(order.price) if order.price else 0
        )
        
        if not result.success:
            # Order placement failed
            logger.error(f"Broker rejected order {order_id}: {result.message}")
            
            order.status = 'rejected'
            order.error_message = result.message
            order.save(update_fields=['status', 'error_message', 'updated_at'])
            
            # Retry on recoverable errors
            if _is_recoverable_error(result.message):
                raise self.retry(
                    exc=Exception(result.message),
                    countdown=2 ** self.request.retries
                )
            
            _notify_order_failed(user_id, order_id, result.message)
            
            return {
                'success': False,
                'message': result.message
            }
        
        # Order placed successfully
        logger.info(f"✅ Order {order_id} placed: broker_order_id={result.order_id}")
        
        with transaction.atomic():
            order.status = 'placed'
            order.broker_order_id = result.order_id
            order.placed_at = timezone.now()
            order.save(update_fields=[
                'status',
                'broker_order_id',
                'placed_at',
                'updated_at'
            ])
        
        # Notify user
        _notify_order_placed(user_id, order_id, result.order_id)
        
        # Start fill monitoring
        monitor_order_fill.apply_async(
            kwargs={'order_id': order_id},
            countdown=2,  # Check after 2 seconds
            priority=9
        )
        
        return {
            'success': True,
            'broker_order_id': result.order_id,
            'message': 'Order placed successfully'
        }
    
    except Order.DoesNotExist:
        logger.error(f"Order {order_id} not found")
        return {
            'success': False,
            'message': 'Order not found'
        }
    
    except Exception as e:
        logger.error(f"Order execution error: {e}", exc_info=True)
        
        # Update order status
        try:
            order = Order.objects.get(id=order_id)
            order.status = 'failed'
            order.error_message = str(e)
            order.save(update_fields=['status', 'error_message', 'updated_at'])
        except Exception:
            pass
        
        # Retry if not exceeded max retries
        if self.request.retries < self.max_retries:
            raise self.retry(exc=e, countdown=2 ** self.request.retries)
        else:
            # Final failure
            _notify_order_failed(user_id, order_id, str(e))
            raise
 
 
@celery_app.task(
    bind=True,
    max_retries=5,
    default_retry_delay=10
)
def monitor_order_fill(self, order_id: int):
    """
    Monitor order fill status and update accordingly
    Polls every 10-30 seconds until filled or timeout
    """
    from apps.orders.models import Order
    from apps.brokers.models import BrokerAccount
    from broker_adapters.registry import BrokerRegistry
    
    try:
        order = Order.objects.get(id=order_id)
        
        # If already in final state, stop monitoring
        if order.status in ['filled', 'cancelled', 'rejected']:
            logger.info(f"Order {order_id} in final state: {order.status}")
            return
        
        # Get broker adapter
        broker_account = BrokerAccount.objects.filter(
            user=order.user,
            is_active=True
        ).first()
        
        if not broker_account:
            logger.error(f"No broker account for order {order_id}")
            return
        
        adapter = BrokerRegistry.make(
            broker_account.broker,
            {
                'access_token': broker_account.access_token,
                'app_id': broker_account.app_id,
                'secret_key': broker_account.secret_key,
            }
        )
        
        # Get order status from broker
        broker_orders = adapter.get_orders(status='all')
        
        # Find our order
        broker_order = next(
            (o for o in broker_orders if o.get('order_id') == order.broker_order_id),
            None
        )
        
        if not broker_order:
            logger.warning(f"Order {order_id} not found in broker orders")
            
            # Retry
            raise self.retry(countdown=10)
        
        # Update order based on broker status
        broker_status = broker_order.get('status', '').lower()
        
        if 'filled' in broker_status or 'complete' in broker_status:
            # Order filled
            logger.info(f"✅ Order {order_id} filled")
            
            with transaction.atomic():
                order.status = 'filled'
                order.filled_at = timezone.now()
                order.filled_qty = Decimal(str(broker_order.get('filled_qty', 0)))
                order.avg_fill_price = Decimal(str(broker_order.get('avg_price', 0)))
                order.save(update_fields=[
                    'status',
                    'filled_at',
                    'filled_qty',
                    'avg_fill_price',
                    'updated_at'
                ])
            
            # Notify user
            _notify_order_filled(order.user_id, order_id)
            
            # Update position
            update_position_from_order.apply_async(
                kwargs={'order_id': order_id},
                priority=8
            )
        
        elif 'cancelled' in broker_status or 'rejected' in broker_status:
            # Order cancelled/rejected
            logger.info(f"Order {order_id} {broker_status}")
            
            order.status = broker_status
            order.save(update_fields=['status', 'updated_at'])
            
            _notify_order_cancelled(order.user_id, order_id, broker_status)
        
        else:
            # Still pending, retry monitoring
            logger.debug(f"Order {order_id} still {broker_status}, will retry")
            
            # Exponential backoff for monitoring
            countdown = min(10 * (2 ** self.request.retries), 60)
            raise self.retry(countdown=countdown)
    
    except Order.DoesNotExist:
        logger.error(f"Order {order_id} not found")
    
    except Exception as e:
        logger.error(f"Fill monitoring error: {e}", exc_info=True)
        
        # Retry with backoff
        if self.request.retries < self.max_retries:
            raise self.retry(exc=e, countdown=10 * (2 ** self.request.retries))
 
 
@celery_app.task(bind=True, max_retries=3)
def cancel_order(self, order_id: int, user_id: int) -> Dict:
    """
    Cancel an existing order
    """
    from apps.orders.models import Order
    from apps.brokers.models import BrokerAccount
    from broker_adapters.registry import BrokerRegistry
    
    logger.info(f"Cancelling order {order_id} for user {user_id}")
    
    try:
        order = Order.objects.select_for_update().get(
            id=order_id,
            user_id=user_id
        )
        
        # Check if cancellable
        if order.status not in ['pending', 'placed']:
            return {
                'success': False,
                'message': f'Cannot cancel order in {order.status} state'
            }
        
        # Get broker adapter
        broker_account = BrokerAccount.objects.filter(
            user_id=user_id,
            is_active=True
        ).first()
        
        if not broker_account:
            raise Exception("No active broker account")
        
        adapter = BrokerRegistry.make(
            broker_account.broker,
            {
                'access_token': broker_account.access_token,
                'app_id': broker_account.app_id,
                'secret_key': broker_account.secret_key,
            }
        )
        
        # Cancel via broker
        result = adapter.cancel_order(order.broker_order_id)
        
        if result.success:
            order.status = 'cancelled'
            order.save(update_fields=['status', 'updated_at'])
            
            _notify_order_cancelled(user_id, order_id, 'user_cancelled')
            
            logger.info(f"✅ Order {order_id} cancelled")
            
            return {
                'success': True,
                'message': 'Order cancelled'
            }
        else:
            logger.error(f"Failed to cancel order {order_id}: {result.message}")
            
            return {
                'success': False,
                'message': result.message
            }
    
    except Exception as e:
        logger.error(f"Cancel order error: {e}", exc_info=True)
        
        # Retry
        if self.request.retries < self.max_retries:
            raise self.retry(exc=e, countdown=5)
        else:
            raise
 
 
@celery_app.task
def update_position_from_order(order_id: int):
    """
    Update user's position after order fill
    """
    from apps.orders.models import Order, Position
    
    try:
        order = Order.objects.select_related('user').get(id=order_id)
        
        if order.status != 'filled':
            logger.warning(f"Order {order_id} not filled, skipping position update")
            return
        
        # Get or create position
        position, created = Position.objects.get_or_create(
            user=order.user,
            symbol=order.symbol,
            defaults={
                'quantity': 0,
                'avg_price': 0,
                'side': order.side
            }
        )
        
        # Update position
        if order.side == 'buy':
            # Add to position
            total_qty = position.quantity + order.filled_qty
            total_value = (position.quantity * position.avg_price) + (order.filled_qty * order.avg_fill_price)
            position.avg_price = total_value / total_qty if total_qty > 0 else 0
            position.quantity = total_qty
        
        else:  # sell
            # Reduce position
            position.quantity = max(0, position.quantity - order.filled_qty)
            
            # Calculate realized P&L
            if order.filled_qty > 0:
                pnl = (order.avg_fill_price - position.avg_price) * order.filled_qty
                
                # Update order with realized PnL
                order.realized_pnl = pnl
                order.save(update_fields=['realized_pnl', 'updated_at'])
        
        position.last_updated = timezone.now()
        position.save()
        
        logger.info(f"✅ Updated position for {order.symbol}: qty={position.quantity}")
        
        # Notify user of position update
        _notify_position_update(order.user_id, position)
    
    except Exception as e:
        logger.error(f"Position update error: {e}", exc_info=True)
 
 
# ── Helper Functions ──
 
def _is_recoverable_error(error_message: str) -> bool:
    """Check if error is recoverable (should retry)"""
    recoverable_keywords = [
        'timeout',
        'connection',
        'network',
        'temporary',
        'try again',
        'rate limit'
    ]
    
    error_lower = error_message.lower()
    return any(keyword in error_lower for keyword in recoverable_keywords)
 
 
def _notify_order_placed(user_id: int, order_id: int, broker_order_id: str):
    """Notify user that order was placed"""
    try:
        from channels.layers import get_channel_layer
        import asyncio
        
        channel_layer = get_channel_layer()
        
        asyncio.get_event_loop().run_until_complete(
            channel_layer.group_send(
                f"user_{user_id}",
                {
                    'type': 'order_update',
                    'data': {
                        'order_id': order_id,
                        'broker_order_id': broker_order_id,
                        'status': 'placed',
                        'message': 'Order placed successfully'
                    }
                }
            )
        )
    except Exception as e:
        logger.error(f"Failed to send order placed notification: {e}")
 
 
def _notify_order_filled(user_id: int, order_id: int):
    """Notify user that order was filled"""
    try:
        from channels.layers import get_channel_layer
        import asyncio
        
        channel_layer = get_channel_layer()
        
        asyncio.get_event_loop().run_until_complete(
            channel_layer.group_send(
                f"user_{user_id}",
                {
                    'type': 'order_update',
                    'data': {
                        'order_id': order_id,
                        'status': 'filled',
                        'message': 'Order filled'
                    }
                }
            )
        )
    except Exception as e:
        logger.error(f"Failed to send order filled notification: {e}")
 
 
def _notify_order_failed(user_id: int, order_id: int, reason: str):
    """Notify user that order failed"""
    try:
        from channels.layers import get_channel_layer
        import asyncio
        
        channel_layer = get_channel_layer()
        
        asyncio.get_event_loop().run_until_complete(
            channel_layer.group_send(
                f"user_{user_id}",
                {
                    'type': 'order_update',
                    'data': {
                        'order_id': order_id,
                        'status': 'failed',
                        'message': reason
                    }
                }
            )
        )
    except Exception as e:
        logger.error(f"Failed to send order failed notification: {e}")
 
 
def _notify_order_cancelled(user_id: int, order_id: int, reason: str):
    """Notify user that order was cancelled"""
    try:
        from channels.layers import get_channel_layer
        import asyncio
        
        channel_layer = get_channel_layer()
        
        asyncio.get_event_loop().run_until_complete(
            channel_layer.group_send(
                f"user_{user_id}",
                {
                    'type': 'order_update',
                    'data': {
                        'order_id': order_id,
                        'status': 'cancelled',
                        'message': f'Order cancelled: {reason}'
                    }
                }
            )
        )
    except Exception as e:
        logger.error(f"Failed to send order cancelled notification: {e}")
 
 
def _notify_position_update(user_id: int, position):
    """Notify user of position update"""
    try:
        from channels.layers import get_channel_layer
        import asyncio
        
        channel_layer = get_channel_layer()
        
        asyncio.get_event_loop().run_until_complete(
            channel_layer.group_send(
                f"user_{user_id}",
                {
                    'type': 'position_update',
                    'data': {
                        'symbol': position.symbol,
                        'quantity': float(position.quantity),
                        'avg_price': float(position.avg_price),
                        'side': position.side
                    }
                }
            )
        )
    except Exception as e:
        logger.error(f"Failed to send position update: {e}")

@celery_app.task(bind=True, max_retries=2)
def save_daily_pnl_snapshot(self, mode="live", market_type="all"):
    from django.contrib.auth import get_user_model
    from django.utils import timezone
    from apps.orders.models import Trade, Position, DailyPnlSnapshot
    User = get_user_model()
    today = timezone.now().date()
    users = User.objects.filter(is_active=True)
    for user in users:
        try:
            trades_qs = Trade.objects.filter(user=user, created_at__date=today)
            if mode != "all":
                trades_qs = trades_qs.filter(mode=mode)
            if market_type != "all":
                trades_qs = trades_qs.filter(market_type=market_type)
            realised = 0.0
            fees = 0.0
            wins = 0
            losses = 0
            for t in trades_qs:
                pnl = float(t.realized_pnl or 0)
                fee = float(t.fee or 0)
                realised += pnl - fee
                fees += fee
                if pnl > 0:
                    wins += 1
                elif pnl < 0:
                    losses += 1
            pos_qs = Position.objects.filter(user=user, status="open")
            if mode != "all":
                pos_qs = pos_qs.filter(mode=mode)
            unrealised = sum(float(p.unrealized_pnl or 0) for p in pos_qs)
            DailyPnlSnapshot.objects.update_or_create(
                user=user, date=today, mode=mode, market_type=market_type,
                defaults=dict(
                    realised_pnl=round(realised, 4),
                    unrealised_pnl=round(unrealised, 4),
                    fees=round(fees, 4),
                    total_trades=trades_qs.count(),
                    wins=wins,
                    losses=losses,
                )
            )
        except Exception as e:
            import logging
            logging.getLogger(__name__).error("DailyPnlSnapshot error: " + str(e))
    return "Snapshot done"


@shared_task(bind=True, name="orders.sync_fyers_pnl")
def sync_fyers_pnl(self):
    """Fyers se real-time P&L sync karo — har 5 min mein market hours mein."""
    from django.contrib.auth import get_user_model
    from django.utils import timezone
    from apps.brokers.models import BrokerAccount
    from apps.orders.models import DailyPnlSnapshot
    from decimal import Decimal
    import logging
    logger = logging.getLogger(__name__)

    today = timezone.now().date()
    accounts = BrokerAccount.objects.filter(
        broker="fyers", is_active=True
    ).select_related("user")

    for account in accounts:
        try:
            from fyers_apiv3 import fyersModel
            fyers = fyersModel.FyersModel(
                client_id=account.app_id,
                token=account.access_token,
                is_async=False, log_path=""
            )
            positions = fyers.positions()
            if positions.get("s") != "ok":
                continue
            overall = positions.get("overall", {})
            realized = Decimal(str(overall.get("pl_realized", 0)))
            unrealized = Decimal(str(overall.get("pl_unrealized", 0)))
            total = realized + unrealized

            DailyPnlSnapshot.objects.update_or_create(
                user=account.user,
                date=today,
                mode="live",
                defaults={
                    "realised_pnl": realized,
                    "unrealised_pnl": unrealized,
                    "total_pnl": total,
                    "total_trades": overall.get("count_total", 0),
                    "win_count": sum(
                        1 for p in positions.get("netPositions", [])
                        if p.get("realized_profit", 0) > 0
                    ),
                }
            )
            logger.info("Fyers P&L synced | user=%s | realized=%.2f | unrealized=%.2f",
                       account.user.email, realized, unrealized)
        except Exception as e:
            logger.warning("Fyers P&L sync failed | user=%s | err=%s", account.user.email, e)

    return "P&L synced"


@shared_task(bind=True, name="orders.sync_delta_pnl")
def sync_delta_pnl(self):
    """Delta Exchange se real-time P&L sync karo — 24x7."""
    from django.contrib.auth import get_user_model
    from django.utils import timezone
    from apps.brokers.models import BrokerAccount
    from apps.orders.models import DailyPnlSnapshot
    from decimal import Decimal
    import logging
    logger = logging.getLogger(__name__)

    today = timezone.now().date()
    accounts = BrokerAccount.objects.filter(
        broker="delta", is_active=True, is_verified=True
    ).select_related("user")

    for account in accounts:
        try:
            from apps.websocket.delta_feed import _delta_get_wallet
            wallet = _delta_get_wallet(account)
            if not wallet:
                continue

            # Delta positions fetch
            from apps.strategies.signal_router import _delta_sign_and_post
            positions_resp = _delta_sign_and_post(
                account.api_key, account.api_secret,
                "/v2/positions/margined", {}, method="GET"
            )
            positions = positions_resp.get("result", [])
            realized = Decimal("0")
            unrealized = Decimal("0")

            for pos in positions:
                realized   += Decimal(str(pos.get("realized_pnl", 0)))
                unrealized += Decimal(str(pos.get("unrealized_pnl", 0)))

            DailyPnlSnapshot.objects.update_or_create(
                user=account.user,
                date=today,
                mode="live",
                defaults={
                    "realized_pnl":   realized,
                    "unrealised_pnl": unrealized,
                    "total_pnl":      realized + unrealized,
                    "trade_count":    len(positions),
                    "wins":           sum(1 for p in positions if float(p.get("realized_pnl", 0)) > 0),
                }
            )
            logger.info("Delta P&L synced | user=%s | realized=%.2f", account.user.email, realized)
        except Exception as e:
            logger.warning("Delta P&L sync failed | user=%s | err=%s", account.user.email, e)

    return "Delta P&L synced"


@shared_task(bind=True, name="orders.sync_fyers_tradebook")
def sync_fyers_tradebook(self):
    """Fyers tradebook se aaj ke trades OptionTrade mein save karo."""
    from django.utils import timezone
    from apps.brokers.models import BrokerAccount
    from apps.options.models import OptionTrade
    from decimal import Decimal
    import logging
    logger = logging.getLogger(__name__)

    today = timezone.now().date()
    accounts = BrokerAccount.objects.filter(
        broker="fyers", is_active=True
    ).select_related("user")

    for account in accounts:
        try:
            from fyers_apiv3 import fyersModel
            fyers = fyersModel.FyersModel(
                client_id=account.app_id,
                token=account.access_token,
                is_async=False, log_path=""
            )
            tb = fyers.tradebook()
            if tb.get("s") != "ok":
                continue

            trades = tb.get("tradeBook", [])
            saved = 0
            for t in trades:
                order_no = t.get("orderNumber", "")
                if not order_no:
                    continue
                if Order.objects.filter(exchange_order_id=order_no).exists():
                    continue

                symbol = t.get("symbol", "")
                action = "buy" if t.get("side", 1) == 1 else "sell"
                qty = int(t.get("tradedQty", 0))
                price = Decimal(str(t.get("tradePrice", 0)))
                option_type = "CE" if symbol.endswith("CE") else "PE" if symbol.endswith("PE") else ""

                from apps.orders.models import Order
                from apps.market.models import Asset
                # Symbol se underlying extract
                u = next((n for n in ["BANKNIFTY","MIDCPNIFTY","FINNIFTY","SENSEX","NIFTY"] if n in symbol), "NIFTY")
                asset = Asset.objects.filter(symbol=u).first()
                Order.objects.create(
                    user=account.user,
                    asset=asset,
                    side=action,
                    quantity=qty,
                    avg_fill_price=price,
                    status="closed" if action == "sell" else "open",
                    mode="live",
                    order_type="market",
                    execution_status="filled",
                    notes=symbol,
                    exchange_order_id=order_no,
                )
                saved += 1

            logger.info("Fyers tradebook synced | user=%s | saved=%d", account.user.email, saved)
        except Exception as e:
            logger.warning("Fyers tradebook sync failed | user=%s | err=%s", account.user.email, e)

    return "Tradebook synced"
