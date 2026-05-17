from rest_framework import serializers
from .models import PaperAccount, PaperTopUp, PaperTrade


# ─────────────────────────────────────────────
# TRADE SERIALIZER
# ─────────────────────────────────────────────
class PaperTradeSerializer(serializers.ModelSerializer):
    unrealized_pnl = serializers.SerializerMethodField()
    unrealized_pnl_pct = serializers.SerializerMethodField()
    is_open = serializers.SerializerMethodField()


    class Meta:
        model = PaperTrade
        fields = [
            "id",

            # ── Symbol
            "symbol",
            "asset_type",
            "display_name",

            # ── Trade
            "side",
            "quantity",
            "lot_size",
            "leverage",

            # ── Prices
            "entry_price",
            "current_price",
            "stop_loss",
            "target_price",
            "exit_price",

            # ── Option
            "strike_price",
            "option_type",

            # ── Financials
            "pnl",
            "margin_used",

            # ── Status
            "status",
            "is_open",
            "exit_reason",

            # ── Strategy
            "setup_type",
            "strategy_id",
            "nifty_spot_at_entry",

            # ── Time
            "opened_at",
            "closed_at",

            # ── Calculated
            "unrealized_pnl",
            "unrealized_pnl_pct",
        ]

        read_only_fields = [
            "id",
            "pnl",
            "margin_used",
            "opened_at",
            "closed_at",
        ]

    def get_unrealized_pnl(self, obj):
        return round(float(obj.unrealized_pnl), 2)

    def get_unrealized_pnl_pct(self, obj):
        return round(float(obj.unrealized_pnl_pct), 2)
     
    def get_is_open(self, obj):
        return obj.status == "open"


# ─────────────────────────────────────────────
# ACCOUNT SERIALIZER
# ─────────────────────────────────────────────
class PaperAccountSerializer(serializers.ModelSerializer):
    # PnL fields
    total_pnl = serializers.SerializerMethodField()
    unrealized_pnl = serializers.SerializerMethodField()
    net_pnl = serializers.SerializerMethodField()
    todays_pnl = serializers.SerializerMethodField()

    # Trade counts
    open_trades = serializers.SerializerMethodField()
    closed_trades = serializers.SerializerMethodField()

    # Risk status
    can_trade = serializers.SerializerMethodField()
    can_open_trade = serializers.SerializerMethodField()
    trade_limit_reason = serializers.SerializerMethodField()
    daily_limit_hit = serializers.SerializerMethodField()
    daily_loss_limit_amount = serializers.SerializerMethodField()
    
    # Extra insights (NEW)
    risk_status = serializers.SerializerMethodField()
    margin_usage_pct = serializers.SerializerMethodField()
    
    # User flags
    is_paid_user = serializers.SerializerMethodField()

    class Meta:
        model = PaperAccount
        fields = [
            "id",

            # ── Balance
            "balance",
            "initial_capital",
            "total_withdrawn",
            "available_balance",
            "margin_used",

            # ── Plan
            "free_limit",
            "is_free_plan",
            "total_topup",

            # ── Risk Management Settings
            "daily_loss_limit_pct",
            "daily_loss_limit_fixed",
            "use_percentage_limit",
            "max_open_trades",
            "risk_per_trade_pct",
            "risk_tier",
            "max_crypto_positions",
            "current_crypto_positions",
            "max_leverage_crypto",
            "min_margin_buffer_pct",

            # ── PnL
            "total_pnl",
            "unrealized_pnl",
            "net_pnl",
            "todays_pnl",

            # ── Trades
            "open_trades",
            "closed_trades",

            # ── Risk Status
            "can_trade",
            "can_open_trade",
            "trade_limit_reason",
            "daily_limit_hit",
            "daily_loss_limit_amount",
            "risk_status",
            "margin_usage_pct",

            "is_paid_user",
            "is_active",

            # ── Meta
            "created_at",
            "updated_at",
        ]

    # ───────── PnL ─────────
    def get_total_pnl(self, obj):
        return round(float(obj.total_pnl), 2)

    def get_unrealized_pnl(self, obj):
        return round(float(obj.unrealized_pnl), 2)

    def get_net_pnl(self, obj):
        return round(float(obj.net_pnl), 2)

    def get_todays_pnl(self, obj):
        return round(float(obj.todays_realized_pnl), 2)

    # ───────── Trades ─────────
    def get_open_trades(self, obj):
        qs = obj.trades.filter(status="open")[:20]  # performance safe
        return PaperTradeSerializer(qs, many=True).data

    def get_closed_trades(self, obj):
        qs = obj.trades.filter(status="closed").order_by("-closed_at")[:50]
        return PaperTradeSerializer(qs, many=True).data

    # ───────── Risk Status ─────────
    def get_can_trade(self, obj):
        return obj.can_trade

    def get_can_open_trade(self, obj):
        return obj.can_trade  # cleaner + accurate

    def get_trade_limit_reason(self, obj):
        _, reason = obj.can_open_new_trade(asset_type="crypto")
        return reason if reason != "OK" else ""

    def get_daily_limit_hit(self, obj):
        return obj.is_daily_loss_limit_hit

    def get_daily_loss_limit_amount(self, obj):
        return round(float(obj.daily_loss_limit_amount), 2)

    def get_is_paid_user(self, obj):
        return obj.is_paid_user

    def get_risk_status(self, obj):
        if obj.is_daily_loss_limit_hit:
            return "blocked"
        elif obj.available_balance <= 0:
            return "no_balance"
        return "active"

    def get_margin_usage_pct(self, obj):
        total = obj.balance + obj.margin_used
        if total == 0:
            return 0
        return round(float(obj.margin_used / total * 100), 2)

    def get_risk_tier(self, obj):
        return obj.risk_tier

    def get_max_crypto_positions(self, obj):
        return obj.max_crypto_positions

    def get_current_crypto_positions(self, obj):
        return obj.current_crypto_positions

    def get_max_leverage_crypto(self, obj):
        return obj.max_leverage_crypto

    def get_min_margin_buffer_pct(self, obj):
        return round(float(obj.min_margin_buffer_pct), 2)


# ─────────────────────────────────────────────
# TOPUP SERIALIZER
# ─────────────────────────────────────────────
class PaperTopUpSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaperTopUp
        fields = [
            "id",
            "amount",
            "status",
            "payment_id",
            "provider",
            "created_at",
        ]
        read_only_fields = ["id", "status", "created_at"]