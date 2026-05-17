from rest_framework import serializers

from .models import Asset, MarketQuote


class AssetSerializer(serializers.ModelSerializer):
    class Meta:
        model = Asset
        fields = [
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
        ]
        read_only_fields = ["id", "last_price", "updated_at"]


class MarketQuoteSerializer(serializers.ModelSerializer):
    symbol = serializers.CharField(source="asset.symbol", read_only=True)

    class Meta:
        model = MarketQuote
        fields = [
            "symbol",
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
        ]
