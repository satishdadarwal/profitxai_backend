from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    OrderViewSet,
    TradeViewSet,
    TradeJournalEntryViewSet,
    TradeJournalListView,
    CalendarPerformanceView,
    ExportTradesCSVView,
    ExportTradesPDFView,
    DailyPnlView,
    TradeJournalListView,
)
from .views import calculate_risk_reward
from .position_views import get_open_positions, close_position, close_all_positions

router = DefaultRouter()
router.register('orders', OrderViewSet, basename='order')
router.register('trades', TradeViewSet, basename='trade')
router.register('journal-entries', TradeJournalEntryViewSet, basename='journal-entry')

urlpatterns = [
    # ✅ Custom paths BEFORE router — prevent trades/<pk> conflict
    path('journal/', TradeJournalListView.as_view(), name='trade-journal'),
    path('trades/calendar-performance/', CalendarPerformanceView.as_view(), name='calendar-performance'),
    path('trades/export/csv/', ExportTradesCSVView.as_view(), name='export-csv'),
    path('trades/export/pdf/', ExportTradesPDFView.as_view(), name='export-pdf'),
    path('trades/calculate-rr/', calculate_risk_reward, name='calculate-rr'),
    path('daily-pnl/', DailyPnlView.as_view(), name='daily-pnl'),
    path('positions/open/', get_open_positions, name='open-positions'),
    path('positions/<uuid:position_id>/close/', close_position, name='close-position'),
    path('positions/close-all/', close_all_positions, name='close-all-positions'),
    # Router last mein
    path('', include(router.urls)),
]
