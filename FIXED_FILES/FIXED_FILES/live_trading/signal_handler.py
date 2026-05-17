"""
Fixed Signal Handler for Live Trading
Permanent fix for "No strategies" issue
"""

from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse
from .models import Strategy, LiveTradingSession, TradingSignal
from brokers.models import Broker
import json
import logging

logger = logging.getLogger(__name__)


@login_required
def strategy_list(request):
    """
    List all active strategies for live trading
    FIX: Removed user filter to show all active strategies
    """
    try:
        # FIXED: Show all active strategies without user restriction
        strategies = Strategy.objects.filter(
            is_active=True
        ).select_related('broker').order_by('-created_at')
        
        logger.info(f"Loaded {strategies.count()} active strategies")
        
        context = {
            'strategies': strategies,
            'total_strategies': strategies.count(),
            'page_title': 'Trading Strategies'
        }
        return render(request, 'live_trading/strategy_list.html', context)
        
    except Exception as e:
        logger.error(f"Error loading strategies: {str(e)}")
        messages.error(request, f"Error loading strategies: {str(e)}")
        return render(request, 'live_trading/strategy_list.html', {
            'strategies': [],
            'error': str(e)
        })


@login_required
def get_strategies_api(request):
    """
    API endpoint to fetch strategies for live trading dashboard
    FIX: Returns all active strategies with broker validation
    """
    try:
        # FIXED: Fetch all active strategies without user restriction
        # Also validate that broker is active
        strategies = Strategy.objects.filter(
            is_active=True,
            broker__is_active=True
        ).select_related('broker').values(
            'id', 
            'name', 
            'strategy_type',
            'symbol',
            'timeframe',
            'lot_size',
            'stop_loss',
            'take_profit',
            'broker__name',
            'broker__id',
            'created_at',
            'updated_at'
        ).order_by('-created_at')
        
        strategy_list = list(strategies)
        
        logger.info(f"API returned {len(strategy_list)} strategies")
        
        return JsonResponse({
            'success': True,
            'strategies': strategy_list,
            'count': len(strategy_list),
            'message': f'Found {len(strategy_list)} active strategies'
        })
        
    except Exception as e:
        logger.error(f"API Error: {str(e)}")
        return JsonResponse({
            'success': False,
            'message': str(e),
            'strategies': [],
            'count': 0
        }, status=500)


@login_required
def create_strategy(request):
    """Create a new trading strategy"""
    if request.method == 'POST':
        try:
            # Handle both JSON and form data
            if request.content_type == 'application/json':
                data = json.loads(request.body)
            else:
                data = request.POST
            
            # Validate required fields
            required_fields = ['name', 'strategy_type', 'symbol', 'broker_id']
            for field in required_fields:
                if not data.get(field):
                    raise ValueError(f"Missing required field: {field}")
            
            # Create strategy
            strategy = Strategy.objects.create(
                name=data.get('name'),
                strategy_type=data.get('strategy_type'),
                symbol=data.get('symbol').upper(),
                timeframe=data.get('timeframe', '5m'),
                lot_size=float(data.get('lot_size', 1.0)),
                stop_loss=float(data.get('stop_loss', 0)),
                take_profit=float(data.get('take_profit', 0)),
                broker_id=data.get('broker_id'),
                user=request.user,
                is_active=True  # IMPORTANT: Set active by default
            )
            
            logger.info(f"Strategy created: {strategy.name} (ID: {strategy.id})")
            messages.success(request, f"Strategy '{strategy.name}' created successfully!")
            
            return JsonResponse({
                'success': True,
                'strategy_id': strategy.id,
                'strategy_name': strategy.name,
                'message': 'Strategy created successfully'
            })
            
        except ValueError as e:
            logger.warning(f"Validation error: {str(e)}")
            return JsonResponse({
                'success': False,
                'message': str(e)
            }, status=400)
            
        except Exception as e:
            logger.error(f"Error creating strategy: {str(e)}")
            return JsonResponse({
                'success': False,
                'message': f"Error creating strategy: {str(e)}"
            }, status=500)
    
    # GET request - show form
    brokers = Broker.objects.filter(is_active=True)
    
    # If user wants only their brokers, uncomment:
    # brokers = Broker.objects.filter(is_active=True, user=request.user)
    
    context = {
        'brokers': brokers,
        'strategy_types': ['EMA_CROSSOVER', 'RSI', 'MACD', 'BOLLINGER_BANDS', 'CUSTOM'],
        'timeframes': ['1m', '5m', '15m', '30m', '1h', '4h', '1d']
    }
    return render(request, 'live_trading/create_strategy.html', context)


