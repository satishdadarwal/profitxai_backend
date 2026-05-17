from django.contrib import admin

from .models import BacktestRun


@admin.register(BacktestRun)
class BacktestRunAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "name",
        "symbol",
        "timeframe",
        "strategy_name",
        "status",
        "created_at",
        "completed_at",
    )
    list_filter = ("status", "timeframe", "strategy_name", "created_at")
    search_fields = ("name", "symbol", "strategy_name", "user__username")
    ordering = ("-created_at",)

    # Read-only fields (timestamps and results)
    readonly_fields = ("created_at", "completed_at", "results", "error_message")

    # Custom actions for bulk status update
    actions = ["mark_pending", "mark_running", "mark_done", "mark_failed"]

    def mark_pending(self, request, queryset):
        queryset.update(status=BacktestRun.Status.PENDING)

    mark_pending.short_description = "Mark selected runs as Pending"

    def mark_running(self, request, queryset):
        queryset.update(status=BacktestRun.Status.RUNNING)

    mark_running.short_description = "Mark selected runs as Running"

    def mark_done(self, request, queryset):
        queryset.update(status=BacktestRun.Status.DONE)

    mark_done.short_description = "Mark selected runs as Done"

    def mark_failed(self, request, queryset):
        queryset.update(status=BacktestRun.Status.FAILED)

    mark_failed.short_description = "Mark selected runs as Failed"
