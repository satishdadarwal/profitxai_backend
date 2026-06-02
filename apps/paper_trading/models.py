import uuid
from decimal import Decimal
from typing import TYPE_CHECKING

from django.conf import settings
from django.db import models
from django.utils import timezone

if TYPE_CHECKING:
    from django.db.models import QuerySet


# ─────────────────────────────────────────────
# 🔧 UTILITY FUNCTIONS
# ─────────────────────────────────────────────
def normalize_symbol(symbol: str) -> str:
    """
    Normalize symbol by removing exchange prefixes
    DELTA:BTC-USDT -> BTCUSD
    BTC-USDT -> BTCUSD
    NSE:RELIANCE -> RELIANCE
    """
    if not symbol:
        return ""
    s = (
        symbol.replace("DELTA:", "")
        .replace("NSE:", "")
        .replace("BSE:", "")
        .strip()
        .upper()
    )
    # #FIX: Delta crypto symbol normalize karo
    # BTC-USDT → BTCUSD, ETH-USDT → ETHUSD etc.
    _crypto_map = {
        "BTC-USDT": "BTCUSD",
        "ETH-USDT": "ETHUSD",
        "SOL-USDT": "SOLUSD",
        "BNB-USDT": "BNBUSD",
        "XRP-USDT": "XRPUSD",
        "DOGE-USDT": "DOGEUSD",
        "ADA-USDT": "ADAUSD",
    }
    if s in _crypto_map:
        return _crypto_map[s]
    # Generic: remove dash and replace USDT→USD
    if "-USDT" in s:
        return s.replace("-USDT", "USD")
    if "-USD" in s:
        return s.replace("-USD", "USD")
    return s


# ─────────────────────────────────────────────
# 📊 ASSET CONFIGURATION (LOT SIZES)
# ─────────────────────────────────────────────
ASSET_LOT_SIZES = {
    # Indian Indices
    "NIFTY": 25,
    "BANKNIFTY": 15,
    "FINNIFTY": 25,
    "MIDCPNIFTY": 50,
    
    # Major Indian Stocks (example - can be extended)
    "RELIANCE": 250,
    "TCS": 150,
    "INFY": 300,
    "HDFCBANK": 550,
    "ICICIBANK": 1375,
    "SBIN": 1500,
    "BHARTIARTL": 1081,
    "TATAMOTORS": 1500,
    "KOTAKBANK": 400,
    "LT": 300,
}

def get_lot_size(symbol: str, asset_type: str) -> int:
    """
    Get lot size for Indian options/futures
    For crypto, returns 1 (fractional trading)
    """
    if asset_type == "crypto":
        return 1
    
    # Extract base symbol (NIFTY24500CE -> NIFTY)
    base = symbol.split("-")[0]  # Handle NIFTY-FUT
    for key in ASSET_LOT_SIZES:
        if base.startswith(key):
            return ASSET_LOT_SIZES[key]
    
    return 1  # Default


