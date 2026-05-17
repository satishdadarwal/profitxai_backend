"""
Fixed URLs Configuration for Live Trading
Ensures all strategy endpoints are properly mapped
"""

from django.urls import path
from . import views, signal_handler

app_name = 'live_trading'

urlpatterns = [
    # Main Dashboard
    path('', views.live_trading_dashboard, name='dashboard'),
    path('dashboard/', views.live_trading_dashboard, name='dashboard_alt'),
    
    # Strategy Management
    path('strategies/', signal_handler.strategy_list, name='strategy_list'),
    path('strategies/create/', signal_handler.create_strategy, name='create_strategy'),
    path('strategies/<int:strategy_id>/', signal_handler.strategy_details, name='strategy_details'),
    path('strategies/<int:strategy_id>/toggle/', signal_handler.toggle_strategy, name='toggle_strategy'),
    path('strategies/<int:strategy_id>/delete/', signal_handler.delete_strategy, name='delete_strategy'),
    
    # API Endpoints
    path('api/strategies/', signal_handler.get_strategies_api, name='api_strategies'),
    path('api/strategies/refresh/', views.refresh_strategies, name='refresh_strategies'),
    path('api/stats/', views.get_dashboard_stats, name='dashboard_stats'),
    path('api/health/', views.check_system_health, name='system_health'),
    
    # Trading Sessions
    path('sessions/start/', views.start_trading_session, name='start_session'),
    path('sessions/<int:session_id>/stop/', views.stop_trading_session, name='stop_session'),
]
