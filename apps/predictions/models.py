
# apps/predictions/models.py

import uuid
from django.db import models
from django.contrib.auth import get_user_model

User = get_user_model()


class DailyPrediction(models.Model):
    """Next day prediction for a symbol."""

    class Bias(models.TextChoices):
        BULLISH = "bullish", "Bullish"
        BEARISH = "bearish", "Bearish"
        NEUTRAL = "neutral", "Neutral"

    class Confidence(models.TextChoices):
        HIGH   = "high",   "High (>70)"
        MEDIUM = "medium", "Medium (45-70)"
        LOW    = "low",    "Low (<45)"

    id             = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    symbol         = models.CharField(max_length=50)
    prediction_date = models.DateField()           # next trading day
    generated_at   = models.DateTimeField(auto_now_add=True)

    # ── ICT/SMC Analysis ──────────────────────────────────
    bias           = models.CharField(max_length=10, choices=Bias.choices)
    confidence     = models.CharField(max_length=10, choices=Confidence.choices)
    confluence_score = models.FloatField(default=0)

    # ── Key Levels ────────────────────────────────────────
    entry_zone_high = models.FloatField(null=True)
    entry_zone_low  = models.FloatField(null=True)
    stop_loss       = models.FloatField(null=True)
    target_1        = models.FloatField(null=True)
    target_2        = models.FloatField(null=True)
    target_3        = models.FloatField(null=True)

    # ── Global Cues ───────────────────────────────────────
    global_score    = models.FloatField(default=0)   # -100 to +100
    global_cues     = models.JSONField(default=dict)

    # ── News Sentiment ────────────────────────────────────
    news_sentiment  = models.FloatField(default=0)   # -1 to +1
    news_summary    = models.TextField(blank=True)
    top_news        = models.JSONField(default=list)

    # ── MTF Breakdown ─────────────────────────────────────
    mtf_analysis    = models.JSONField(default=dict)
    key_levels      = models.JSONField(default=list)

    # ── Final Score ───────────────────────────────────────
    final_score     = models.FloatField(default=0)   # 0-100
    accuracy_score  = models.FloatField(null=True, blank=True)  # post-evaluation score
    summary         = models.TextField(blank=True)
    trade_plan      = models.JSONField(default=dict)

    # ── Outcome tracking ─────────────────────────────────
    actual_move     = models.FloatField(null=True)   # fill after market close
    was_correct     = models.BooleanField(null=True)

    class Meta:
        unique_together = ("symbol", "prediction_date")
        ordering = ["-prediction_date"]

    def __str__(self):
        return f"{self.symbol} | {self.prediction_date} | {self.bias} | {self.final_score:.0f}"


class HourlyPrediction(models.Model):
    """Hourly intraday prediction — generated every 1H during market hours."""

    class Bias(models.TextChoices):
        BULLISH = "bullish", "Bullish"
        BEARISH = "bearish", "Bearish"
        NEUTRAL = "neutral", "Neutral"

    id              = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    symbol          = models.CharField(max_length=50)
    prediction_hour = models.DateTimeField()
    generated_at    = models.DateTimeField(auto_now_add=True)

    bias            = models.CharField(max_length=10, choices=Bias.choices)
    confidence_pct  = models.FloatField(default=0)
    confluence_score = models.FloatField(default=0)

    entry_zone_high = models.FloatField(null=True)
    entry_zone_low  = models.FloatField(null=True)
    stop_loss       = models.FloatField(null=True)
    target_1        = models.FloatField(null=True)
    target_2        = models.FloatField(null=True)

    key_levels      = models.JSONField(default=list)
    ict_breakdown   = models.JSONField(default=dict)
    trade_plan      = models.JSONField(default=dict)
    summary         = models.TextField(blank=True)

    actual_close    = models.FloatField(null=True)
    outcome         = models.CharField(
        max_length=10,
        choices=[("pending","Pending"),("hit","Hit"),("miss","Miss"),("expired","Expired")],
        default="pending"
    )
    was_correct     = models.BooleanField(null=True)

    class Meta:
        unique_together = ("symbol", "prediction_hour")
        ordering = ["-prediction_hour"]
        indexes = [
            models.Index(fields=["symbol", "prediction_hour"]),
        ]

    def __str__(self):
        return f"{self.symbol} | {self.prediction_hour} | {self.bias} | {self.confidence_pct:.0f}%"


class GlobalCueSnapshot(models.Model):
    """Daily snapshot of global market cues."""

    date         = models.DateField(unique=True)
    fetched_at   = models.DateTimeField(auto_now_add=True)

    # US Markets
    sp500_close  = models.FloatField(null=True)
    sp500_chg_pct = models.FloatField(null=True)
    dow_close    = models.FloatField(null=True)
    dow_chg_pct  = models.FloatField(null=True)
    nasdaq_close = models.FloatField(null=True)
    nasdaq_chg_pct = models.FloatField(null=True)

    # Asian Markets
    nikkei_close = models.FloatField(null=True)
    nikkei_chg_pct = models.FloatField(null=True)
    hangseng_close = models.FloatField(null=True)
    hangseng_chg_pct = models.FloatField(null=True)
    gift_nifty   = models.FloatField(null=True)

    # Commodities
    crude_oil    = models.FloatField(null=True)
    crude_chg_pct = models.FloatField(null=True)
    gold         = models.FloatField(null=True)
    gold_chg_pct = models.FloatField(null=True)

    # Fear/Dollar
    vix_india    = models.FloatField(null=True)
    vix_us       = models.FloatField(null=True)
    dxy          = models.FloatField(null=True)
    dxy_chg_pct  = models.FloatField(null=True)

    # FII/DII
    fii_net      = models.FloatField(null=True)
    dii_net      = models.FloatField(null=True)

    # Composite score
    global_score = models.FloatField(default=0)
    raw_data     = models.JSONField(default=dict)

    class Meta:
        ordering = ["-date"]
