# apps/options/models.py
# UPDATED VERSION - WITH NOTES, TAGS, EMOJI_REACTION FOR INDIAN OPTIONS

import uuid
from django.conf import settings
from django.db import models


class OptionSymbol(models.Model):
    """NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY etc."""

    name = models.CharField(max_length=20, unique=True)  # NIFTY
    fyers_symbol = models.CharField(max_length=50)  # NSE:NIFTY50-INDEX
    lot_size = models.IntegerField(default=75)
    strike_step = models.IntegerField(default=50)  # NIFTY=50, BNK=100
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class OptionContract(models.Model):
    CE = "CE"
    PE = "PE"
    TYPE_CHOICES = [(CE, "Call"), (PE, "Put")]

    symbol = models.ForeignKey(OptionSymbol, on_delete=models.CASCADE)
    strike = models.FloatField()
    option_type = models.CharField(max_length=2, choices=TYPE_CHOICES)
    expiry = models.DateField()
    fyers_symbol = models.CharField(max_length=60)  # NSE:NIFTY25APR1022500CE

    class Meta:
        unique_together = ("symbol", "strike", "option_type", "expiry")

    def __str__(self):
        return f"{self.symbol.name} {self.strike}{self.option_type} {self.expiry}"


class OptionSnapshot(models.Model):
    """Live/cached option data – Greeks, OI, IV"""

    contract = models.ForeignKey(
        OptionContract, on_delete=models.CASCADE, related_name="snapshots"
    )
    ltp = models.FloatField()
    oi = models.BigIntegerField(default=0)
    volume = models.BigIntegerField(default=0)
    iv = models.FloatField(null=True, blank=True)
    delta = models.FloatField(null=True, blank=True)
    theta = models.FloatField(null=True, blank=True)
    spot_price = models.FloatField()
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        get_latest_by = "timestamp"


class BacktestRun(models.Model):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    STATUS_CHOICES = [
        (PENDING, "Pending"),
        (RUNNING, "Running"),
        (COMPLETED, "Completed"),
        (FAILED, "Failed"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    symbol = models.ForeignKey(OptionSymbol, on_delete=models.CASCADE)
    from_date = models.DateField()
    to_date = models.DateField()
    strategy = models.CharField(max_length=50, default="ICT_MTF")
    initial_capital = models.FloatField(default=500000)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=PENDING)

    # Results
    final_capital = models.FloatField(null=True, blank=True)
    total_pnl = models.FloatField(null=True, blank=True)
    win_rate = models.FloatField(null=True, blank=True)
    max_drawdown = models.FloatField(null=True, blank=True)
    total_trades = models.IntegerField(null=True, blank=True)
    error_message = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class IVHistory(models.Model):
    symbol = models.ForeignKey(OptionSymbol, on_delete=models.CASCADE, related_name="iv_history")
    date = models.DateField()
    atm_iv = models.FloatField()
    iv_rank = models.FloatField(null=True, blank=True)

    class Meta:
        unique_together = ("symbol", "date")
        ordering = ["-date"]

    def __str__(self):
        return f"{self.symbol.name} | {self.date} | IV={self.atm_iv}"


class OptionChainSnapshot(models.Model):
    symbol = models.ForeignKey(OptionSymbol, on_delete=models.CASCADE, related_name="chain_snapshots")
    expiry = models.DateField()
    spot = models.FloatField()
    pcr_oi = models.FloatField(null=True, blank=True)
    pcr_volume = models.FloatField(null=True, blank=True)
    max_pain = models.FloatField(null=True, blank=True)
    atm_strike = models.IntegerField(null=True, blank=True)
    atm_ce_iv = models.FloatField(null=True, blank=True)
    atm_pe_iv = models.FloatField(null=True, blank=True)
    vix = models.FloatField(null=True, blank=True)
    call_wall = models.FloatField(null=True, blank=True)
    put_wall = models.FloatField(null=True, blank=True)
    chain_data = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.symbol.name} | {self.expiry} | spot={self.spot}"


class OptionsPrediction(models.Model):
    DIRECTION_CHOICES = [("bullish", "Bullish"), ("bearish", "Bearish"), ("neutral", "Neutral")]

    symbol = models.ForeignKey(OptionSymbol, on_delete=models.CASCADE, related_name="predictions")
    snapshot = models.ForeignKey(OptionChainSnapshot, on_delete=models.SET_NULL, null=True, blank=True)
    expiry = models.DateField()
    direction = models.CharField(max_length=10, choices=DIRECTION_CHOICES)
    confidence_pct = models.FloatField(default=0)
    signal_score = models.FloatField(default=0)
    expected_range_low = models.FloatField(null=True, blank=True)
    expected_range_high = models.FloatField(null=True, blank=True)
    max_pain = models.FloatField(null=True, blank=True)
    call_wall = models.FloatField(null=True, blank=True)
    put_wall = models.FloatField(null=True, blank=True)
    breakeven_pts = models.FloatField(null=True, blank=True)
    up_prob = models.FloatField(default=0)
    flat_prob = models.FloatField(default=0)
    down_prob = models.FloatField(default=0)
    suggested_strategy = models.CharField(max_length=50, null=True, blank=True)
    strategy_legs = models.JSONField(default=list)
    pcr_oi = models.FloatField(null=True, blank=True)
    iv_rank = models.FloatField(null=True, blank=True)
    signal_factors = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.symbol.name} | {self.direction} | {self.created_at:%d %b %H:%M}"
