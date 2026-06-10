# apps/strategies/serializers.py
#
# CHANGES:
#   [Global Strategy feature] is_global, allowed_plans, created_by_admin, is_editable add kiye
#   [Symbol Fix] NIFTYNXT50 + BANKEX add kiye (fyers_utils + seed_symbols mein tha, yahan missing tha)
#   [Equity Stocks] Open-ended equity stock support add kiya — NSE/BSE stocks freely allowed
#     Rationale: signal_router.py mein _fyers_equity_order already hai,
#                isliye arbitrary stock symbols block karna galat tha.

from rest_framework import serializers

from .models import Strategy, StrategyPerformanceSnapshot, StrategySignal

# ─────────────────────────────────────────────────────────────────
#  INDEX symbols — strict whitelist (options/futures ke liye)
#  Yeh sirf INDEX instruments hain — inka broker bhi fixed hai
# ─────────────────────────────────────────────────────────────────
INDEX_SYMBOLS = [
    # ── NSE Indices ─────────────────────────
    "NIFTY",
    "BANKNIFTY",
    "FINNIFTY",
    "MIDCPNIFTY",
    "NIFTYNXT50",   # ✅ ADD: seed_symbols.py + options mein tha, serializer mein missing tha
    # ── BSE Indices ─────────────────────────
    "SENSEX",
    "BANKEX",       # ✅ ADD: fyers_utils.py mein LOT_SIZES/STRIKE_STEPS tha, yahan missing tha
    # ── Crypto (Delta Exchange) ─────────────
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "BNBUSDT",
    "XRPUSDT",
    "MATICUSDT",
    "ADAUSDT",
    "DOGEUSDT",
    "AVAXUSDT",
    "LTCUSDT",
]

# ─────────────────────────────────────────────────────────────────
#  Index → allowed instrument types mapping
# ─────────────────────────────────────────────────────────────────
SYMBOL_INSTRUMENT_MAP = {
    # NSE Indices
    "NIFTY":       ["options", "futures"],
    "BANKNIFTY":   ["options", "futures"],
    "FINNIFTY":    ["options", "futures"],
    "MIDCPNIFTY":  ["options", "futures"],
    "NIFTYNXT50":  ["options", "futures"],   # ✅ ADD
    # BSE Indices
    "SENSEX":      ["options", "futures"],
    "BANKEX":      ["options", "futures"],   # ✅ ADD
    # Crypto
    "BTCUSDT":  ["futures", "perp"],
    "ETHUSDT":  ["futures", "perp"],
    "SOLUSDT":  ["futures", "perp"],
    "BNBUSDT":  ["futures", "perp"],
    "XRPUSDT":  ["futures", "perp"],
    "MATICUSDT":["futures", "perp"],
    "ADAUSDT":  ["futures", "perp"],
    "DOGEUSDT": ["futures", "perp"],
    "AVAXUSDT": ["futures", "perp"],
    "LTCUSDT":  ["futures", "perp"],
}

# ─────────────────────────────────────────────────────────────────
#  Index → broker mapping (crypto = delta, baaki = fyers)
# ─────────────────────────────────────────────────────────────────
SYMBOL_BROKER_MAP = {
    "NIFTY":      "fyers",
    "BANKNIFTY":  "fyers",
    "FINNIFTY":   "fyers",
    "MIDCPNIFTY": "fyers",
    "NIFTYNXT50": "fyers",   # ✅ ADD
    "SENSEX":     "fyers",
    "BANKEX":     "fyers",   # ✅ ADD: BSE, but still via Fyers
    "BTCUSDT":  "delta",
    "ETHUSDT":  "delta",
    "SOLUSDT":  "delta",
    "BNBUSDT":  "delta",
    "XRPUSDT":  "delta",
    "MATICUSDT":"delta",
    "ADAUSDT":  "delta",
    "DOGEUSDT": "delta",
    "AVAXUSDT": "delta",
    "LTCUSDT":  "delta",
}

