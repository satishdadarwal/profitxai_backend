from django.contrib import admin

from .models import Order, TradeJournalEntry


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "asset",
        "side",
        "order_type",
        "status",
        "mode",
        "quantity",
        "filled_qty",
        "limit_price",
        "stop_price",
        "avg_fill_price",
        "created_at",
        "updated_at",
    )
    list_filter = ("status", "side", "order_type", "mode", "asset")
    search_fields = ("user__username", "asset__symbol", "exchange_order_id")
    ordering = ("-created_at",)
    readonly_fields = ("created_at", "updated_at")

    # Custom actions
    actions = ["mark_open", "mark_filled", "mark_cancelled"]

    def mark_open(self, request, queryset):
        queryset.update(status=Order.Status.OPEN)

    mark_open.short_description = "Mark selected orders as Open"

    def mark_filled(self, request, queryset):
        queryset.update(status=Order.Status.FILLED)

    mark_filled.short_description = "Mark selected orders as Filled"

    def mark_cancelled(self, request, queryset):
        queryset.update(status=Order.Status.CANCELLED)

    mark_cancelled.short_description = "Mark selected orders as Cancelled"



@admin.register(TradeJournalEntry)
class TradeJournalEntryAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "title",
        "strategy",
        "emotion",
        "outcome",
        "rating",
        "created_at",
        "updated_at",
    )
    list_filter = ("emotion", "outcome", "rating")
    search_fields = ("title", "body", "strategy", "user__username")
    ordering = ("-created_at",)
