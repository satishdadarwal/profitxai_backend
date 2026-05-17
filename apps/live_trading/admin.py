# apps/live_trading/admin.py

from django.contrib import admin
from django.utils.html import format_html
from django.utils import timezone

from .models import TradingSession, LiveSignal, ActivityLog, ManualOrder


# ─────────────────────────────────────────────────────────────
#  Inline: LiveSignal → TradingSession ke andar dikhana
# ─────────────────────────────────────────────────────────────
class LiveSignalInline(admin.TabularInline):
    model = LiveSignal
    extra = 0
    readonly_fields = (
        "symbol", "direction", "signal_type", "strength",
        "entry_price", "stop_loss", "take_profit", "rr_ratio",
        "lots", "margin_req", "mode", "status",
        "detected_at", "expires_at", "acted_at",
    )
    fields = readonly_fields
    show_change_link = True
    can_delete = False
    max_num = 0  # sirf read-only dikhana, add nahi karna


class ActivityLogInline(admin.TabularInline):
    model = ActivityLog
    extra = 0
    readonly_fields = (
        "symbol", "direction", "status", "mode",
        "entry_price", "pnl", "note", "created_at",
    )
    fields = readonly_fields
    show_change_link = True
    can_delete = False
    max_num = 0


class ManualOrderInline(admin.TabularInline):
    model = ManualOrder
    extra = 0
    readonly_fields = (
        "symbol", "direction", "order_type", "lots",
        "price", "stop_loss", "take_profit", "rr_ratio",
        "margin_req", "status", "broker_order_id",
        "placed_at", "filled_at", "fill_price", "created_at",
    )
    fields = readonly_fields
    show_change_link = True
    can_delete = False
    max_num = 0


# ─────────────────────────────────────────────────────────────
#  1. TradingSession Admin
# ─────────────────────────────────────────────────────────────
@admin.register(TradingSession)
class TradingSessionAdmin(admin.ModelAdmin):
    list_display = (
        "id", "user", "strategy_id", "mode",
        "is_active_badge", "started_at", "ended_at",
        "total_trades", "winning_trades", "win_rate_display",
        "total_pnl",
    )
    list_filter = ("mode", "is_active", "started_at")
    search_fields = ("user__username", "user__email", "strategy_id")
    readonly_fields = (
        "started_at", "ended_at",
        "total_trades", "winning_trades", "total_pnl",
        "max_drawdown", "peak_equity", "win_rate_display",
    )
    fieldsets = (
        ("Session Info", {
            "fields": ("user", "strategy_id", "mode", "is_active", "started_at", "ended_at"),
        }),
        ("Performance Summary", {
            "fields": (
                "total_trades", "winning_trades", "win_rate_display",
                "total_pnl", "max_drawdown", "peak_equity",
            ),
            "classes": ("collapse",),
        }),
    )
    inlines = [LiveSignalInline, ActivityLogInline, ManualOrderInline]
    date_hierarchy = "started_at"
    ordering = ("-started_at",)

    @admin.display(description="Win Rate")
    def win_rate_display(self, obj):
        return f"{obj.win_rate:.1f}%"

    @admin.display(description="Active", boolean=False)
    def is_active_badge(self, obj):
        if obj.is_active:
            return format_html('<span style="color:green;font-weight:bold;">● Active</span>')
        return format_html('<span style="color:gray;">○ Closed</span>')

    actions = ["force_close_sessions"]

    @admin.action(description="Selected sessions band karo (force close)")
    def force_close_sessions(self, request, queryset):
        closed = 0
        for session in queryset.filter(is_active=True):
            session.close()
            closed += 1
        self.message_user(request, f"{closed} session(s) band kar diye gaye.")


# ─────────────────────────────────────────────────────────────
#  2. LiveSignal Admin
# ─────────────────────────────────────────────────────────────
@admin.register(LiveSignal)
class LiveSignalAdmin(admin.ModelAdmin):
    list_display = (
        "id", "user", "symbol", "direction_badge", "signal_type",
        "strength", "entry_price", "rr_ratio", "lots",
        "mode", "status_badge", "detected_at", "expires_at",
    )
    list_filter = ("status", "mode", "direction", "strength", "signal_type", "detected_at")
    search_fields = ("user__username", "symbol", "strategy_id")
    readonly_fields = (
        "session", "user", "strategy_id", "symbol",
        "direction", "signal_type", "strength",
        "entry_price", "stop_loss", "take_profit", "rr_ratio",
        "lots", "margin_req", "mode", "status",
        "detected_at", "expires_at", "acted_at", "raw_payload",
    )
    fieldsets = (
        ("Signal Details", {
            "fields": (
                "session", "user", "strategy_id", "symbol",
                "direction", "signal_type", "strength", "mode",
            ),
        }),
        ("Price Levels", {
            "fields": ("entry_price", "stop_loss", "take_profit", "rr_ratio", "lots", "margin_req"),
        }),
        ("Status & Timing", {
            "fields": ("status", "detected_at", "expires_at", "acted_at"),
        }),
        ("Raw Payload", {
            "fields": ("raw_payload",),
            "classes": ("collapse",),
        }),
    )
    date_hierarchy = "detected_at"
    ordering = ("-detected_at",)

    @admin.display(description="Direction")
    def direction_badge(self, obj):
        color = "green" if obj.direction == "buy" else "red"
        label = obj.direction.upper()
        return format_html('<span style="color:{};font-weight:bold;">▲ {}</span>' if obj.direction == "buy"
                           else '<span style="color:{};font-weight:bold;">▼ {}</span>', color, label)

    @admin.display(description="Status")
    def status_badge(self, obj):
        colors = {
            "pending":    "orange",
            "processing": "blue",
            "confirmed":  "teal",
            "executed":   "green",
            "ignored":    "gray",
            "expired":    "darkred",
        }
        color = colors.get(obj.status, "black")
        return format_html('<span style="color:{};">{}</span>', color, obj.get_status_display())

    actions = ["mark_expired_signals"]

    @admin.action(description="Selected signals ko Expired mark karo")
    def mark_expired_signals(self, request, queryset):
        updated = 0
        for signal in queryset.filter(status__in=["pending", "processing"]):
            signal.mark_expired()
            updated += 1
        self.message_user(request, f"{updated} signal(s) expired mark kiye gaye.")


