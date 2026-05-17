# apps/subscriptions/serializers.py

from rest_framework import serializers

from .models import PaymentLog, Plan, Subscription


class PlanSerializer(serializers.ModelSerializer):
    allows_live_trading = serializers.BooleanField(read_only=True)
    max_brokers = serializers.IntegerField(read_only=True)
    max_strategies = serializers.IntegerField(read_only=True)

    class Meta:
        model = Plan
        fields = [
            "id",
            "name",
            "tier",
            "billing_cycle",
            "price_inr",
            "feature_limits",
            "allows_live_trading",
            "max_brokers",
            "max_strategies",
            "razorpay_plan_id",
        ]


class SubscriptionSerializer(serializers.ModelSerializer):
    plan = PlanSerializer(read_only=True)
    tier = serializers.IntegerField(read_only=True)
    is_access_granted = serializers.BooleanField(read_only=True)
    is_pro_or_above = serializers.BooleanField(read_only=True)
    days_until_renewal = serializers.IntegerField(read_only=True, allow_null=True)

    class Meta:
        model = Subscription
        fields = [
            "id",
            "plan",
            "tier",
            "status",
            "is_access_granted",
            "is_pro_or_above",
            "current_period_start",
            "current_period_end",
            "trial_end",
            "grace_until",
            "days_until_renewal",
            "razorpay_subscription_id",
            "created_at",
            "updated_at",
        ]


class CreateOrderSerializer(serializers.Serializer):
    plan_id = serializers.UUIDField()


class VerifyPaymentSerializer(serializers.Serializer):
    razorpay_order_id = serializers.CharField(max_length=100)
    razorpay_payment_id = serializers.CharField(max_length=100)
    razorpay_signature = serializers.CharField(max_length=256)


class PaymentLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaymentLog
        fields = [
            "id",
            "razorpay_order_id",
            "razorpay_payment_id",
            "amount_inr",
            "currency",
            "payment_status",
            "failure_reason",
            "created_at",
        ]
