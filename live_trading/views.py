"""
Fixed Views for Live Trading Dashboard
Permanent fix for strategy display issues
"""

from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.db.models import Count, Sum, Q, Avg
from django.utils import timezone
from datetime import datetime, timedelta
from .models import Strategy, LiveTradingSession, TradingSignal
from brokers.models import Broker
import logging

logger = logging.getLogger(__name__)


@login_required
def live_trading_dashboard(request):
    """
    Main live trading dashboard with strategy display
    FIX: Shows all active strategies without user restrictions
    """
    try:
        # FIXED: Get all active strategies without user filter
        strategies = Strategy.objects.filter(
            is_active=True
        ).select_related('broker').prefetch_related('signals').order_by('-created_at')
        
        logger.info(f"Dashboard loading {strategies.count()} strategies")
        
        # Get active brokers
        brokers = Broker.objects.filter(is_active=True).order_by('name')
        
        # Get recent signals (last 20)
        recent_signals = TradingSignal.objects.select_related(
            'strategy', 'session'
        ).order_by('-created_at')[:20]
        
        # Get active trading sessions
        active_sessions = LiveTradingSession.objects.filter(
            is_active=True
        ).select_related('strategy', 'broker').order_by('-started_at')
        
        # Calculate statistics
        today = timezone.now().date()
        
        total_strategies = strategies.count()
        active_strategies_count = strategies.filter(is_active=True).count()
        
        total_signals_today = TradingSignal.objects.filter(
            created_at__date=today
        ).count()
        
        total_sessions = LiveTradingSession.objects.count()
        active_sessions_count = active_sessions.count()
        
        # Get signal statistics
        signals_stats = TradingSignal.objects.filter(
            created_at__gte=timezone.now() - timedelta(days=7)
        ).aggregate(
            total=Count('id'),
            buy=Count('id', filter=Q(signal_type='BUY')),
            sell=Count('id', filter=Q(signal_type='SELL'))
        )
        
        context = {
            # Main data
            'strategies': strategies,
            'brokers': brokers,
            'recent_signals': recent_signals,
            'active_sessions': active_sessions,
            
            # Statistics
            'total_strategies': total_strategies,
            'active_strategies': active_strategies_count,
            'total_signals_today': total_signals_today,
            'total_sessions': total_sessions,
            'active_sessions_count': active_sessions_count,
            'signals_stats': signals_stats,
            
            # Metadata
            'page_title': 'Live Trading Dashboard',
            'current_time': timezone.now(),
            
            # Debug flag (remove in production)
            'debug_mode': True
        }
        
        return render(request, 'live_trading/dashboard.html', context)
        
    except Exception as e:
        logger.error(f"Dashboard Error: {str(e)}", exc_info=True)
        
        # Return dashboard with error message
        return render(request, 'live_trading/dashboard.html', {
            'strategies': [],
            'brokers': [],
            'recent_signals': [],
            'active_sessions': [],
            'error_message': f"Error loading dashboard: {str(e)}",
            'total_strategies': 0,
            'active_strategies': 0
        })


@login_required
def refresh_strategies(request):
    """
    API endpoint to refresh strategies list
    Used by AJAX calls from frontend
    """
    try:
        # FIXED: Fetch all active strategies
        strategies = Strategy.objects.filter(
            is_active=True,
            broker__is_active=True  # Also check broker is active
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
            'is_active',
            'created_at',
            'updated_at'
        ).order_by('-created_at')
        
        strategy_list = list(strategies)
        
        logger.info(f"Refresh API returned {len(strategy_list)} strategies")
        
        return JsonResponse({
            'success': True,
            'strategies': strategy_list,
            'count': len(strategy_list),
            'timestamp': timezone.now().isoformat(),
            'message': f'Loaded {len(strategy_list)} active strategies'
        })
        
    except Exception as e:
        logger.error(f"Refresh Error: {str(e)}")
        return JsonResponse({
            'success': False,
            'message': str(e),
            'strategies': [],
            'count': 0
        }, status=500)


@login_required
def start_trading_session(request):
    """Start a new live trading session"""
    if request.method != 'POST':
        return JsonResponse({
            'success': False,
            'message': 'Only POST method allowed'
        }, status=405)
    
    try:
        import json
        data = json.loads(request.body) if request.body else request.POST
        
        strategy_id = data.get('strategy_id')
        broker_id = data.get('broker_id')
        
        if not strategy_id:
            return JsonResponse({
                'success': False,
                'message': 'Strategy ID is required'
            }, status=400)
        
        # Get strategy
        strategy = Strategy.objects.get(id=strategy_id, is_active=True)
        
        # Get broker
        if broker_id:
            broker = Broker.objects.get(id=broker_id, is_active=True)
        else:
            broker = strategy.broker
        
        # Check if session already exists
        existing_session = LiveTradingSession.objects.filter(
            strategy=strategy,
            broker=broker,
            is_active=True
        ).first()
        
        if existing_session:
            return JsonResponse({
                'success': False,
                'message': f'Active session already exists for this strategy',
                'session_id': existing_session.id
            }, status=400)
        
        # Create new session
        session = LiveTradingSession.objects.create(
            strategy=strategy,
            broker=broker,
            user=request.user,
            started_at=timezone.now(),
            is_active=True
        )
        
        logger.info(f"Trading session started: {session.id} for strategy {strategy.name}")
        
        return JsonResponse({
            'success': True,
            'session_id': session.id,
            'strategy_name': strategy.name,
            'broker_name': broker.name,
            'message': 'Trading session started successfully'
        })
        
    except Strategy.DoesNotExist:
        return JsonResponse({
            'success': False,
            'message': 'Strategy not found or inactive'
        }, status=404)
        
    except Broker.DoesNotExist:
        return JsonResponse({
            'success': False,
            'message': 'Broker not found or inactive'
        }, status=404)
        
    except Exception as e:
        logger.error(f"Error starting session: {str(e)}")
        return JsonResponse({
            'success': False,
            'message': str(e)
        }, status=500)


