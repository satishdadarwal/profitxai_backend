# apps/subscriptions/middleware.py
#
#  Two middlewares:
#
#  1. SubscriptionMiddleware
#     ─ request.subscription attach karta hai (ya None)
#     ─ Har DB query avoid karne ke liye select_related("plan") cache karta hai
#     ─ Expired trial/subscription ka status auto-update karta hai
#
#  2. SubscriptionAPIMiddleware
#     ─ DRF API endpoints pe subscription check enforce karta hai
#     ─ Blocked endpoints configure karo settings.SUBSCRIPTION_REQUIRED_PATHS
#
#  settings.py mein add karo:
#
#    MIDDLEWARE = [
#        ...
#        "apps.subscriptions.middleware.SubscriptionMiddleware",
#        # Optional — API-level hard block:
#        # "apps.subscriptions.middleware.SubscriptionAPIMiddleware",
#    ]

import json
import logging

from django.conf import settings
from django.http import JsonResponse
from django.utils import timezone
from django.utils.deprecation import MiddlewareMixin

from .models import Subscription

logger = logging.getLogger(__name__)

# Paths that are always accessible regardless of subscription status
_ALWAYS_ALLOWED = frozenset(
    [
        "/api/auth/",
        "/api/subscriptions/",
        "/api/health/",
        "/admin/",
        "/static/",
        "/media/",
    ]
)


class SubscriptionMiddleware(MiddlewareMixin):
    """
    Authenticated user ke liye request.subscription attach karta hai.

    Usage in views / DRF:
        sub = request.subscription          # Subscription | None
        if sub and sub.is_pro_or_above:
            ...

    Also auto-expires stale trials / subscriptions on each request
    (low overhead: only writes when status actually changes).
    """

    def process_request(self, request):
        request.subscription = None

        if not hasattr(request, "user") or not request.user.is_authenticated:
            return None

        try:
            sub = Subscription.objects.select_related("plan").get(user=request.user)
            # ── Auto-expire stale statuses ───────────────────────
            self._maybe_expire(sub)
            request.subscription = sub

        except Subscription.DoesNotExist:
            # New user — free subscription banao
            from .services import get_or_create_free_subscription

            try:
                sub = get_or_create_free_subscription(request.user)
                request.subscription = sub
            except Exception as exc:
                logger.error(
                    "Could not create free subscription for user %s: %s",
                    request.user.id,
                    exc,
                )

        return None

    # ── Private ──────────────────────────────────────────────────
    def _maybe_expire(self, sub: Subscription):
        """
        Agar subscription/trial ki expiry aa gayi hai aur status update
        nahi hua toh silently DB update karo.
        """
        now = timezone.now()
        changed = False

        if sub.status == Subscription.Status.TRIALING:
            if sub.trial_end and now > sub.trial_end:
                sub.status = Subscription.Status.EXPIRED
                changed = True
                logger.info("Trial expired | user=%s", sub.user_id)

        elif sub.status == Subscription.Status.ACTIVE:
            if sub.current_period_end and now > sub.current_period_end:
                sub.status = Subscription.Status.EXPIRED
                changed = True
                logger.info("Subscription period ended | user=%s", sub.user_id)

        elif sub.status == Subscription.Status.PAST_DUE:
            if sub.grace_until and now > sub.grace_until:
                sub.status = Subscription.Status.EXPIRED
                sub.grace_until = None
                changed = True
                logger.info("Grace period ended | user=%s", sub.user_id)

        if changed:
            sub.save(update_fields=["status", "grace_until", "updated_at"])
            self._notify_expiry(sub)

    def _notify_expiry(self, sub: Subscription):
        from apps.notifications.tasks import send_notification_task

        try:
            send_notification_task.delay(
                user_id=sub.user_id,
                channel="both",
                title="Subscription Expired",
                body=(
                    f"Your {sub.plan.name} subscription has expired. "
                    "Please renew to restore full access."
                ),
                level="warning",
                category="subscription",
            )
        except Exception as exc:
            logger.warning("Could not queue expiry notification: %s", exc)


# ─────────────────────────────────────────────────────────────────
#  API-level hard enforcement (optional — use with care)
# ─────────────────────────────────────────────────────────────────
class SubscriptionAPIMiddleware(MiddlewareMixin):
    """
    API endpoints pe subscription check enforce karta hai.

    Customize karo settings.py mein:

        SUBSCRIPTION_REQUIRED_PATHS = [
            "/api/orders/",
            "/api/strategies/",
            "/api/brokers/",
        ]
        SUBSCRIPTION_EXCLUDE_PATHS = [
            "/api/subscriptions/",
            "/api/auth/",
        ]

    Expired / cancelled subscription wale users ko 403 milega
    un paths pe jahan subscription required hai.
    """

    def process_request(self, request):
        if not self._should_check(request):
            return None

        if not request.user.is_authenticated:
            return None

        sub = getattr(request, "subscription", None)

        # SubscriptionMiddleware ne already attach kiya hoga
        if sub is None:
            return self._deny(
                "No active subscription found. Please subscribe to continue."
            )

        if not sub.is_access_granted:
            return self._deny(
                f"Your subscription ({sub.plan.name}) is {sub.status}. "
                "Please renew to continue."
            )

        return None

    def _should_check(self, request) -> bool:
        path = request.path_info

        # Always exclude these paths
        exclude = getattr(settings, "SUBSCRIPTION_EXCLUDE_PATHS", [])
        exclude_set = list(_ALWAYS_ALLOWED) + list(exclude)
        if any(path.startswith(p) for p in exclude_set):
            return False

        # Only check configured required paths
        required_paths = getattr(settings, "SUBSCRIPTION_REQUIRED_PATHS", [])
        if not required_paths:
            return False  # Nothing configured → don't block anything

        return any(path.startswith(p) for p in required_paths)

    def _deny(self, message: str) -> JsonResponse:
        return JsonResponse(
            {
                "error": "subscription_required",
                "message": message,
                "upgrade_url": "/api/subscriptions/plans/",
            },
            status=403,
        )
