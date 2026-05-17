# apps/subscriptions/services.py
#
#  Pure business logic — no HTTP request objects here.
#  Views, webhooks, and Celery tasks all call into this layer.
#
#  Required settings:
#    RAZORPAY_KEY_ID       = "rzp_live_XXXXXXXXXX"
#    RAZORPAY_KEY_SECRET   = "your_secret"
#    RAZORPAY_WEBHOOK_SECRET = "your_webhook_secret"
#    SUBSCRIPTION_TRIAL_DAYS = 14          (optional, default 14)
#    SUBSCRIPTION_GRACE_DAYS = 3           (optional, default 3)

import hashlib
import hmac
import logging
from datetime import timedelta
from decimal import Decimal
from typing import Optional

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction as db_transaction
from django.utils import timezone

import razorpay

from apps.notifications.tasks import send_notification_task

from .models import PaymentLog, Plan, RazorpayWebhookEvent, Subscription

User = get_user_model()
logger = logging.getLogger(__name__)

TRIAL_DAYS = getattr(settings, "SUBSCRIPTION_TRIAL_DAYS", 14)
GRACE_DAYS = getattr(settings, "SUBSCRIPTION_GRACE_DAYS", 3)


# ─────────────────────────────────────────────────────────────────
#  Exceptions
# ─────────────────────────────────────────────────────────────────
class SubscriptionError(Exception):
    pass


class PaymentVerificationError(SubscriptionError):
    pass


class DuplicateWebhookError(SubscriptionError):
    pass


class PlanNotFoundError(SubscriptionError):
    pass


# ─────────────────────────────────────────────────────────────────
#  Razorpay client (lazy singleton)
# ─────────────────────────────────────────────────────────────────
_rzp_client: Optional[razorpay.Client] = None


def get_razorpay_client() -> razorpay.Client:
    global _rzp_client
    if _rzp_client is None:
        _rzp_client = razorpay.Client(
            auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET)
        )
        _rzp_client.set_app_details({"title": "TradingApp", "version": "1.0"})
    return _rzp_client


# ─────────────────────────────────────────────────────────────────
#  1. Create / ensure free subscription on signup
# ─────────────────────────────────────────────────────────────────
def get_or_create_free_subscription(user) -> Subscription:
    """
    User registration ke time call karo (signal ya view).
    Free plan pe 14-day trial shuru hota hai.
    """
    if hasattr(user, "subscription"):
        return user.subscription

    free_plan = Plan.objects.filter(tier=Plan.Tier.FREE, is_active=True).first()
    if not free_plan:
        raise PlanNotFoundError(
            "No active FREE plan found. Run management command to seed plans."
        )

    trial_end = timezone.now() + timedelta(days=TRIAL_DAYS)
    sub = Subscription.objects.create(
        user=user,
        plan=free_plan,
        status=Subscription.Status.TRIALING,
        trial_end=trial_end,
    )
    logger.info(
        "Free subscription created | user=%s | trial_end=%s", user.id, trial_end
    )
    return sub


# ─────────────────────────────────────────────────────────────────
#  2. Create Razorpay Order  (one-time / first payment)
# ─────────────────────────────────────────────────────────────────
def create_razorpay_order(*, user, plan_id: str) -> dict:
    """
    Razorpay order create karo aur frontend ko return karo.
    Frontend is order_id se Razorpay checkout open karega.

    Returns dict with keys:
        order_id, amount, currency, key_id, plan_name, user_email, user_name
    """
    plan = _get_plan(plan_id)
    rzp = get_razorpay_client()

    amount_paise = int(plan.price_inr * 100)  # Razorpay paise mein chahiye

    order_data = {
        "amount": amount_paise,
        "currency": "INR",
        "receipt": f"sub_{user.id}_{plan.id}",
        "notes": {
            "user_id": str(user.id),
            "plan_id": str(plan.id),
        },
    }

    try:
        rzp_order = rzp.order.create(data=order_data)
    except razorpay.errors.BadRequestError as exc:
        logger.error("Razorpay order creation failed | user=%s | %s", user.id, exc)
        raise SubscriptionError(f"Payment gateway error: {exc}") from exc

    # Create pending PaymentLog
    sub = _get_or_create_subscription(user, plan)
    PaymentLog.objects.create(
        subscription=sub,
        user=user,
        razorpay_order_id=rzp_order["id"],
        amount_inr=plan.price_inr,
        payment_status=PaymentLog.PaymentStatus.CREATED,
        raw_payload=rzp_order,
    )

    logger.info(
        "Razorpay order created | user=%s | plan=%s | order=%s",
        user.id,
        plan.name,
        rzp_order["id"],
    )

    return {
        "order_id": rzp_order["id"],
        "amount": amount_paise,
        "currency": "INR",
        "key_id": settings.RAZORPAY_KEY_ID,
        "plan_name": plan.name,
        "user_email": user.email,
        "user_name": getattr(user, "get_full_name", lambda: user.username)(),
    }


