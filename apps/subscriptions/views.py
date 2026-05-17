# apps/subscriptions/views.py

import hashlib
import hmac
import json
import logging

from django.conf import settings
from django.http import JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import PaymentLog, Plan
from .serializers import (
    CreateOrderSerializer,
    PaymentLogSerializer,
    PlanSerializer,
    SubscriptionSerializer,
    VerifyPaymentSerializer,
)
from .services import (
    PaymentVerificationError,
    PlanNotFoundError,
    SubscriptionError,
    cancel_subscription,
    change_plan,
    create_razorpay_order,
    get_or_create_free_subscription,
    verify_and_activate,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
#  1. Plan catalog
# ─────────────────────────────────────────────────────────────────
class PlanListView(APIView):
    permission_classes = []

    def get(self, request):
        plans = Plan.objects.filter(is_active=True).order_by("tier", "billing_cycle")
        return Response(PlanSerializer(plans, many=True).data)


# ─────────────────────────────────────────────────────────────────
#  2. Current subscription
# ─────────────────────────────────────────────────────────────────
class CurrentSubscriptionView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        sub = getattr(request, "subscription", None)
        if sub is None:
            sub = get_or_create_free_subscription(request.user)
        return Response(SubscriptionSerializer(sub).data)


# ─────────────────────────────────────────────────────────────────
#  3. Create Razorpay order
# ─────────────────────────────────────────────────────────────────
class CreateOrderView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        ser = CreateOrderSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)

        try:
            order_data = create_razorpay_order(
                user=request.user,
                plan_id=str(ser.validated_data["plan_id"]),
            )
        except PlanNotFoundError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_404_NOT_FOUND)
        except SubscriptionError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(order_data, status=status.HTTP_201_CREATED)


# ─────────────────────────────────────────────────────────────────
#  4. Verify payment
# ─────────────────────────────────────────────────────────────────
class VerifyPaymentView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        ser = VerifyPaymentSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)

        d = ser.validated_data
        try:
            sub = verify_and_activate(
                user=request.user,
                razorpay_order_id=d["razorpay_order_id"],
                razorpay_payment_id=d["razorpay_payment_id"],
                razorpay_signature=d["razorpay_signature"],
            )
        except PaymentVerificationError as exc:
            logger.warning(
                "Payment verification failed | user=%s | %s", request.user.id, exc
            )
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except SubscriptionError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            {
                "message": "Subscription activated successfully.",
                "subscription": SubscriptionSerializer(sub).data,
            }
        )


# ─────────────────────────────────────────────────────────────────
#  5. Change plan
# ─────────────────────────────────────────────────────────────────
class ChangePlanView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        ser = CreateOrderSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)

        try:
            order_data = change_plan(
                user=request.user,
                new_plan_id=str(ser.validated_data["plan_id"]),
            )
        except PlanNotFoundError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_404_NOT_FOUND)
        except SubscriptionError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(order_data, status=status.HTTP_201_CREATED)


# ─────────────────────────────────────────────────────────────────
#  6. Cancel subscription
# ─────────────────────────────────────────────────────────────────
class CancelSubscriptionView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        reason = request.data.get("reason", "")
        try:
            sub = cancel_subscription(user=request.user, reason=reason)
        except SubscriptionError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            {
                "message": "Subscription cancelled. Access continues until period end.",
                "subscription": SubscriptionSerializer(sub).data,
            }
        )


# ─────────────────────────────────────────────────────────────────
#  7. Payment history
# ─────────────────────────────────────────────────────────────────
class PaymentHistoryView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        logs = PaymentLog.objects.filter(user=request.user).order_by("-created_at")[:50]
        return Response(PaymentLogSerializer(logs, many=True).data)


# ─────────────────────────────────────────────────────────────────
#  8. Subscription status
# ─────────────────────────────────────────────────────────────────
class SubscriptionStatusView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        plan = user.plan or "free"
        plan_config = settings.SUBSCRIPTION_PLANS.get(plan, {})
        features = plan_config.get("features", {})

        return Response(
            {
                "plan": plan,
                "plan_expires": user.plan_expires,
                "is_active": True,
                "features": features,
            }
        )


# ─────────────────────────────────────────────────────────────────
#  9. Razorpay Webhook
# ─────────────────────────────────────────────────────────────────
@method_decorator(csrf_exempt, name="dispatch")
class RazorpayWebhookView(View):

    def post(self, request, *args, **kwargs):
        webhook_secret = getattr(settings, "RAZORPAY_WEBHOOK_SECRET", "")
        razorpay_signature = request.headers.get("X-Razorpay-Signature", "")
        body = request.body

        if webhook_secret and razorpay_signature:
            expected = hmac.new(
                webhook_secret.encode("utf-8"), body, hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(expected, razorpay_signature):
                return JsonResponse({"error": "Invalid signature"}, status=400)

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)

        event = payload.get("event", "")
        logger.info("Razorpay webhook | event=%s", event)

        return JsonResponse({"status": "ok"}, status=200)