# ─────────────────────────────────────────────
# 💰 PAPER ACCOUNT MODEL
# ─────────────────────────────────────────────
class PaperAccount(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="paper_account",
    )

    balance = models.DecimalField(max_digits=14, decimal_places=2, default=100000)
    initial_capital = models.DecimalField(max_digits=14, decimal_places=2, default=100000)
    free_limit = models.DecimalField(max_digits=14, decimal_places=2, default=100000)
    is_free_plan = models.BooleanField(default=True)
    total_topup = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_withdrawn = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    
    # ─────────────────────────────────────────────
    # 🎯 RISK MANAGEMENT SETTINGS
    # ─────────────────────────────────────────────

    # NOTE: daily_loss_limit_pct, risk_per_trade_pct, and max_open_trades
    # are now computed properties based on risk_tier (see below).
    # Removed their model fields to avoid shadowing.

    daily_loss_limit_fixed = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True,
        help_text="Fixed daily loss limit (overrides tier % if use_percentage_limit=False)"
    )
    use_percentage_limit = models.BooleanField(
        default=True,
        help_text="Use percentage-based limit (True) or fixed amount (False)"
    )

    # Asset-specific max position sizes are now tier-based (see get_max_position_size).
    # The three fixed fields below are removed in favour of the dynamic property.

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    if TYPE_CHECKING:
        trades: "QuerySet[PaperTrade]"

    def __str__(self):
        return f"{self.user} | ₹{self.balance}"

    # ─────────────────────────────────────────────
    # 🏆 TIER SYSTEM
    # ─────────────────────────────────────────────

    @property
    def risk_tier(self):
        """Determine risk tier based on initial capital"""
        capital = self.initial_capital
        if capital <= Decimal('500000'):
            return "tier_1"     # ≤ ₹5L
        elif capital <= Decimal('2500000'):
            return "tier_2"     # ₹5L – ₹25L
        elif capital <= Decimal('10000000'):
            return "tier_3"     # ₹25L – ₹1Cr
        else:
            return "tier_4"     # > ₹1Cr

    @property
    def daily_loss_limit_pct(self):
        """Auto-calculate daily loss % based on tier"""
        tier_limits = {
            "tier_1": Decimal('5.0'),
            "tier_2": Decimal('3.0'),
            "tier_3": Decimal('2.0'),
            "tier_4": Decimal('1.5'),
        }
        return tier_limits.get(self.risk_tier, Decimal('5.0'))

    @property
    def risk_per_trade_pct(self):
        """Auto-calculate per-trade risk % based on tier"""
        tier_risk = {
            "tier_1": Decimal('2.0'),
            "tier_2": Decimal('1.5'),
            "tier_3": Decimal('1.0'),
            "tier_4": Decimal('0.5'),
        }
        return tier_risk.get(self.risk_tier, Decimal('2.0'))

    @property
    def max_open_trades(self):
        """Tier-based max concurrent open positions"""
        tier_trades = {
            "tier_1": 5,
            "tier_2": 8,
            "tier_3": 10,
            "tier_4": 15,
        }
        return tier_trades.get(self.risk_tier, 5)

    def get_max_position_size(self, asset_type: str) -> Decimal:
        """Get tier-based max position size per asset type"""
        limits = {
            "tier_1": {
                "crypto":   Decimal('50000'),
                "option":   Decimal('50000'),
                "futures":  Decimal('100000'),
            },
            "tier_2": {
                "crypto":   Decimal('100000'),
                "option":   Decimal('150000'),
                "futures":  Decimal('150000'),
            },
            "tier_3": {
                "crypto":   Decimal('300000'),
                "option":   Decimal('400000'),
                "futures":  Decimal('400000'),
            },
            "tier_4": {
                "crypto":   Decimal('500000'),
                "option":   Decimal('1000000'),
                "futures":  Decimal('1000000'),
            },
        }
        return limits.get(self.risk_tier, {}).get(asset_type, Decimal('50000'))

    # ─────────────────────────────────────────────
    # 🔐 CRYPTO-SPECIFIC LIMITS
    # ─────────────────────────────────────────────

    @property
    def max_crypto_positions(self):
        """Max crypto positions (subset of total open trades)"""
        tier_crypto = {
            "tier_1": 3,    # out of 5 total
            "tier_2": 4,    # out of 8 total
            "tier_3": 5,    # out of 10 total
            "tier_4": 6,    # out of 15 total
        }
        return tier_crypto.get(self.risk_tier, 3)

    @property
    def max_leverage_crypto(self):
        """Max leverage allowed for crypto trades"""
        tier_leverage = {
            "tier_1": 10,
            "tier_2": 15,
            "tier_3": 20,
            "tier_4": 25,
        }
        return tier_leverage.get(self.risk_tier, 10)

    @property
    def min_margin_buffer_pct(self):
        """Minimum free-margin % that must be kept"""
        tier_buffer = {
            "tier_1": Decimal('30.0'),
            "tier_2": Decimal('25.0'),
            "tier_3": Decimal('20.0'),
            "tier_4": Decimal('20.0'),
        }
        return tier_buffer.get(self.risk_tier, Decimal('30.0'))

    @property
    def current_crypto_positions(self):
        """Count of currently open crypto positions"""
        return self.trades.filter(asset_type="crypto", status="open").count()

    # ─────────────────────────────────────────────
    # 📈 PNL PROPERTIES
    # ─────────────────────────────────────────────

    @property
    def total_pnl(self):
        """Total realized PnL from closed trades"""
        closed = self.trades.filter(status="closed").aggregate(
            total=models.Sum("pnl")
        )["total"] or Decimal("0")
        return closed

    @property
    def unrealized_pnl(self):
        """Total unrealized PnL from open trades"""
        total = Decimal("0")
        for t in self.trades.filter(status="open"):
            total += t.unrealized_pnl
        return total

    @property
    def net_pnl(self):
        """Total PnL (realized + unrealized)"""
        return self.total_pnl + self.unrealized_pnl

    @property
    def todays_realized_pnl(self):
        """Today's realized PnL (from closed trades)"""
        today = timezone.now().date()
        return self.trades.filter(
            status="closed",
            closed_at__date=today
        ).aggregate(total=models.Sum("pnl"))["total"] or Decimal("0")

    @property
    def daily_loss_limit_amount(self):
        """
        Calculate daily loss limit amount.
        - use_percentage_limit=True  → tier % of initial_capital
        - use_percentage_limit=False → fixed amount from daily_loss_limit_fixed
        """
        if self.use_percentage_limit:
            return -(self.initial_capital * (self.daily_loss_limit_pct / 100))
        else:
            return self.daily_loss_limit_fixed or Decimal('-5000')

    # ─────────────────────────────────────────────
    # 🔒 RISK CHECK PROPERTIES
    # ─────────────────────────────────────────────

    @property
    def is_balance_exhausted(self):
        return self.balance <= 0

    @property
    def is_daily_loss_limit_hit(self):
        """Check if today's loss has exceeded the daily limit"""
        return self.todays_realized_pnl <= self.daily_loss_limit_amount

    @property
    def can_trade(self):
        return self.is_active and self.balance > 0

    @property
    def is_paid_user(self):
        return self.total_topup > 0

    @property
    def margin_used(self):
        """Total margin locked in open positions"""
        return self.trades.filter(status="open").aggregate(
            total=models.Sum("margin_used")
        )["total"] or Decimal("0")

    @property
    def available_balance(self):
        """Available balance after deducting used margin"""
        return self.balance - self.margin_used

    def can_open_new_trade(self, asset_type: str = None, position_size: Decimal = None, leverage: int = 1):
        """
        Pre-trade validation.
        Returns: (bool, str) — (can_trade, reason)
        """
        if not self.can_trade:
            return False, "Account inactive or balance exhausted"

        if self.is_daily_loss_limit_hit:
            limit_display = f"₹{abs(self.daily_loss_limit_amount)}"
            if self.use_percentage_limit:
                limit_display += f" ({self.daily_loss_limit_pct}% of capital)"
            return False, f"Daily loss limit hit: ₹{self.todays_realized_pnl} (limit: {limit_display})"

        open_count = self.trades.filter(status="open").count()
        if open_count >= self.max_open_trades:
            return False, f"Max positions reached ({self.max_open_trades})"

        # ── Crypto-specific checks ──────────────────────────────────────
        if asset_type == "crypto":
            if self.current_crypto_positions >= self.max_crypto_positions:
                return False, (
                    f"Max crypto positions reached "
                    f"({self.max_crypto_positions} for {self.risk_tier})"
                )

            if leverage > self.max_leverage_crypto:
                return False, (
                    f"Leverage {leverage}x exceeds max allowed "
                    f"{self.max_leverage_crypto}x for {self.risk_tier}"
                )

        # ── Margin buffer check ─────────────────────────────────────────
        min_free = self.balance * (self.min_margin_buffer_pct / 100)
        if self.available_balance < min_free:
            return False, (
                f"Insufficient free margin: must keep "
                f"{self.min_margin_buffer_pct}% (₹{min_free:.0f}) free"
            )

        # ── Asset-specific position-size check ──────────────────────────
        if asset_type and position_size:
            max_size = self.get_max_position_size(asset_type)
            if position_size > max_size:
                return False, (
                    f"Position size ₹{position_size} exceeds "
                    f"{asset_type} limit ₹{max_size} for {self.risk_tier}"
                )

        return True, "OK"