# ─────────────────────────────────────────────────────────────────
#  3. Verify payment signature + activate subscription
# ─────────────────────────────────────────────────────────────────
def verify_and_activate(
    *,
    user,
    razorpay_order_id: str,
    razorpay_payment_id: str,
    razorpay_signature: str,
) -> Subscription:
    """
    Frontend se payment_id + signature aane ke baad call karo.
    HMAC verify → PaymentLog update → Subscription activate.

    Raises PaymentVerificationError on invalid signature.
    """
    # ── Signature verification ───────────────────────────────────
    _verify_payment_signature(
        order_id=razorpay_order_id,
        payment_id=razorpay_payment_id,
        signature=razorpay_signature,
    )

    with db_transaction.atomic():
        # ── Update PaymentLog ────────────────────────────────────
        log = (
            PaymentLog.objects.select_for_update()
            .filter(
                razorpay_order_id=razorpay_order_id,
                user=user,
            )
            .first()
        )

        if not log:
            raise SubscriptionError("PaymentLog not found for this order.")

        log.razorpay_payment_id = razorpay_payment_id
        log.razorpay_signature = razorpay_signature
        log.payment_status = PaymentLog.PaymentStatus.CAPTURED
        log.save(
            update_fields=[
                "razorpay_payment_id",
                "razorpay_signature",
                "payment_status",
                "updated_at",
            ]
        )

        # ── Activate Subscription ────────────────────────────────
        sub = log.subscription
        _activate_subscription(sub)

    logger.info(
        "Subscription activated | user=%s | sub=%s | payment=%s",
        user.id,
        sub.id,
        razorpay_payment_id,
    )

    # ── Notify user ──────────────────────────────────────────────
    send_notification_task.delay(
        user_id=user.id,
        channel="both",
        title="Subscription Activated 🎉",
        body=f"Welcome to {sub.plan.name}! Your plan is now active.",
        level="success",
        category="subscription",
    )

    return sub


# ─────────────────────────────────────────────────────────────────
#  4. Webhook event processor
# ─────────────────────────────────────────────────────────────────
def process_webhook_event(*, event_id: str, event_type: str, payload: dict) -> str:
    """
    Razorpay webhook payload process karo.
    Returns: "processed" | "duplicate" | "ignored"

    Idempotency: duplicate event_id silently ignore hoti hai.
    """
    # ── Idempotency check ────────────────────────────────────────
    _, created = RazorpayWebhookEvent.objects.get_or_create(
        event_id=event_id,
        defaults={"event_type": event_type, "payload": payload},
    )
    if not created:
        logger.info("Duplicate webhook ignored | event_id=%s", event_id)
        return "duplicate"

    logger.info("Processing webhook | event=%s | id=%s", event_type, event_id)

    handler = _WEBHOOK_HANDLERS.get(event_type)
    if handler:
        try:
            handler(payload)
            return "processed"
        except Exception as exc:
            logger.exception("Webhook handler failed | event=%s | %s", event_type, exc)
            raise

    logger.debug("No handler for webhook event: %s", event_type)
    return "ignored"