@login_required
def stop_trading_session(request, session_id):
    """Stop an active trading session"""
    if request.method != 'POST':
        return JsonResponse({
            'success': False,
            'message': 'Only POST method allowed'
        }, status=405)
    
    try:
        session = LiveTradingSession.objects.get(id=session_id, is_active=True)
        
        # Stop the session
        session.is_active = False
        session.ended_at = timezone.now()
        session.save()
        
        logger.info(f"Trading session stopped: {session.id}")
        
        return JsonResponse({
            'success': True,
            'session_id': session.id,
            'message': 'Trading session stopped successfully'
        })
        
    except LiveTradingSession.DoesNotExist:
        return JsonResponse({
            'success': False,
            'message': 'Session not found or already stopped'
        }, status=404)
        
    except Exception as e:
        logger.error(f"Error stopping session: {str(e)}")
        return JsonResponse({
            'success': False,
            'message': str(e)
        }, status=500)


@login_required
def get_dashboard_stats(request):
    """Get real-time dashboard statistics"""
    try:
        today = timezone.now().date()
        last_7_days = timezone.now() - timedelta(days=7)
        
        # Strategy stats
        total_strategies = Strategy.objects.count()
        active_strategies = Strategy.objects.filter(is_active=True).count()
        
        # Session stats
        total_sessions = LiveTradingSession.objects.count()
        active_sessions = LiveTradingSession.objects.filter(is_active=True).count()
        
        # Signal stats
        signals_today = TradingSignal.objects.filter(
            created_at__date=today
        ).count()
        
        signals_week = TradingSignal.objects.filter(
            created_at__gte=last_7_days
        ).aggregate(
            total=Count('id'),
            buy=Count('id', filter=Q(signal_type='BUY')),
            sell=Count('id', filter=Q(signal_type='SELL'))
        )
        
        # Broker stats
        active_brokers = Broker.objects.filter(is_active=True).count()
        
        stats = {
            'strategies': {
                'total': total_strategies,
                'active': active_strategies,
                'inactive': total_strategies - active_strategies
            },
            'sessions': {
                'total': total_sessions,
                'active': active_sessions,
                'stopped': total_sessions - active_sessions
            },
            'signals': {
                'today': signals_today,
                'week_total': signals_week['total'],
                'week_buy': signals_week['buy'],
                'week_sell': signals_week['sell']
            },
            'brokers': {
                'active': active_brokers
            },
            'timestamp': timezone.now().isoformat()
        }
        
        return JsonResponse({
            'success': True,
            'stats': stats
        })
        
    except Exception as e:
        logger.error(f"Error fetching stats: {str(e)}")
        return JsonResponse({
            'success': False,
            'message': str(e)
        }, status=500)


@login_required
def check_system_health(request):
    """
    Check if strategies are loading properly
    Diagnostic endpoint for debugging
    """
    try:
        health_data = {
            'strategies': {
                'total': Strategy.objects.count(),
                'active': Strategy.objects.filter(is_active=True).count(),
                'inactive': Strategy.objects.filter(is_active=False).count(),
                'with_broker': Strategy.objects.filter(broker__isnull=False).count(),
                'without_broker': Strategy.objects.filter(broker__isnull=True).count()
            },
            'brokers': {
                'total': Broker.objects.count(),
                'active': Broker.objects.filter(is_active=True).count()
            },
            'sessions': {
                'total': LiveTradingSession.objects.count(),
                'active': LiveTradingSession.objects.filter(is_active=True).count()
            },
            'signals': {
                'total': TradingSignal.objects.count(),
                'today': TradingSignal.objects.filter(
                    created_at__date=timezone.now().date()
                ).count()
            },
            'timestamp': timezone.now().isoformat(),
            'status': 'healthy'
        }
        
        # Check for issues
        issues = []
        
        if health_data['strategies']['active'] == 0:
            issues.append('No active strategies found')
        
        if health_data['brokers']['active'] == 0:
            issues.append('No active brokers found')
        
        if health_data['strategies']['without_broker'] > 0:
            issues.append(f"{health_data['strategies']['without_broker']} strategies have no broker assigned")
        
        health_data['issues'] = issues
        health_data['has_issues'] = len(issues) > 0
        
        return JsonResponse({
            'success': True,
            'health': health_data
        })
        
    except Exception as e:
        logger.error(f"Health check error: {str(e)}")
        return JsonResponse({
            'success': False,
            'message': str(e),
            'status': 'unhealthy'
        }, status=500)
