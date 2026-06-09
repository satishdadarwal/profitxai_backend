# apps/users/serializers.py

import random
import string
from datetime import timedelta

from django.contrib.auth import authenticate
from django.utils import timezone

from rest_framework import serializers
from rest_framework_simplejwt.tokens import RefreshToken

from .models import BrokerCredential, OTPCode, User


# ── Helpers ───────────────────────────────────────────────────
def _make_otp(length=6):
    return "".join(random.choices(string.digits, k=length))


# ── Plan config — subscription model se lena best hai,
#    lekin fallback ke liye yahan bhi define karo
# ─────────────────────────────────────────────────────────────
def _get_plan_config(user) -> dict:
    """
    Priority:
    1. User ki active Subscription → plan.feature_limits (DB-driven)
    2. Hardcoded fallback (agar subscription nahi bani)
    """
    _FALLBACK = {
        'free':  {'max_brokers': 1,  'max_strategies': 1,  'live_trading': False, 'api_access': False, 'backtesting': False, 'paper_trading': True},
        'basic': {'max_brokers': 2,  'max_strategies': 3,  'live_trading': True,  'api_access': False, 'backtesting': True,  'paper_trading': True},
        'pro':   {'max_brokers': 5,  'max_strategies': 10, 'live_trading': True,  'api_access': True,  'backtesting': True,  'paper_trading': True},
        'elite': {'max_brokers': 10, 'max_strategies': 99, 'live_trading': True,  'api_access': True,  'backtesting': True,  'paper_trading': True},
    }

    try:
        sub = user.subscription  # OneToOne reverse relation
        if sub and sub.plan and sub.plan.feature_limits:
            return sub.plan.feature_limits
    except Exception:
        pass

    return _FALLBACK.get(user.plan, _FALLBACK['free'])


# ── Register ──────────────────────────────────────────────────
class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, min_length=8)
    password2 = serializers.CharField(write_only=True)

    class Meta:
        model = User
        fields = ["email", "full_name", "phone", "password", "password2"]

    def validate(self, data):
        if data["password"] != data["password2"]:
            raise serializers.ValidationError("Passwords do not match")
        return data

    def create(self, validated_data):
        validated_data.pop("password2")
        user = User.objects.create_user(**validated_data)
        OTPCode.objects.create(
            user=user,
            code=_make_otp(),
            purpose="verify",
            expires_at=timezone.now() + timedelta(minutes=15),
        )
        return user


# ── Login ─────────────────────────────────────────────────────
class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)

    def validate(self, data):
        user = authenticate(username=data["email"], password=data["password"])
        if not user:
            raise serializers.ValidationError("Invalid credentials")
        if not user.is_active:
            raise serializers.ValidationError("Account disabled")
        refresh = RefreshToken.for_user(user)
        return {
            "user": UserProfileSerializer(user).data,
            "access_token": str(refresh.access_token),
            "refresh_token": str(refresh),
        }


class OTPResendSerializer(serializers.Serializer):
    email = serializers.EmailField()
    purpose = serializers.ChoiceField(
        choices=["verify", "login", "reset"], default="verify", required=False
    )


# ── OTP Verify ────────────────────────────────────────────────
class OTPVerifySerializer(serializers.Serializer):
    email = serializers.EmailField()
    code = serializers.CharField(max_length=6)
    purpose = serializers.ChoiceField(choices=["verify", "reset", "login"])

    def validate(self, data):
        try:
            user = User.objects.get(email=data["email"])
        except User.DoesNotExist:
            raise serializers.ValidationError("User not found")

        otp = (
            OTPCode.objects.filter(
                user=user,
                code=data["code"],
                purpose=data["purpose"],
                is_used=False,
            )
            .order_by("-created_at")
            .first()
        )

        if not otp or not otp.is_valid():
            raise serializers.ValidationError("Invalid or expired OTP")

        otp.is_used = True
        otp.save()

        if data["purpose"] == "verify":
            user.is_verified = True
            user.save()

        data["user"] = user
        return data


# ── Password Reset ────────────────────────────────────────────
class ForgotPasswordSerializer(serializers.Serializer):
    email = serializers.EmailField()

    def validate_email(self, value):
        if not User.objects.filter(email=value).exists():
            return value
        return value


class ResetPasswordSerializer(serializers.Serializer):
    email = serializers.EmailField()
    code = serializers.CharField(max_length=6)
    new_password = serializers.CharField(min_length=8)

    def validate(self, data):
        try:
            user = User.objects.get(email=data["email"])
        except User.DoesNotExist:
            raise serializers.ValidationError("Invalid request")

        otp = (
            OTPCode.objects.filter(
                user=user, code=data["code"], purpose="reset", is_used=False
            )
            .order_by("-created_at")
            .first()
        )

        if not otp or not otp.is_valid():
            raise serializers.ValidationError("Invalid or expired OTP")

        otp.is_used = True
        otp.save()
        user.set_password(data["new_password"])
        user.save()
        data["user"] = user
        return data


# ── Profile ───────────────────────────────────────────────────
class UserProfileSerializer(serializers.ModelSerializer):
    trading_profile = serializers.SerializerMethodField()

    def get_trading_profile(self, obj) -> dict:
        try:
            from apps.risk.models import TradingProfile
            tp = TradingProfile.objects.get(user=obj)
            return {"exit_mode": tp.exit_mode, "risk_per_trade_pct": str(tp.risk_per_trade_pct or "0.10")}
        except Exception:
            return {"exit_mode": "gtt_oco", "risk_per_trade_pct": "0.10"}
    # ✅ plan_config — subscription se lao, fallback hardcoded
    plan_config = serializers.SerializerMethodField()

    def get_plan_config(self, obj) -> dict:
        return _get_plan_config(obj)

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "full_name",
            "phone",
            "plan",
            "plan_expires",
            "is_verified",
            "date_joined",
            "plan_config",
            "trading_profile",
        ]
        read_only_fields = [
            "id",
            "email",
            "plan",
            "plan_expires",
            "is_verified",
            "date_joined",
            "plan_config",
        ]


# ── Broker Credential ─────────────────────────────────────────
class BrokerCredentialSerializer(serializers.ModelSerializer):
    class Meta:
        model = BrokerCredential
        fields = [
            "id",
            "broker_slug",
            "label",
            "is_active",
            "is_verified",
            "last_used",
            "created_at",
        ]
        read_only_fields = ["id", "is_verified", "last_used", "created_at"]


class BrokerCredentialWriteSerializer(serializers.Serializer):
    """Used when saving broker credentials — broker-specific fields."""

    broker_slug = serializers.CharField(max_length=30)
    label = serializers.CharField(max_length=50, required=False, default="")
    credentials = serializers.DictField()