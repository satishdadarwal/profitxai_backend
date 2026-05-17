# apps/orders/position_views.py
# NEW FILE - Position Management Views for Live Trading

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django.shortcuts import get_object_or_404
from django.utils import timezone
from decimal import Decimal
from .models import Position, Order
from .serializers import (
    PositionSerializer,
    PositionCloseSerializer,
    CloseAllPositionsSerializer
)
import logging

logger = logging.getLogger(__name__)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_open_positions(request):
    """
    GET /api/orders/positions/open/
    
    Returns all open positions for the current user.
    Positions are automatically updated with current market prices.
    
    Query Parameters:
    - mode: Filter by 'live' or 'paper' (optional)
    - asset: Filter by asset symbol (optional)
    """
    try:
        # Get base queryset
        positions = Position.objects.filter(
            user=request.user,
            status=Position.Status.OPEN
        ).select_related('asset', 'opening_order', 'live_signal')
        
        # Apply filters
        mode = request.query_params.get('mode')
        if mode and mode in ['live', 'paper']:
            positions = positions.filter(mode=mode)
        
        asset_symbol = request.query_params.get('asset')
        if asset_symbol:
            positions = positions.filter(asset__symbol__iexact=asset_symbol)
        
        # Order by most recent
        positions = positions.order_by('-opened_at')
        
        # TODO: Update current prices from market data
        # for position in positions:
        #     current_price = get_current_price(position.asset.symbol)
        #     position.update_current_price(current_price)
        
        serializer = PositionSerializer(positions, many=True)
        
        # Calculate totals
        total_unrealized_pnl = sum(
            float(p.unrealized_pnl) for p in positions if p.unrealized_pnl
        )
        
        response_data = {
            'success': True,
            'count': positions.count(),
            'positions': serializer.data,
            'summary': {
                'total_positions': positions.count(),
                'total_unrealized_pnl': round(total_unrealized_pnl, 2),
                'live_count': positions.filter(mode=Order.Mode.LIVE).count(),
                'paper_count': positions.filter(mode=Order.Mode.PAPER).count(),
            }
        }
        
        logger.info(f"User {request.user.id} retrieved {positions.count()} open positions")
        return Response(response_data)
        
    except Exception as e:
        logger.error(f"Error fetching open positions: {str(e)}", exc_info=True)
        return Response({
            'success': False,
            'message': f'Error fetching positions: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def close_position(request, position_id):
    """
    POST /api/orders/positions/{position_id}/close/
    
    Close a specific position (fully or partially).
    
    Request Body:
    {
        "close_price": 50000.50,  // Optional: If not provided, uses current market price
        "partial_qty": 0.5        // Optional: For partial close
    }
    
    Response:
    {
        "success": true,
        "message": "Position closed successfully",
        "position": {...},
        "closing_order_id": "uuid",
        "realized_pnl": 1250.50
    }
    """
    try:
        # Get position
        position = get_object_or_404(
            Position,
            id=position_id,
            user=request.user,
            status__in=[Position.Status.OPEN, Position.Status.PARTIAL]
        )
        
        # Validate request data
        serializer = PositionCloseSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        close_price = serializer.validated_data.get('close_price')
        partial_qty = serializer.validated_data.get('partial_qty')
        
        # Get close price
        if not close_price:
            # TODO: Fetch from market data
            close_price = position.current_price or position.avg_entry_price
            logger.info(f"Using current price {close_price} for position {position_id}")
        else:
            close_price = Decimal(str(close_price))
        
        # Validate partial quantity
        if partial_qty:
            partial_qty = Decimal(str(partial_qty))
            if partial_qty > position.remaining_qty:
                return Response({
                    'success': False,
                    'message': f'Partial quantity ({partial_qty}) exceeds remaining quantity ({position.remaining_qty})'
                }, status=status.HTTP_400_BAD_REQUEST)
        
        # Determine close quantity
        close_qty = partial_qty if partial_qty else position.remaining_qty
        
        # Create closing order
        closing_order = Order.objects.create(
            user=request.user,
            asset=position.asset,
            side=Order.Side.SELL if position.side == Order.Side.BUY else Order.Side.BUY,
            order_type=Order.OrderType.MARKET,
            quantity=close_qty,
            status=Order.Status.FILLED,
            avg_fill_price=close_price,
            filled_qty=close_qty,
            mode=position.mode,
            notes=f"Closing position {position.id}"
        )
        
        # Close or partially close position
        is_full_close = close_qty >= position.remaining_qty
        
        if is_full_close:
            position.close_position(close_price, closing_order)
            message = 'Position closed successfully'
        else:
            position.partial_close(close_qty, close_price)
            message = f'Position partially closed ({close_qty} units)'
        
        logger.info(
            f"Position {position_id} {'closed' if is_full_close else 'partially closed'} "
            f"by user {request.user.id}. P&L: {position.realized_pnl}"
        )
        
        return Response({
            'success': True,
            'message': message,
            'position': PositionSerializer(position).data,
            'closing_order_id': str(closing_order.id),
            'realized_pnl': float(position.realized_pnl),
            'is_fully_closed': is_full_close
        })
        
    except Position.DoesNotExist:
        return Response({
            'success': False,
            'message': 'Position not found or already closed'
        }, status=status.HTTP_404_NOT_FOUND)
        
    except Exception as e:
        logger.error(f"Error closing position {position_id}: {str(e)}", exc_info=True)
        return Response({
            'success': False,
            'message': f'Error closing position: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def close_all_positions(request):
    """
    POST /api/orders/positions/close-all/
    
    Close all open positions for the user (with optional filters).
    
    Request Body:
    {
        "asset_ids": ["uuid1", "uuid2"],  // Optional: Close only specific assets
        "mode": "live"                     // Optional: Filter by mode (live/paper)
    }
    
    Response:
    {
        "success": true,
        "message": "Closed 5 positions",
        "closed_count": 5,
        "failed_count": 0,
        "total_pnl": 2500.75,
        "closed_positions": [...],
        "failed_positions": [...]
    }
    """
    try:
        # Validate request data
        serializer = CloseAllPositionsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        # Get all open positions
        positions = Position.objects.filter(
            user=request.user,
            status__in=[Position.Status.OPEN, Position.Status.PARTIAL]
        )
        
        # Apply filters
        asset_ids = serializer.validated_data.get('asset_ids', [])
        if asset_ids:
            positions = positions.filter(asset_id__in=asset_ids)
        
        mode_filter = serializer.validated_data.get('mode')
        if mode_filter:
            positions = positions.filter(mode=mode_filter)
        
        if not positions.exists():
            return Response({
                'success': False,
                'message': 'No open positions found to close'
            }, status=status.HTTP_404_NOT_FOUND)
        
        # Close all positions
        closed_positions = []
        failed_positions = []
        total_pnl = Decimal("0")
        
        for position in positions:
            try:
                # Get current market price
                # TODO: Fetch from market data
                close_price = position.current_price or position.avg_entry_price
                
                # Create closing order
                closing_order = Order.objects.create(
                    user=request.user,
                    asset=position.asset,
                    side=Order.Side.SELL if position.side == Order.Side.BUY else Order.Side.BUY,
                    order_type=Order.OrderType.MARKET,
                    quantity=position.remaining_qty,
                    status=Order.Status.FILLED,
                    avg_fill_price=close_price,
                    filled_qty=position.remaining_qty,
                    mode=position.mode,
                    notes=f"Closing all positions - Position {position.id}"
                )
                
                # Close position
                position.close_position(close_price, closing_order)
                
                closed_positions.append({
                    'position_id': str(position.id),
                    'symbol': position.symbol,
                    'side': position.side,
                    'quantity': float(position.quantity),
                    'entry_price': float(position.avg_entry_price),
                    'close_price': float(close_price),
                    'pnl': float(position.realized_pnl),
                    'order_id': str(closing_order.id)
                })
                
                total_pnl += position.realized_pnl
                
            except Exception as e:
                logger.error(f"Failed to close position {position.id}: {str(e)}")
                failed_positions.append({
                    'position_id': str(position.id),
                    'symbol': position.symbol,
                    'error': str(e)
                })
        
        logger.info(
            f"User {request.user.id} closed {len(closed_positions)} positions. "
            f"Total P&L: {total_pnl}. Failed: {len(failed_positions)}"
        )
        
        return Response({
            'success': True,
            'message': f'Closed {len(closed_positions)} position{"s" if len(closed_positions) != 1 else ""}',
            'closed_count': len(closed_positions),
            'failed_count': len(failed_positions),
            'total_pnl': float(total_pnl),
            'closed_positions': closed_positions,
            'failed_positions': failed_positions if failed_positions else []
        })
        
    except Exception as e:
        logger.error(f"Error closing all positions: {str(e)}", exc_info=True)
        return Response({
            'success': False,
            'message': f'Error closing positions: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)