# ─────────────────────────────────────────────────────────────
#  3. ActivityLog Admin
# ─────────────────────────────────────────────────────────────
@admin.register(ActivityLog)
class ActivityLogAdmin(admin.ModelAdmin):
    list_display = (
        "id", "user", "symbol", "direction", "status",
        "mode", "entry_price", "pnl_colored", "note", "created_at",
    )
    list_filter = ("status", "mode", "direction", "created_at")
    search_fields = ("user__username", "symbol", "note")
    readonly_fields = (
        "session", "signal", "user", "status", "mode",
        "symbol", "direction", "entry_price", "pnl",
        "note", "created_at", "metadata",
    )
    fieldsets = (
        ("Trade Info", {
            "fields": ("session", "signal", "user", "symbol", "direction", "mode", "status"),
        }),
        ("Financials", {
            "fields": ("entry_price", "pnl"),
        }),
        ("Notes & Meta", {
            "fields": ("note", "created_at", "metadata"),
            "classes": ("collapse",),
        }),
    )
    date_hierarchy = "created_at"
    ordering = ("-created_at",)

    @admin.display(description="P&L")
    def pnl_colored(self, obj):
        if obj.pnl is None:
            return "—"
        color = "green" if obj.pnl >= 0 else "red"
        return format_html('<span style="color:{};">₹ {}</span>', color, obj.pnl)


# ─────────────────────────────────────────────────────────────
#  4. ManualOrder Admin
# ─────────────────────────────────────────────────────────────
@admin.register(ManualOrder)
class ManualOrderAdmin(admin.ModelAdmin):
    list_display = (
        "id", "user", "symbol", "direction_badge",
        "order_type", "lots", "price", "stop_loss", "take_profit",
        "rr_ratio", "margin_req", "status_badge",
        "broker_order_id", "placed_at", "fill_price", "created_at",
    )
    list_filter = ("status", "direction", "order_type", "created_at")
    search_fields = ("user__username", "symbol", "broker_order_id")
    readonly_fields = (
        "session", "user", "symbol", "direction",
        "order_type", "lots", "price", "stop_loss", "take_profit",
        "rr_ratio", "margin_req", "status", "broker_order_id",
        "placed_at", "filled_at", "fill_price", "created_at",
    )
    fieldsets = (
        ("Order Details", {
            "fields": (
                "session", "user", "symbol", "direction",
                "order_type", "lots", "price",
            ),
        }),
        ("Risk Management", {
            "fields": ("stop_loss", "take_profit", "rr_ratio", "margin_req"),
        }),
        ("Execution", {
            "fields": (
                "status", "broker_order_id",
                "placed_at", "filled_at", "fill_price",
            ),
        }),
        ("Timestamps", {
            "fields": ("created_at",),
            "classes": ("collapse",),
        }),
    )
    date_hierarchy = "created_at"
    ordering = ("-created_at",)

    @admin.display(description="Direction")
    def direction_badge(self, obj):
        color = "green" if obj.direction == "buy" else "red"
        arrow = "▲" if obj.direction == "buy" else "▼"
        return format_html(
            '<span style="color:{};font-weight:bold;">{} {}</span>',
            color, arrow, obj.direction.upper()
        )

    @admin.display(description="Status")
    def status_badge(self, obj):
        colors = {
            "draft":     "gray",
            "placed":    "blue",
            "filled":    "green",
            "rejected":  "red",
            "cancelled": "darkred",
        }
        color = colors.get(obj.status, "black")
        return format_html('<span style="color:{};">{}</span>', color, obj.get_status_display())

    actions = ["cancel_orders"]

    @admin.action(description="Selected orders cancel karo")
    def cancel_orders(self, request, queryset):
        updated = queryset.filter(status__in=["draft", "placed"]).update(status="cancelled")
        self.message_user(request, f"{updated} order(s) cancel kar diye gaye.")