# ─────────────────────────────────────────────
# 💳 TOP-UP MODEL
# ─────────────────────────────────────────────
class PaperTopUp(models.Model):
    STATUS = [
        ("pending", "Pending"),
        ("success", "Success"),
        ("failed", "Failed"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(PaperAccount, on_delete=models.CASCADE, related_name="topups")
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    status = models.CharField(max_length=10, choices=STATUS, default="pending")
    payment_id = models.CharField(max_length=100, blank=True)
    provider = models.CharField(max_length=50, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"₹{self.amount} | {self.status}"


# ─────────────────────────────────────────────
# 📊 PAPER TRADE MODEL
# ─────────────────────────────────────────────
class PaperTrade(models.Model):

    class AssetType(models.TextChoices):
        OPTION = "option", "Option"
        FUTURES = "futures", "Futures"
        CRYPTO = "crypto", "Crypto"

    class Side(models.TextChoices):
        BUY = "buy", "Buy"
        SELL = "sell", "Sell"
        LONG = "long", "Long"
        SHORT = "short", "Short"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        CLOSED = "closed", "Closed"

    class ExitReason(models.TextChoices):
        TARGET = "target", "Target Hit"
        SL = "sl", "Stop Loss Hit"
        MANUAL = "manual", "Manual Close"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(PaperAccount, on_delete=models.CASCADE, related_name="trades")
    symbol = models.CharField(max_length=50)
    asset_type = models.CharField(max_length=20, choices=AssetType.choices)
    display_name = models.CharField(max_length=100, blank=True)
    
    side = models.CharField(max_length=10, choices=Side.choices)
    
    quantity = models.DecimalField(max_digits=10, decimal_places=4)
    lot_size = models.IntegerField(default=1)
    leverage = models.IntegerField(default=1)
    entry_price = models.DecimalField(max_digits=14, decimal_places=4)
    current_price = models.DecimalField(max_digits=14, decimal_places=4, default=0)
    stop_loss = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    target_price = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    exit_price = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    strike_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    option_type = models.CharField(max_length=10, blank=True)
    pnl = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    margin_used = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    exit_reason = models.CharField(max_length=50, choices=ExitReason.choices, blank=True)
    setup_type = models.CharField(max_length=50, blank=True)
    strategy_id = models.CharField(max_length=100, blank=True)
    nifty_spot_at_entry = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    opened_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-opened_at"]
        indexes = [
            models.Index(fields=['symbol', 'status']),
            models.Index(fields=['account', 'status']),
        ]

    def __str__(self):
        return f"{self.symbol} {self.side} @ {self.entry_price}"

    def save(self, *args, **kwargs):
        # ✅ Always normalize symbol before saving
        self.symbol = normalize_symbol(self.symbol)
        super().save(*args, **kwargs)

    @property
    def unrealized_pnl(self):
        """Calculate unrealized PnL for open positions"""
        if self.status != "open" or not self.current_price:
            return Decimal("0")
        
        qty = self.quantity * self.lot_size
        cp = Decimal(str(self.current_price))
        ep = Decimal(str(self.entry_price))
        
        # Handle both buy/sell AND long/short
        if self.side in ['buy', 'long']:
            raw = (cp - ep) * qty
        else:  # sell or short
            raw = (ep - cp) * qty
        
        return raw * self.leverage

    @property
    def unrealized_pnl_pct(self):
        """Calculate unrealized PnL %"""
        if not self.margin_used:
            return Decimal("0")
        return (self.unrealized_pnl / self.margin_used * 100).quantize(Decimal("0.01"))