# apps/strategies/serializers.py

from rest_framework import serializers

from .models import Strategy, StrategyPerformanceSnapshot, StrategySignal

ALLOWED_SYMBOLS = [
    # ── Indian (Fyers) ────────────────
    "NIFTY",
    "BANKNIFTY",
    "FINNIFTY",
    "MIDCPNIFTY",
    "SENSEX",
    # ── Crypto (Delta) ────────────────
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

# Symbol → allowed instrument types
SYMBOL_INSTRUMENT_MAP = {
    "NIFTY": ["options", "futures"],
    "BANKNIFTY": ["options", "futures"],
    "FINNIFTY": ["options", "futures"],
    "MIDCPNIFTY": ["options", "futures"],
    "SENSEX": ["options", "futures"],
    "BTCUSDT": ["futures", "perp"],
    "ETHUSDT": ["futures", "perp"],
    "SOLUSDT": ["futures", "perp"],
    "BNBUSDT": ["futures", "perp"],
    "XRPUSDT": ["futures", "perp"],
    "MATICUSDT": ["futures", "perp"],
    "ADAUSDT": ["futures", "perp"],
    "DOGEUSDT": ["futures", "perp"],
    "AVAXUSDT": ["futures", "perp"],
    "LTCUSDT": ["futures", "perp"],
}

# Symbol → correct broker slug
SYMBOL_BROKER_MAP = {
    "NIFTY": "fyers",
    "BANKNIFTY": "fyers",
    "FINNIFTY": "fyers",
    "MIDCPNIFTY": "fyers",
    "SENSEX": "fyers",
    "BTCUSDT": "delta",
    "ETHUSDT": "delta",
    "SOLUSDT": "delta",
    "BNBUSDT": "delta",
    "XRPUSDT": "delta",
    "MATICUSDT": "delta",
    "ADAUSDT": "delta",
    "DOGEUSDT": "delta",
    "AVAXUSDT": "delta",
    "LTCUSDT": "delta",
}


class StrategySerializer(serializers.ModelSerializer):
    is_running = serializers.BooleanField(read_only=True)
    broker_slug = serializers.CharField(read_only=True)
    broker_label = serializers.SerializerMethodField()
    broker_id = serializers.SerializerMethodField()

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
        ]
        read_only_fields = [
            "id",
            "state",
            "is_active", 
            "is_running",
            "broker_slug",
            "broker_label",
            "error_msg",
            "started_at",
            "stopped_at",
            "created_at",
            "updated_at",
        ]

    def get_broker_label(self, obj):
        labels = {"fyers": "Fyers", "delta": "Delta Exchange"}
        return labels.get(obj.broker_slug, obj.broker_slug or "None")

    def get_broker_id(self, obj):
        return str(obj.broker_id) if obj.broker_id else None

    def validate_symbols(self, value):
        invalid = [s for s in value if s.upper() not in ALLOWED_SYMBOLS]
        if invalid:
            raise serializers.ValidationError(f"Invalid symbols: {invalid}")
        return [s.upper() for s in value]


class StrategyWriteSerializer(serializers.ModelSerializer):
    """
    Strategy create/edit ke liye.
    broker field: BrokerCredential ka PK (id)
    instrument_type: options/futures/equity/perp
    risk_config: { sl_pct, target_pct, qty, rr_ratio, max_trades_per_day, sl_type }
    """

    class Meta:
        model = Strategy
        fields = [
            "name",
            "algo_name",
            "symbol",
            "symbols",
            "instrument_type",
            "risk_config",
            "mode",
            "interval_seconds",
            "parameters",
            "broker",
        ]

    def validate_interval_seconds(self, value):
        if value < 10:
            raise serializers.ValidationError("Minimum interval is 10 seconds.")
        return value

    def validate_symbols(self, value):
        invalid = [s for s in value if s.upper() not in ALLOWED_SYMBOLS]
        if invalid:
            raise serializers.ValidationError(f"Invalid symbols: {invalid}")
        return [s.upper() for s in value]

    def validate(self, attrs):
        symbols = attrs.get("symbols", [])
        instrument = attrs.get("instrument_type", "futures")
        broker_cred = attrs.get("broker")

        # ── Symbol → instrument_type compatibility check ──────────
        for sym in symbols:
            allowed = SYMBOL_INSTRUMENT_MAP.get(sym.upper(), [])
            if allowed and instrument not in allowed:
                raise serializers.ValidationError(
                    f"Symbol '{sym}' ke liye instrument_type '{instrument}' allowed nahi. "
                    f"Use: {allowed}"
                )

        # ── Symbol → broker compatibility check ──────────────────
        if broker_cred and symbols:
            for sym in symbols:
                expected_broker = SYMBOL_BROKER_MAP.get(sym.upper())
                if expected_broker and broker_cred.broker != expected_broker:
                    raise serializers.ValidationError(
                        f"Symbol '{sym}' ke liye '{expected_broker}' broker chahiye, "
                        f"lekin '{broker_cred.broker}' connected hai."
                    )

        # ── risk_config defaults fill karo ───────────────────────
        risk = attrs.get("risk_config", {})
        attrs["risk_config"] = {
            "sl_pct": risk.get("sl_pct", 0.5),
            "target_pct": risk.get("target_pct", 1.0),
            "qty": risk.get("qty", 1),
            "rr_ratio": risk.get("rr_ratio", 2.0),
            "max_trades_per_day": risk.get("max_trades_per_day", 3),
            "sl_type": risk.get("sl_type", "candle_hl"),
            **{
                k: v
                for k, v in risk.items()
                if k
                not in (
                    "sl_pct",
                    "target_pct",
                    "qty",
                    "rr_ratio",
                    "max_trades_per_day",
                    "sl_type",
                )
            },
        }

        return attrs


class StrategySignalSerializer(serializers.ModelSerializer):
    class Meta:
        model = StrategySignal
        fields = [
            "id",
            "signal_type",
            "symbol",
            "price",
            "reason",
            "result",
            "order",
            "created_at",
        ]


class StrategyPerformanceSnapshotSerializer(serializers.ModelSerializer):
    class Meta:
        model = StrategyPerformanceSnapshot
        fields = [
            "id",
            "granularity",
            "period_start",
            "total_trades",
            "win_rate",
            "total_pnl",
            "total_fees",
        ]
