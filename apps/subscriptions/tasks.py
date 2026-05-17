# apps/subscriptions/tasks.py
#
#  Celery Beat periodic tasks — subscription lifecycle management.
#
#  celery-beat container (docker-compose) mein scheduled hain.
#  settings.py mein add karo:
#
#    from celery.schedules import crontab
#    CELERY_BEAT_SCHEDULE = {
#        "subscription-expiry-check": {
#            "task":     "subscriptions.check_expiring_subscriptions",
#            "schedule": crontab(hour=9, minute=0),   # daily 9 AM
#        },
#        "subscription-renewal-reminders": {
#            "task":     "subscriptions.send_renewal_reminders",
#            "schedule": crontab(hour=10, minute=0),
#        },
#    }

import logging
from datetime import timedelta

from django.utils import timezone

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name="subscriptions.check_expiring_subscriptions", queue="default")
def check_expiring_subscriptions():
    """
    Daily run karo — expired trials aur subscriptions detect karke DB update karo.
    Middleware bhi yeh karta hai per-request, yeh task un users ke liye hai
    jo din bhar login nahi karte.
    """
    from .models import Subscription

    now = timezone.now()
    updated = 0

    # ── Expired trials ───────────────────────────────────────────
    expired_trials = Subscription.objects.filter(
        status=Subscription.Status.TRIALING,
        trial_end__lt=now,
    )
    for sub in expired_trials:
        sub.status = Subscription.Status.EXPIRED
        sub.save(update_fields=["status", "updated_at"])
        updated += 1
        _notify_expired(sub)

    # ── Expired active subscriptions ─────────────────────────────
    expired_active = Subscription.objects.filter(
        status=Subscription.Status.ACTIVE,
        current_period_end__lt=now,
    )
    for sub in expired_active:
        sub.status = Subscription.Status.EXPIRED
        sub.save(update_fields=["status", "updated_at"])
        updated += 1
        _notify_expired(sub)

    # ── Grace period over ────────────────────────────────────────
    expired_grace = Subscription.objects.filter(
        status=Subscription.Status.PAST_DUE,
        grace_until__lt=now,
    )
    for sub in expired_grace:
        sub.status = Subscription.Status.EXPIRED
        sub.grace_until = None
        sub.save(update_fields=["status", "grace_until", "updated_at"])
        updated += 1
        _notify_expired(sub)

    logger.info("Expiry check complete | updated=%d", updated)
    return {"updated": updated}


@shared_task(name="subscriptions.send_renewal_reminders", queue="default")
def send_renewal_reminders():
    """
    Expiring subscriptions ke users ko reminder bhejo.
    Sends reminders at: 7 days, 3 days, 1 day before expiry.
    """
    from apps.notifications.tasks import send_notification_task

    from .models import Subscription

    now = timezone.now()
    reminders = 0

    for days_before in [7, 3, 1]:
        window_start = now + timedelta(days=days_before) - timedelta(hours=1)
        window_end = now + timedelta(days=days_before) + timedelta(hours=1)

        expiring = Subscription.objects.filter(
            status=Subscription.Status.ACTIVE,
            current_period_end__range=(window_start, window_end),
        ).select_related("plan", "user")

        for sub in expiring:
            send_notification_task.delay(
                user_id=sub.user_id,
                channel="both",
                title=f"Subscription Renewing in {days_before} Day{'s' if days_before > 1 else ''}",
                body=(
                    f"Your {sub.plan.name} plan renews on "
                    f"{sub.current_period_end.strftime('%d %b %Y')}. "
                    "Ensure your payment method is up to date."
                ),
                level="info",
                category="subscription_reminder",
            )
            reminders += 1

    # ── Trial ending soon ────────────────────────────────────────
    trial_expiring = Subscription.objects.filter(
        status=Subscription.Status.TRIALING,
        trial_end__range=(
            now + timedelta(days=2),
            now + timedelta(days=3),
        ),
    ).select_related("plan", "user")

    for sub in trial_expiring:
        send_notification_task.delay(
            user_id=sub.user_id,
            channel="both",
            title="Free Trial Ending Soon",
            body=(
                f"Your free trial ends on {sub.trial_end.strftime('%d %b %Y')}. "
                "Subscribe now to continue using all features."
            ),
            level="warning",
            category="trial_reminder",
            metadata={"upgrade_url": "/api/subscriptions/plans/"},
        )
        reminders += 1

    logger.info("Renewal reminders sent | count=%d", reminders)
    return {"reminders_sent": reminders}


# ── Private helpers ──────────────────────────────────────────────
def _notify_expired(sub):
    from apps.notifications.tasks import send_notification_task

    try:
        send_notification_task.delay(
            user_id=sub.user_id,
            channel="both",
            title="Subscription Expired",
            body=(
                f"Your {sub.plan.name} plan has expired. "
                "Please renew to restore access."
            ),
            level="warning",
            category="subscription",
            metadata={"upgrade_url": "/api/subscriptions/plans/"},
        )
    except Exception as exc:
        logger.warning(
            "Could not send expiry notification for user %s: %s", sub.user_id, exc
        )
