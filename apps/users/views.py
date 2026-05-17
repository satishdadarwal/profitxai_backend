from datetime import timedelta

from django.utils import timezone

from drf_yasg.utils import swagger_auto_schema
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from broker_adapters.registry import BrokerRegistry
from core.security import decrypt_credentials, encrypt_credentials

from .models import BrokerCredential, OTPCode, User
from .serializers import (
    BrokerCredentialSerializer,
    BrokerCredentialWriteSerializer,
    ForgotPasswordSerializer,
    LoginSerializer,
    OTPResendSerializer,
    OTPVerifySerializer,
    RegisterSerializer,
    ResetPasswordSerializer,
    UserProfileSerializer,
)
from .tasks import send_otp_email


# ── Helpers ───────────────────────────────────────────────────
def ok(data=None, msg="Success", status_code=200):
    return Response({"success": True, "message": msg, "data": data}, status=status_code)


def err(msg="Error", status_code=400):
    return Response({"success": False, "message": msg}, status=status_code)


# ── Register ──────────────────────────────────────────────────
class RegisterView(APIView):
    permission_classes = [AllowAny]

    @swagger_auto_schema(request_body=RegisterSerializer)
    def post(self, request):
        s = RegisterSerializer(data=request.data)
        if not s.is_valid():
            return Response({"success": False, "errors": s.errors}, status=400)

        user = s.save()
        otp = OTPCode.objects.filter(user=user, purpose="verify").first()
        send_otp_email.delay(user.email, otp.code, "verify")

        return ok({"email": user.email}, "Registered — check email for OTP", 201)


# ── Login ─────────────────────────────────────────────────────
class LoginView(APIView):
    permission_classes = [AllowAny]

    @swagger_auto_schema(request_body=LoginSerializer)
    def post(self, request):
        print("📥 LOGIN DATA:", request.data)

        s = LoginSerializer(data=request.data)
        if not s.is_valid():
            print("❌ ERRORS:", s.errors)
            return Response({"success": False, "errors": s.errors}, status=400)

        return ok(s.validated_data, "Login successful")


# ── OTP Verify ────────────────────────────────────────────────
class OTPVerifyView(APIView):
    permission_classes = [AllowAny]

    @swagger_auto_schema(request_body=OTPVerifySerializer)
    def post(self, request):
        s = OTPVerifySerializer(data=request.data)
        if not s.is_valid():
            return err(s.errors)
        user = s.validated_data["user"]
        if s.validated_data.get("purpose") == "login":
            refresh = RefreshToken.for_user(user)
        return ok(
            {
                "access_token": str(refresh.access_token),
                "refresh_token": str(refresh),
            }
        )
        return ok(msg="Verified")


# ── OTP Resend ────────────────────────────────────────────────
class OTPResendView(APIView):
    permission_classes = [AllowAny]

    @swagger_auto_schema(request_body=OTPResendSerializer)
    def post(self, request):
        s = OTPResendSerializer(data=request.data)
        if not s.is_valid():
            return err(s.errors)

        email = s.validated_data.get("email", "").strip()
        purpose = s.validated_data.get("purpose", "verify")

        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            return ok(msg="If email exists, OTP sent")

        # Invalidate old OTPs
        OTPCode.objects.filter(user=user, purpose=purpose, is_used=False).update(
            is_used=True
        )

        # Create new OTP
        otp = OTPCode.objects.create(
            user=user,
            purpose=purpose,
            code=str(__import__("random").randint(100000, 999999)),
            expires_at=timezone.now() + timedelta(minutes=15),
        )

        # Send email async
        send_otp_email.delay(user.email, otp.code, purpose)

        return ok(msg="OTP sent")


