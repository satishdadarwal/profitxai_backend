from django.contrib import admin

from .models import Asset, MarketQuote


@admin.register(Asset)
class AssetAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "symbol",
        "name",
        "asset_type",
        "exchange",
        "currency",
        "is_active",
        "last_price",
        "lot_size",
        "tick_size",
        "updated_at",
        "created_at",
    )
    list_filter = ("asset_type", "exchange", "is_active")
    search_fields = ("symbol", "name", "exchange")
    ordering = ("symbol",)


@admin.register(MarketQuote)
class MarketQuoteAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "asset",
        "ltp",
        "bid",
        "ask",
        "volume",
        "change",
        "change_pct",
        "high",
        "low",
        "open",
        "prev_close",
        "updated_at",
    )
    list_filter = ("asset__asset_type", "asset__exchange")
    search_fields = ("asset__symbol", "asset__name")
    ordering = ("-updated_at",)
