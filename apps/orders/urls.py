from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    OrderViewSet,
    TradeViewSet,
    TradeJournalEntryViewSet,
    CalendarPerformanceView,
    ExportTradesCSVView,
    ExportTradesPDFView,
    calculate_risk_reward,
    TradeJournalListView,   # ✅ Journal list view
)
from .position_views import (
    get_open_positions,     # ✅ GET /positions/open/
    close_position,         # ✅ POST /positions/{id}/close/
    close_all_positions,    # ✅ POST /positions/close-all/
)

# Router setup
router = DefaultRouter()
router.register('orders', OrderViewSet, basename='order')
router.register('trades', TradeViewSet, basename='trade')
router.register('journal-entries', TradeJournalEntryViewSet, basename='journal-entry')

urlpatterns = [
    path('', include(router.urls)),

    # Journal list (Flutter expects /api/v1/orders/journal/)
    path('journal/', TradeJournalListView.as_view(), name='trade-journal'),

    # Calendar performance
    path('trades/calendar-performance/', CalendarPerformanceView.as_view(), name='calendar-performance'),

    # Export
    path('trades/export/csv/', ExportTradesCSVView.as_view(), name='export-csv'),
    path('trades/export/pdf/', ExportTradesPDFView.as_view(), name='export-pdf'),

    # Risk/Reward Calculator
    path('trades/calculate-rr/', calculate_risk_reward, name='calculate-rr'),

    # ✅ Position Management (NEW)
    path('positions/open/', get_open_positions, name='open-positions'),
    path('positions/<uuid:position_id>/close/', close_position, name='close-position'),
    path('positions/close-all/', close_all_positions, name='close-all-positions'),
]