# ─────────────────────────────────────────────────────────────────
#  5. Plan upgrade / downgrade
# ─────────────────────────────────────────────────────────────────
def change_plan(*, user, new_plan_id: str) -> dict:
    """
    Immediately create a new Razorpay order for the new plan.
    Actual plan change happens in verify_and_activate().
    Returns same dict as create_razorpay_order().
    """
    new_plan = _get_plan(new_plan_id)
    current = getattr(user, "subscription", None)

    if current and current.plan_id == new_plan.id:
        raise SubscriptionError("User is already on this plan.")

    return create_razorpay_order(user=user, plan_id=str(new_plan_id))


# ─────────────────────────────────────────────────────────────────
#  6. Cancel subscription
# ─────────────────────────────────────────────────────────────────
def cancel_subscription(*, user, reason: str = "") -> Subscription:
    """
    Current period end tak access milta rahega, phir expire hoga.
    Razorpay subscription bhi cancel kiya jata hai agar hai toh.
    """
    sub = _require_subscription(user)

    if sub.status == Subscription.Status.CANCELLED:
        raise SubscriptionError("Subscription is already cancelled.")

    # Cancel on Razorpay side
    if sub.razorpay_subscription_id:
        try:
            rzp = get_razorpay_client()
            rzp.subscription.cancel(
                sub.razorpay_subscription_id, {"cancel_at_cycle_end": 1}
            )
        except Exception as exc:
            logger.warning("Razorpay cancel API failed (continuing): %s", exc)

    sub.status = Subscription.Status.CANCELLED
    sub.cancelled_at = timezone.now()
    sub.save(update_fields=["status", "cancelled_at", "updated_at"])

    logger.info("Subscription cancelled | user=%s | reason=%s", user.id, reason)

    send_notification_task.delay(
        user_id=user.id,
        channel="both",
        title="Subscription Cancelled",
        body="Your subscription has been cancelled. Access continues until period end.",
        level="warning",
        category="subscription",
    )

    return sub


# ─────────────────────────────────────────────────────────────────
#  Signature verification
# ─────────────────────────────────────────────────────────────────
def verify_webhook_signature(raw_body: bytes, rzp_signature: str) -> bool:
    """
    Webhook endpoint pe raw body aur X-Razorpay-Signature header se call karo.
    """
    secret = settings.RAZORPAY_WEBHOOK_SECRET.encode("utf-8")
    digest = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, rzp_signature)


# ─────────────────────────────────────────────────────────────────
#  Private — webhook handlers map
# ─────────────────────────────────────────────────────────────────
def _handle_payment_captured(payload: dict):
    """payment.captured — payment successful."""
    payment = payload.get("payload", {}).get("payment", {}).get("entity", {})
    order_id = payment.get("order_id", "")
    payment_id = payment.get("id", "")

    log = (
        PaymentLog.objects.filter(razorpay_order_id=order_id)
        .select_related("subscription__user")
        .first()
    )

    if not log:
        logger.warning("payment.captured: no PaymentLog for order %s", order_id)
        return

    with db_transaction.atomic():
        log.razorpay_payment_id = payment_id
        log.payment_status = PaymentLog.PaymentStatus.CAPTURED
        log.raw_payload = payment
        log.save(
            update_fields=[
                "razorpay_payment_id",
                "payment_status",
                "raw_payload",
                "updated_at",
            ]
        )

        _activate_subscription(log.subscription)

    send_notification_task.delay(
        user_id=log.user_id,
        channel="both",
        title="Payment Successful",
        body=f"₹{log.amount_inr} payment confirmed. Your plan is active.",
        level="success",
        category="payment",
    )


def _handle_payment_failed(payload: dict):
    """payment.failed — move to past_due, start grace period."""
    payment = payload.get("payload", {}).get("payment", {}).get("entity", {})
    order_id = payment.get("order_id", "")
    reason = payment.get("error_description", "Unknown reason")

    log = (
        PaymentLog.objects.filter(razorpay_order_id=order_id)
        .select_related("subscription__user")
        .first()
    )

    if not log:
        logger.warning("payment.failed: no PaymentLog for order %s", order_id)
        return

    with db_transaction.atomic():
        log.payment_status = PaymentLog.PaymentStatus.FAILED
        log.failure_reason = reason
        log.raw_payload = payment
        log.save(
            update_fields=[
                "payment_status",
                "failure_reason",
                "raw_payload",
                "updated_at",
            ]
        )

        sub = log.subscription
        sub.status = Subscription.Status.PAST_DUE
        sub.grace_until = timezone.now() + timedelta(days=GRACE_DAYS)
        sub.save(update_fields=["status", "grace_until", "updated_at"])

    send_notification_task.delay(
        user_id=log.user_id,
        channel="both",
        title="Payment Failed",
        body=(
            f"Your payment could not be processed ({reason}). "
            f"You have {GRACE_DAYS} days grace period. Please update your payment method."
        ),
        level="error",
        category="payment",
    )


