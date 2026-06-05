from django.db import models
from django.conf import settings

class TradingProfile(models.Model):
    """Per-user trading risk configuration"""
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='trading_profile'
    )
    
    # Daily limits
    max_daily_loss = models.DecimalField(max_digits=14, decimal_places=2, default=10000)
    profit_lock_amount = models.DecimalField(max_digits=14, decimal_places=2, default=5000)
    max_daily_trades = models.IntegerField(default=50)
    
    # Position limits
    max_position_size = models.DecimalField(max_digits=14, decimal_places=2, default=100000)
    max_positions = models.IntegerField(default=10)
    
    # Risk limits
    max_drawdown = models.DecimalField(max_digits=5, decimal_places=4, default=0.20)
    max_loss_per_trade = models.DecimalField(max_digits=14, decimal_places=2, default=2000)
    min_rr_ratio = models.DecimalField(max_digits=5, decimal_places=2, default=1.5)
    
    # ── % Based Risk Config (auto-scales with capital) ──────────
    # Agar None hai toh hardcoded max_* values use honge (backward compat)
    risk_per_trade_pct = models.DecimalField(
        max_digits=5, decimal_places=4, null=True, blank=True,
        help_text="Per trade max loss % of capital. e.g. 0.02 = 2%"
    )
    max_daily_loss_pct = models.DecimalField(
        max_digits=5, decimal_places=4, null=True, blank=True,
        help_text="Max daily loss % of capital. e.g. 0.05 = 5%"
    )
    max_position_pct = models.DecimalField(
        max_digits=5, decimal_places=4, null=True, blank=True,
        help_text="Max position size % of capital. e.g. 0.10 = 10%"
    )

    # Preferences
    require_stop_loss = models.BooleanField(default=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class RiskEvent(models.Model):
    """Log of risk management events"""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    event_type = models.CharField(max_length=32)  # kill_switch, risk_check_failed, etc.
    severity = models.CharField(max_length=16)    # info, warning, critical
    reason = models.TextField()
    metadata = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'event_type']),
            models.Index(fields=['created_at']),
        ]