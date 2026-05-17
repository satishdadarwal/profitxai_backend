from django.contrib import admin

from .models import Strategy, StrategyPerformanceSnapshot, StrategySignal


@admin.register(Strategy)
class StrategyAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "broker",
        "name",
        "algo_name",
        "symbol",
        "symbols",
        "mode",
        "state",
        "interval_seconds",
        "is_active",
        "created_at",
        "updated_at",
    )
    list_filter = ("mode", "state", "is_active", "created_at")
    search_fields = ("name", "algo_name", "symbol", "user__username")
    ordering = ("-created_at",)
    readonly_fields = (
        "created_at",
        "updated_at",
        "started_at",
        "stopped_at",
        "error_msg",
    )

    # Custom actions
    actions = ["mark_idle", "mark_running", "mark_error"]

    def mark_idle(self, request, queryset):
        queryset.update(state=Strategy.State.IDLE)

    mark_idle.short_description = "Mark selected strategies as Idle"

    def mark_running(self, request, queryset):
        queryset.update(state=Strategy.State.RUNNING)

    mark_running.short_description = "Mark selected strategies as Running"

    def mark_error(self, request, queryset):
        queryset.update(state=Strategy.State.ERROR)

    mark_error.short_description = "Mark selected strategies as Error"


@admin.register(StrategySignal)
class StrategySignalAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "strategy",
        "signal_type",
        "symbol",
        "price",
        "result",
        "order",
        "created_at",
    )
    list_filter = ("signal_type", "result", "created_at")
    search_fields = ("symbol", "reason", "strategy__name")
    ordering = ("-created_at",)


@admin.register(StrategyPerformanceSnapshot)
class StrategyPerformanceSnapshotAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "strategy",
        "granularity",
        "period_start",
        "total_trades",
        "win_rate",
        "total_pnl",
        "total_fees",
        "created_at",
    )
    list_filter = ("granularity", "period_start")
    search_fields = ("strategy__name",)
    ordering = ("-period_start",)