class ForgotPasswordView(APIView):
    permission_classes = [AllowAny]

    @swagger_auto_schema(request_body=ForgotPasswordSerializer)
    def post(self, request):
        s = ForgotPasswordSerializer(data=request.data)
        s.is_valid(raise_exception=True)

        email = s.validated_data["email"]

        try:
            user = User.objects.get(email=email)

            OTPCode.objects.filter(user=user, purpose="reset", is_used=False).update(
                is_used=True
            )

            otp = OTPCode.objects.create(
                user=user,
                purpose="reset",
                code=str(__import__("random").randint(100000, 999999)),
                expires_at=timezone.now() + timedelta(minutes=15),
            )

            send_otp_email.delay(user.email, otp.code, "reset")

        except User.DoesNotExist:
            pass

        return ok(msg="If email exists, OTP sent")


# ── Reset Password ────────────────────────────────────────────
class ResetPasswordView(APIView):
    permission_classes = [AllowAny]

    @swagger_auto_schema(request_body=ResetPasswordSerializer)
    def post(self, request):
        s = ResetPasswordSerializer(data=request.data)

        if not s.is_valid():
            return Response({"success": False, "errors": s.errors}, status=400)

        return ok(msg="Password reset successful")


# ── Profile ───────────────────────────────────────────────────
class ProfileView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return ok(UserProfileSerializer(request.user).data)

    @swagger_auto_schema(request_body=UserProfileSerializer)
    def patch(self, request):
        s = UserProfileSerializer(request.user, data=request.data, partial=True)

        if not s.is_valid():
            return Response({"success": False, "errors": s.errors}, status=400)

        s.save()
        return ok(s.data, "Profile updated")


# ── Broker List ───────────────────────────────────────────────
class BrokerListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        brokers = BrokerRegistry.list_brokers()
        return ok(brokers)


# ── User Broker ───────────────────────────────────────────────
class UserBrokerView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        creds = BrokerCredential.objects.filter(user=request.user)
        return ok(BrokerCredentialSerializer(creds, many=True).data)

    @swagger_auto_schema(request_body=BrokerCredentialWriteSerializer)
    def post(self, request):
        s = BrokerCredentialWriteSerializer(data=request.data)

        if not s.is_valid():
            return Response({"success": False, "errors": s.errors}, status=400)

        slug = s.validated_data["broker_slug"]
        label = s.validated_data.get("label", "") or slug
        creds = s.validated_data["credentials"]

        if not BrokerRegistry.has(slug):
            return err(f'Broker "{slug}" not supported')

        adapter_cls = BrokerRegistry.get(slug)
        missing = [f for f in adapter_cls.REQUIRED_CREDENTIAL_FIELDS if f not in creds]

        if missing:
            return err(f'Missing fields: {", ".join(missing)}')

        encrypted = encrypt_credentials(creds)

        obj, created = BrokerCredential.objects.update_or_create(
            user=request.user,
            broker_slug=slug,
            label=label,
            defaults={
                "credentials": encrypted,
                "is_active": True,
                "is_verified": False,
            },
        )

        return ok(
            BrokerCredentialSerializer(obj).data,
            "Saved" if created else "Updated",
            201 if created else 200,
        )


# ── Broker Verify ─────────────────────────────────────────────
class BrokerVerifyView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, broker_slug):
        try:
            cred = BrokerCredential.objects.get(
                user=request.user, broker_slug=broker_slug, is_active=True
            )
        except BrokerCredential.DoesNotExist:
            return err("Broker not connected", 404)

        try:
            decrypted = decrypt_credentials(cred.credentials)
            adapter = BrokerRegistry.get(broker_slug)(decrypted)

            result = adapter.verify_connection()

            cred.is_verified = result.get("success", False)
            cred.last_used = timezone.now()
            cred.save()

            return ok(result)

        except Exception as e:
            return err(str(e))


# ── Broker Delete ─────────────────────────────────────────────
class BrokerDeleteView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, pk):
        try:
            cred = BrokerCredential.objects.get(pk=pk, user=request.user)
            cred.delete()
            return ok(msg="Removed")

        except BrokerCredential.DoesNotExist:
            return err("Not found", 404)