@login_required
def toggle_strategy(request, strategy_id):
    """Activate or deactivate a strategy"""
    try:
        strategy = get_object_or_404(Strategy, id=strategy_id)
        
        # Toggle the is_active status
        strategy.is_active = not strategy.is_active
        strategy.save()
        
        status = "activated" if strategy.is_active else "deactivated"
        logger.info(f"Strategy {strategy.name} {status}")
        
        messages.success(request, f"Strategy '{strategy.name}' {status} successfully!")
        
        return JsonResponse({
            'success': True,
            'is_active': strategy.is_active,
            'message': f'Strategy {status}',
            'strategy_id': strategy.id
        })
        
    except Strategy.DoesNotExist:
        logger.error(f"Strategy {strategy_id} not found")
        return JsonResponse({
            'success': False,
            'message': 'Strategy not found'
        }, status=404)
        
    except Exception as e:
        logger.error(f"Error toggling strategy: {str(e)}")
        return JsonResponse({
            'success': False,
            'message': str(e)
        }, status=500)


@login_required
def delete_strategy(request, strategy_id):
    """Delete a strategy"""
    if request.method != 'POST':
        return JsonResponse({
            'success': False,
            'message': 'Only POST method allowed'
        }, status=405)
    
    try:
        strategy = get_object_or_404(Strategy, id=strategy_id)
        strategy_name = strategy.name
        
        # Check if strategy has active sessions
        active_sessions = LiveTradingSession.objects.filter(
            strategy=strategy,
            is_active=True
        ).exists()
        
        if active_sessions:
            return JsonResponse({
                'success': False,
                'message': 'Cannot delete strategy with active trading sessions. Stop them first.'
            }, status=400)
        
        strategy.delete()
        logger.info(f"Strategy {strategy_name} deleted")
        
        messages.success(request, f"Strategy '{strategy_name}' deleted successfully!")
        
        return JsonResponse({
            'success': True,
            'message': 'Strategy deleted successfully'
        })
        
    except Strategy.DoesNotExist:
        return JsonResponse({
            'success': False,
            'message': 'Strategy not found'
        }, status=404)
        
    except Exception as e:
        logger.error(f"Error deleting strategy: {str(e)}")
        return JsonResponse({
            'success': False,
            'message': str(e)
        }, status=500)


@login_required
def strategy_details(request, strategy_id):
    """Get detailed information about a strategy"""
    try:
        strategy = get_object_or_404(
            Strategy.objects.select_related('broker', 'user'),
            id=strategy_id
        )
        
        # Get strategy performance stats
        signals = TradingSignal.objects.filter(strategy=strategy)
        total_signals = signals.count()
        
        data = {
            'id': strategy.id,
            'name': strategy.name,
            'strategy_type': strategy.strategy_type,
            'symbol': strategy.symbol,
            'timeframe': strategy.timeframe,
            'lot_size': float(strategy.lot_size),
            'stop_loss': float(strategy.stop_loss),
            'take_profit': float(strategy.take_profit),
            'is_active': strategy.is_active,
            'broker': {
                'id': strategy.broker.id,
                'name': strategy.broker.name
            } if strategy.broker else None,
            'created_at': strategy.created_at.isoformat(),
            'updated_at': strategy.updated_at.isoformat(),
            'total_signals': total_signals
        }
        
        return JsonResponse({
            'success': True,
            'strategy': data
        })
        
    except Strategy.DoesNotExist:
        return JsonResponse({
            'success': False,
            'message': 'Strategy not found'
        }, status=404)
        
    except Exception as e:
        logger.error(f"Error fetching strategy details: {str(e)}")
        return JsonResponse({
            'success': False,
            'message': str(e)
        }, status=500)
