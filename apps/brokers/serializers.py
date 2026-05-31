# apps/brokers/serializers.py
from rest_framework import serializers
from .models import BrokerAccount, BrokerOrder


class BrokerAccountSerializer(serializers.ModelSerializer):
    """
    Write: user credentials store karo
    Read:  sensitive fields (secret_key, access_token, etc.) NEVER expose karo
    """

    class Meta:
        model  = BrokerAccount
        fields = [
            "id", "broker", "label",
            "app_id",       # Fyers — write only
            "secret_key",   # Fyers — write only
            "api_key",      # Zerodha/Delta — write only
            "api_secret",   # Delta — write only
            "redirect_uri",
            "is_active", "is_verified",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "is_verified", "created_at", "updated_at"]
        # Sensitive fields — sirf write, read pe blank
        extra_kwargs = {
            "secret_key": {"write_only": True},
            "app_id":     {"write_only": True},
            "api_key":    {"write_only": True},
            "api_secret": {"write_only": True, "required": False, "allow_null": True, "allow_blank": True},
        }


class BrokerOrderSerializer(serializers.ModelSerializer):
    broker_name  = serializers.CharField(source="broker_account.broker", read_only=True)
    trade_id     = serializers.PrimaryKeyRelatedField(
        source="option_trade", read_only=True
    )

    class Meta:
        model  = BrokerOrder
        fields = [
            "id", "broker_name", "trade_id",
            "order_type", "exchange_order_id",
            "status", "rejection_reason",
            "retry_count", "max_retries",
            "placed_at", "sent_to_broker_at",
            "executed_at", "cancelled_at",
            "notes",
            # broker_response intentionally excluded — debug only, use admin
        ]
        read_only_fields = fields   # API se sirf read, koi write nahi