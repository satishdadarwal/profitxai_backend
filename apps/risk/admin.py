from django.contrib import admin
from .models import TradingProfile, RiskEvent

@admin.register(TradingProfile)
class TradingProfileAdmin(admin.ModelAdmin):
    list_display = ['user', 'max_daily_loss', 'max_daily_trades', 'require_stop_loss']
    search_fields = ['user__username', 'user__email']

@admin.register(RiskEvent)
class RiskEventAdmin(admin.ModelAdmin):
    list_display = ['user', 'event_type', 'severity', 'created_at']
    list_filter = ['event_type', 'severity', 'created_at']
    search_fields = ['user__username', 'reason']