# ─────────────────────────────────────────────────────────────────
#  Equity stock symbol validation helper
#  NSE/BSE stocks: RELIANCE, TCS, INFY, HDFC, etc.
#  Yeh sirf equity instrument_type ke saath kaam karte hain
# ─────────────────────────────────────────────────────────────────
def _is_valid_equity_stock(symbol: str) -> bool:
    """
    Equity stocks ke liye basic validation.
    Rules:
     - Alphabets only (hyphens allowed for some BSE symbols)
     - Max 20 chars
     - Already NSE:/BSE: prefix ke saath bhi accept karo
     - Crypto symbols reject karo (USDT se end hote hain)
    """
    s = symbol.upper().strip()
    # NSE:/BSE: prefix strip karo
    for prefix in ("NSE:", "BSE:"):
        if s.startswith(prefix):
            s = s[len(prefix):]
    # -EQ suffix strip (Fyers equity format)
    if s.endswith("-EQ"):
        s = s[:-3]
    # Crypto reject
    if s.endswith("USDT") or s.endswith("USD") or "-USDT" in s:
        return False
    # Basic format check: alphanumeric + hyphen, reasonable length
    import re
    return bool(re.match(r'^[A-Z0-9&\-]{1,20}$', s))


class StrategySerializer(serializers.ModelSerializer):
    is_running = serializers.BooleanField(read_only=True)
    broker_slug = serializers.CharField(read_only=True)
    broker_label = serializers.SerializerMethodField()
    broker_id = serializers.SerializerMethodField()
    is_editable = serializers.SerializerMethodField()
    # ✅ User ka preferred mode (paper/live) — per-user preference
    preferred_mode  = serializers.SerializerMethodField()
    effective_mode  = serializers.SerializerMethodField()
    can_live_trade  = serializers.SerializerMethodField()
    exit_mode       = serializers.SerializerMethodField()

    class Meta:
        model = Strategy
        fields = [
            "id",
            "name",
            "algo_name",
            "symbol",
            "symbols",
            "instrument_type",
            "risk_config",
            "mode",
            "state",
            "is_active",
            "is_running",
            "broker_id",
            "broker_slug",
            "broker_label",
            "interval_seconds",
            "parameters",
            "error_msg",
            "started_at",
            "stopped_at",
            "created_at",
            "updated_at",
            # Global Strategy fields
            "is_global",
            "allowed_plans",
            "created_by_admin",
            "is_editable",
            # ✅ User preference fields
            "preferred_mode",
            "effective_mode",
            "can_live_trade",
            "exit_mode",
        ]
        read_only_fields = [
            "id", "state", "is_active", "is_running",
            "broker_slug", "broker_label", "error_msg",
            "started_at", "stopped_at", "created_at", "updated_at",
            "is_global", "allowed_plans", "created_by_admin", "is_editable",
            "preferred_mode", "effective_mode", "can_live_trade", "exit_mode",
        ]

    def get_broker_label(self, obj):
        labels = {"fyers": "Fyers", "delta": "Delta Exchange"}
        return labels.get(obj.broker_slug, obj.broker_slug or "None")

    def get_broker_id(self, obj):
        return str(obj.broker_id) if obj.broker_id else None

    def get_is_editable(self, obj):
        request = self.context.get("request")
        if not request:
            return not obj.is_global
        if request.user.is_staff:
            return True
        return not obj.is_global

    def get_preferred_mode(self, obj):
        """
        User ka preferred mode fetch karo.
        Agar preference nahi hai → strategy ka master mode return karo.
        ✅ FIX: Yeh field `strategies/` list API mein inject hota hai
        taaki Flutter ko separate preference/ call ki zaroorat nahi.
        """
        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            return obj.mode
        try:
            from .models import UserStrategyPreference
            pref = UserStrategyPreference.objects.filter(
                user=request.user, strategy=obj
            ).first()
            return pref.preferred_mode if pref else obj.mode
        except Exception:
            return obj.mode

    def get_effective_mode(self, obj):
        """Effective mode — preferred_mode se hi aata hai."""
        return self.get_preferred_mode(obj)

    def get_can_live_trade(self, obj):
        """User live trading kar sakta hai?"""
        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            return False
        try:
            from .services import _user_can_live_trade
            return _user_can_live_trade(request.user)
        except Exception:
            return False

    def get_exit_mode(self, obj):
        """User ka per-strategy exit mode — fallback: trading_profile > default."""
        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            return 'gtt_oco'
        try:
            from .models import UserStrategyPreference
            pref = UserStrategyPreference.objects.filter(
                user=request.user, strategy=obj
            ).first()
            if pref:
                return pref.exit_mode
        except Exception:
            pass
        try:
            return request.user.trading_profile.exit_mode
        except Exception:
            return 'gtt_oco'

    def validate_symbols(self, value):
        errors = []
        for sym in value:
            s = sym.upper()
            # Index/Crypto whitelist
            if s in INDEX_SYMBOLS:
                continue
            # Equity stocks — instrument_type check baad mein validate() mein hoga
            # Yahan sirf format check karo
            if not _is_valid_equity_stock(s):
                errors.append(s)
        if errors:
            raise serializers.ValidationError(
                f"Invalid symbols: {errors}. "
                f"Allowed: {INDEX_SYMBOLS} ya NSE/BSE equity stocks (e.g. RELIANCE, TCS)"
            )
        return [s.upper() for s in value]


class StrategyWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Strategy
        fields = [
            "name", "algo_name", "symbol", "symbols",
            "instrument_type", "risk_config", "mode",
            "interval_seconds", "parameters", "broker",
        ]

    def validate_interval_seconds(self, value):
        if value < 10:
            raise serializers.ValidationError("Minimum interval is 10 seconds.")
        return value

    def validate_symbols(self, value):
        errors = []
        for sym in value:
            s = sym.upper()
            if s in INDEX_SYMBOLS:
                continue
            if not _is_valid_equity_stock(s):
                errors.append(s)
        if errors:
            raise serializers.ValidationError(f"Invalid symbols: {errors}")
        return [s.upper() for s in value]

    def validate(self, attrs):
        symbols   = attrs.get("symbols", [])
        instrument = attrs.get("instrument_type", "futures")
        broker_cred = attrs.get("broker")

        for sym in symbols:
            s = sym.upper()

            if s in INDEX_SYMBOLS:
                # ── Index/Crypto: strict instrument check ────────
                allowed = SYMBOL_INSTRUMENT_MAP.get(s, [])
                if allowed and instrument not in allowed:
                    raise serializers.ValidationError(
                        f"'{s}' ke liye instrument_type '{instrument}' allowed nahi. "
                        f"Use: {allowed}"
                    )
                # Broker compatibility
                if broker_cred:
                    expected = SYMBOL_BROKER_MAP.get(s)
                    if expected and broker_cred.broker != expected:
                        raise serializers.ValidationError(
                            f"'{s}' ke liye '{expected}' broker chahiye, "
                            f"lekin '{broker_cred.broker}' connected hai."
                        )
            else:
                # ── Equity stock: sirf 'equity' instrument allowed ──
                if instrument != "equity":
                    raise serializers.ValidationError(
                        f"'{s}' ek equity stock hai. Iske saath instrument_type='equity' use karo, "
                        f"'{instrument}' nahi."
                    )
                # Equity stocks sirf Fyers pe kaam karti hain
                if broker_cred and broker_cred.broker != "fyers":
                    raise serializers.ValidationError(
                        f"Equity stocks sirf Fyers pe trade hoti hain. "
                        f"'{broker_cred.broker}' broker use nahi ho sakta."
                    )

        risk = attrs.get("risk_config", {})
        attrs["risk_config"] = {
            "sl_pct":             risk.get("sl_pct", 0.5),
            "target_pct":         risk.get("target_pct", 1.0),
            "qty":                risk.get("qty", 1),
            "rr_ratio":           risk.get("rr_ratio", 2.0),
            "max_trades_per_day": risk.get("max_trades_per_day", 50),
            "sl_type":            risk.get("sl_type", "candle_hl"),
            **{
                k: v for k, v in risk.items()
                if k not in (
                    "sl_pct", "target_pct", "qty", "rr_ratio",
                    "max_trades_per_day", "sl_type"
                )
            },
        }
        return attrs


class StrategySignalSerializer(serializers.ModelSerializer):
    class Meta:
        model = StrategySignal
        fields = [
            "id", "signal_type", "symbol", "price",
            "reason", "result", "order", "created_at",
        ]


class StrategyPerformanceSnapshotSerializer(serializers.ModelSerializer):
    class Meta:
        model = StrategyPerformanceSnapshot
        fields = [
            "id", "granularity", "period_start",
            "total_trades", "win_rate", "total_pnl", "total_fees",
        ]