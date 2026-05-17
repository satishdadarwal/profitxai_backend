# apps/market/models.py

import uuid

from django.db import models


class Asset(models.Model):
    """
    Tradeable asset — equity, crypto, futures, options.
    Broker adapters aur orders dono yahan se symbol lookup karte hain.
    """

    class AssetType(models.TextChoices):
        EQUITY = "equity", "Equity"
        CRYPTO = "crypto", "Crypto"
        FUTURES = "futures", "Futures"
        OPTIONS = "options", "Options"
        INDEX = "index", "Index"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    symbol = models.CharField(max_length=50, unique=True, db_index=True)
    name = models.CharField(max_length=200)
    asset_type = models.CharField(
        max_length=10, choices=AssetType.choices, default=AssetType.EQUITY
    )
    exchange = models.CharField(max_length=20, blank=True)  # NSE, BSE, CRYPTO
    currency = models.CharField(max_length=10, default="INR")
    is_active = models.BooleanField(default=True, db_index=True)
    last_price = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    lot_size = models.PositiveIntegerField(default=1)
    tick_size = models.DecimalField(max_digits=10, decimal_places=5, default="0.05")
    metadata = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "market_assets"
        ordering = ["symbol"]

    def __str__(self):
        return f"{self.symbol} ({self.exchange})"

    @classmethod
    def get_or_create_from_symbol(cls, symbol: str, **defaults) -> "Asset":
        """
        Symbol string se asset fetch karo — nahi hai toh create karo.
        orders/services.py aur broker adapters yahan se call karte hain.
        """
        obj, _ = cls.objects.get_or_create(
            symbol=symbol.upper(),
            defaults={
                "name": defaults.get("name", symbol),
                "exchange": defaults.get("exchange", ""),
                "currency": defaults.get("currency", "INR"),
                "asset_type": defaults.get("asset_type", cls.AssetType.EQUITY),
                "is_active": True,
            },
        )
        return obj


class MarketQuote(models.Model):
    """Live quote snapshot — WebSocket ya polling se update hota hai."""

    asset = models.OneToOneField(Asset, on_delete=models.CASCADE, related_name="quote")
    ltp = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    bid = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    ask = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    volume = models.BigIntegerField(default=0)
    change = models.DecimalField(max_digits=10, decimal_places=4, default=0)
    change_pct = models.DecimalField(max_digits=8, decimal_places=4, default=0)
    high = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    low = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    open = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    prev_close = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "market_quotes"

    def __str__(self):
        return f"{self.asset.symbol} @ {self.ltp}"
