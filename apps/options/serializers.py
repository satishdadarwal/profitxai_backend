# apps/options/serializers.py
# COMPLETE PRODUCTION-READY VERSION

from rest_framework import serializers
from django.utils import timezone

from apps.options.models import (
    BacktestRun,
    OptionContract,
    OptionSnapshot,
    OptionSymbol,
    OptionsPrediction,
)

# Paper trading app models
from apps.paper_trading.models import (
    PaperAccount,
    PaperTopUp,
)


class OptionSymbolSerializer(serializers.ModelSerializer):
    class Meta:
        model = OptionSymbol
        fields = ["id", "name", "fyers_symbol", "lot_size", "strike_step", "is_active"]


class OptionContractSerializer(serializers.ModelSerializer):
    symbol = OptionSymbolSerializer(read_only=True)

    class Meta:
        model = OptionContract
        fields = ["id", "symbol", "strike", "option_type", "expiry", "fyers_symbol"]


class OptionSnapshotSerializer(serializers.ModelSerializer):
    contract = OptionContractSerializer(read_only=True)

    class Meta:
        model = OptionSnapshot
        fields = [
            "id",
            "contract",
            "ltp",
            "oi",
            "volume",
            "iv",
            "delta",
            "theta",
            "spot_price",
            "timestamp",
        ]


class PaperAccountSerializer(serializers.ModelSerializer):
    user = serializers.StringRelatedField(read_only=True)

    class Meta:
        model = PaperAccount
        fields = ["id", "user", "balance", "initial_capital", "total_pnl"]


class PaperTopUpSerializer(serializers.ModelSerializer):
    account = PaperAccountSerializer(read_only=True)

    class Meta:
        model = PaperTopUp
        fields = ["id", "account", "amount", "status", "payment_id", "provider", "created_at"]


class BacktestRunSerializer(serializers.ModelSerializer):
    user = serializers.StringRelatedField(read_only=True)
    symbol = OptionSymbolSerializer(read_only=True)

    class Meta:
        model = BacktestRun
        fields = [
            "id",
            "user",
            "symbol",
            "from_date",
            "to_date",
            "strategy",
            "initial_capital",
            "status",
            "final_capital",
            "total_pnl",
            "win_rate",
            "max_drawdown",
            "total_trades",
            "error_message",
            "created_at",
            "completed_at",
        ]

class OptionsPredictionSerializer(serializers.ModelSerializer):
    symbol_name = serializers.CharField(source="symbol.name", read_only=True)

    class Meta:
        model = OptionsPrediction
        fields = [
            "id", "symbol_name", "expiry", "direction", "confidence_pct",
            "signal_score", "expected_range_low", "expected_range_high",
            "max_pain", "call_wall", "put_wall", "breakeven_pts",
            "up_prob", "flat_prob", "down_prob", "suggested_strategy",
            "strategy_legs", "pcr_oi", "iv_rank", "signal_factors", "created_at",
        ]
