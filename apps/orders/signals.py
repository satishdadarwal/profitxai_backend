# apps/orders/signals.py
# Auto-create Position when Order is filled

from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import Order, Position
import logging

logger = logging.getLogger(__name__)


@receiver(post_save, sender=Order)
def create_position_on_order_fill(sender, instance, created, **kwargs):
    """
    Automatically create Position when Order is filled.
    
    Triggers when:
    - Order.status changes to 'filled'
    - Order is a BUY order (opening position)
    - No position already exists for this order
    
    Integration with existing flow:
    - Order placed → apps/orders/tasks.py::execute_order
    - Order filled → apps/orders/tasks.py::monitor_order_fill
    - Position created → THIS SIGNAL (automatically)
    - Position updated → apps/orders/tasks.py::update_position_from_order
    """
    
    # Only trigger for filled orders
    if instance.status != Order.Status.FILLED:
        return
    
    # Only for BUY orders (opening positions)
    # SELL orders will close positions via the close_position API
    if instance.side != Order.Side.BUY:
        logger.debug(f"Skipping position creation for SELL order {instance.id}")
        return
    
    # Check if position already exists for THIS order
    existing_position = Position.objects.filter(opening_order=instance).first()
    if existing_position:
        logger.info(f"Position {existing_position.id} already exists for order {instance.id}")
        return

    # DUPLICATE GUARD: same asset+user+mode mein already open position hai?
    duplicate = Position.objects.filter(
        user=instance.user,
        asset=instance.asset,
        mode=instance.mode,
        status="open",
    ).exclude(opening_order=instance).first()
    if duplicate:
        import logging
        logging.getLogger(__name__).warning(
            f"Duplicate position blocked | Asset: {instance.asset.symbol} | "
            f"Existing: {duplicate.id} | Blocked order: {instance.id}"
        )
        return
    
    try:
        # Determine live_signal if order came from live trading
        live_signal = None
        if hasattr(instance, 'strategy') and instance.strategy:
            # Try to find associated LiveSignal
            from apps.live_trading.models import LiveSignal
            live_signal = LiveSignal.objects.filter(
                user=instance.user,
                symbol=instance.asset.symbol,
                status='executed',
                acted_at__isnull=False
            ).order_by('-acted_at').first()
        
        # Create new position
        position = Position.objects.create(
            user=instance.user,
            asset=instance.asset,
            opening_order=instance,
            live_signal=live_signal,
            
            # Position details
            side=instance.side,
            quantity=instance.filled_qty,
            remaining_qty=instance.filled_qty,
            avg_entry_price=instance.avg_fill_price or instance.limit_price,
            current_price=instance.avg_fill_price or instance.limit_price,
            
            # Risk management
            stop_loss=instance.sl_price,
            take_profit=instance.target_price,
            
            # Mode
            mode=instance.mode
        )
        
        logger.info(
            f"✅ Position {position.id} auto-created | "
            f"Order: {instance.id} | "
            f"{position.side.upper()} {position.quantity} {position.symbol} @ {position.avg_entry_price} | "
            f"Mode: {position.mode.upper()}"
        )
        
        # Send WebSocket notification
        _notify_position_created(instance.user.id, position)
        
    except Exception as e:
        logger.error(
            f"❌ Failed to auto-create position for order {instance.id}: {str(e)}",
            exc_info=True
        )


def _notify_position_created(user_id: int, position):
    """Send WebSocket notification when position is created"""
    try:
        from channels.layers import get_channel_layer
        import asyncio
        
        channel_layer = get_channel_layer()
        
        asyncio.get_event_loop().run_until_complete(
            channel_layer.group_send(
                f"user_{user_id}",
                {
                    'type': 'position_created',
                    'data': {
                        'position_id': str(position.id),
                        'symbol': position.symbol,
                        'side': position.side,
                        'quantity': float(position.quantity),
                        'entry_price': float(position.avg_entry_price),
                        'stop_loss': float(position.stop_loss) if position.stop_loss else None,
                        'take_profit': float(position.take_profit) if position.take_profit else None,
                        'mode': position.mode,
                        'message': f'Position opened: {position.side.upper()} {position.quantity} {position.symbol}'
                    }
                }
            )
        )
        
        logger.info(f"📡 Position creation notification sent to user {user_id}")
        
    except Exception as e:
        logger.error(f"Failed to send position creation notification: {e}")