def _handle_subscription_activated(payload: dict):
    """subscription.activated — Razorpay recurring mandate confirmed."""
    entity = payload.get("payload", {}).get("subscription", {}).get("entity", {})
    rzp_sub_id = entity.get("id", "")

    sub = Subscription.objects.filter(razorpay_subscription_id=rzp_sub_id).first()

    if sub:
        _activate_subscription(sub)


def _handle_subscription_cancelled(payload: dict):
    """subscription.cancelled — Razorpay side se cancel hua."""
    entity = payload.get("payload", {}).get("subscription", {}).get("entity", {})
    rzp_sub_id = entity.get("id", "")

    Subscription.objects.filter(razorpay_subscription_id=rzp_sub_id).update(
        status=Subscription.Status.CANCELLED,
        cancelled_at=timezone.now(),
    )


def _handle_subscription_charged(payload: dict):
    """subscription.charged — recurring renewal payment."""
    _handle_payment_captured(payload)


_WEBHOOK_HANDLERS = {
    "payment.captured": _handle_payment_captured,
    "payment.failed": _handle_payment_failed,
    "subscription.activated": _handle_subscription_activated,
    "subscription.cancelled": _handle_subscription_cancelled,
    "subscription.charged": _handle_subscription_charged,
}


# ─────────────────────────────────────────────────────────────────
#  Private helpers
# ─────────────────────────────────────────────────────────────────
def _get_plan(plan_id: str) -> Plan:
    try:
        return Plan.objects.get(pk=plan_id, is_active=True)
    except Plan.DoesNotExist:
        raise PlanNotFoundError(f"Plan '{plan_id}' not found or inactive.")


def _require_subscription(user) -> Subscription:
    sub = getattr(user, "subscription", None)
    if not sub:
        raise SubscriptionError("User has no subscription.")
    return sub


def _get_or_create_subscription(user, plan: Plan) -> Subscription:
    """Existing subscription upgrade karo ya naya banao."""
    sub = getattr(user, "subscription", None)
    if sub:
        if sub.plan_id != plan.id:
            sub.plan = plan
            sub.save(update_fields=["plan", "updated_at"])
        return sub

    return Subscription.objects.create(
        user=user,
        plan=plan,
        status=Subscription.Status.TRIALING,
    )


def _activate_subscription(sub: Subscription):
    """Subscription status active karo aur billing period set karo."""
    now = timezone.now()
    plan = sub.plan

    # Billing period calculate karo
    if plan.billing_cycle == Plan.BillingCycle.YEARLY:
        period_end = now + timedelta(days=365)
    else:
        period_end = now + timedelta(days=30)

    sub.status = Subscription.Status.ACTIVE
    sub.current_period_start = now
    sub.current_period_end = period_end
    sub.grace_until = None
    sub.save(
        update_fields=[
            "status",
            "current_period_start",
            "current_period_end",
            "grace_until",
            "updated_at",
        ]
    )


def _verify_payment_signature(*, order_id: str, payment_id: str, signature: str):
    """
    Razorpay payment signature verify karo.
    Raises PaymentVerificationError on mismatch.
    """
    message = f"{order_id}|{payment_id}".encode("utf-8")
    secret = settings.RAZORPAY_KEY_SECRET.encode("utf-8")
    digest = hmac.new(secret, message, hashlib.sha256).hexdigest()

    if not hmac.compare_digest(digest, signature):
        logger.warning(
            "Payment signature mismatch | order=%s | payment=%s", order_id, payment_id
        )
        raise PaymentVerificationError(
            "Invalid payment signature. Possible tampering detected."
        )
