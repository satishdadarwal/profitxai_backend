# apps/notifications/tasks.py
#
#  Celery tasks for all notification channels.
#  Queue: "default"  (celery-strategies worker handles this)
#
#  Entry point for the whole codebase:
#
#    from apps.notifications.tasks import send_notification_task
#
#    send_notification_task.delay(
#        user_id  = 42,
#        channel  = "both",      # "email" | "ws" | "push" | "both" | "all"
#        title    = "Trade Filled",
#        body     = "Your BUY 0.1 BTC @ 65000 has been executed.",
#        level    = "success",   # info | success | warning | error
#        category = "trade",
#        metadata = {"order_id": "abc123"},
#    )

import logging

from django.contrib.auth import get_user_model
from django.utils import timezone

from celery import shared_task

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
#  Master dispatcher
# ─────────────────────────────────────────────────────────────────
@shared_task(
    bind=True,
    queue="default",
    max_retries=3,
    default_retry_delay=60,
    name="notifications.send",
)
def send_notification_task(
    self,
    *,
    user_id: int,
    channel: str = "both",  # email | ws | push | both | all
    title: str,
    body: str,
    level: str = "info",
    category: str = "general",
    metadata: dict = None,
):
    """
    Channels:
      "email" — sirf email
      "ws"    — sirf WebSocket
      "push"  — sirf FCM/APNs push
      "both"  — email + ws
      "all"   — email + ws + push
    """
    metadata = metadata or {}

    User = get_user_model()
    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        logger.error("send_notification_task: user %s not found", user_id)
        return

    prefs = _get_prefs(user)

    # ── Save to DB (always — builds notification centre) ─────────
    notif = _save_notification(
        user=user,
        title=title,
        body=body,
        level=level,
        category=category,
        metadata=metadata,
    )
    logger.debug("Notification saved | id=%s | user=%s", notif.id, user_id)

    if not prefs.is_category_enabled(category):
        logger.info("Notifications disabled for category=%s user=%s", category, user_id)
        return

    # ── Dispatch per channel ─────────────────────────────────────
    if channel in ("ws", "both", "all") and prefs.ws_enabled:
        send_ws_notification.delay(
            user_id=user_id,
            notif_id=notif.id,   # frontend read/unread mark ke liye
            title=title,
            body=body,
            level=level,
            category=category,
        )

    if channel in ("email", "both", "all") and prefs.email_enabled:
        send_email_notification.delay(
            user_id=user_id,
            title=title,
            body=body,
            category=category,
        )

    if channel in ("push", "all") and prefs.push_enabled:
        send_push_notification.delay(
            user_id=user_id,
            title=title,
            body=body,
            category=category,
            metadata=metadata,
        )


# ─────────────────────────────────────────────────────────────────
#  WebSocket task
# ─────────────────────────────────────────────────────────────────
@shared_task(
    queue="default",
    max_retries=3,
    default_retry_delay=10,
    name="notifications.ws",
)
def send_ws_notification(
    *,
    user_id: int,
    notif_id: int,
    title: str,
    body: str,
    level: str = "info",
    category: str = "general",
):
    """
    Django Channels ke through user ke connected WebSocket client ko push karo.
    consumers.py ka notification() handler yeh receive karega.
    notif_id frontend ko diya jata hai taaki read/unread toggle ho sake.
    """
    from apps.websocket.push import push_notification

    push_notification(
        user_id=user_id,
        notif_id=notif_id,
        level=level,
        title=title,
        body=body,
    )
    logger.debug(
        "WS notification sent | notif_id=%s | user=%s | title=%s",
        notif_id,
        user_id,
        title,
    )


# ─────────────────────────────────────────────────────────────────
#  Email task
# ─────────────────────────────────────────────────────────────────
@shared_task(
    queue="default",
    max_retries=3,
    default_retry_delay=60,
    name="notifications.email",
)
def send_email_notification(
    *,
    user_id: int,
    title: str,
    body: str,
    category: str = "general",
):
    """
    Django's built-in email backend use karta hai.
    Settings mein EMAIL_* configure karo (SMTP / SES / SendGrid).
    """
    from django.conf import settings
    from django.core.mail import send_mail

    User = get_user_model()
    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        return

    if not user.email:
        logger.warning("No email address for user %s", user_id)
        return

    try:
        html_body = _render_email_html(
            title=title, body=body, category=category, user=user
        )
        send_mail(
            subject=title,
            message=body,  # plain-text fallback
            html_message=html_body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
            fail_silently=False,
        )
        logger.info("Email sent | user=%s | subject=%s", user_id, title)

    except Exception as exc:
        logger.exception("Email send failed | user=%s | %s", user_id, exc)
        raise


# ─────────────────────────────────────────────────────────────────
#  FCM Push Notification task
# ─────────────────────────────────────────────────────────────────
@shared_task(
    queue="default",
    max_retries=3,
    default_retry_delay=30,
    name="notifications.push",
)
def send_push_notification(
    *,
    user_id: int,
    title: str,
    body: str,
    category: str = "general",
    metadata: dict = None,
):
    """
    Firebase Cloud Messaging (FCM) ke through mobile push bhejo.

    Requirements:
      pip install firebase-admin
      settings.py mein:
        FIREBASE_CREDENTIALS_PATH = "/path/to/serviceAccountKey.json"

    User ka FCM token FCMDevice model mein store hona chahiye.
    """
    metadata = metadata or {}

    try:
        from django.conf import settings

        import firebase_admin
        from firebase_admin import credentials, messaging
    except ImportError:
        logger.warning("firebase-admin not installed. Skipping push notification.")
        return

    # ── Init Firebase (idempotent) ───────────────────────────────
    if not firebase_admin._apps:
        cred = credentials.Certificate(settings.FIREBASE_CREDENTIALS_PATH)
        firebase_admin.initialize_app(cred)

    # ── Get device tokens ────────────────────────────────────────
    try:
        from apps.notifications.models import FCMDevice

        tokens = list(
            FCMDevice.objects.filter(user_id=user_id, is_active=True).values_list(
                "token", flat=True
            )
        )
    except Exception:
        logger.warning("FCMDevice model not found — skipping push")
        return

    if not tokens:
        logger.debug("No FCM tokens for user %s", user_id)
        return

    # ── Send ─────────────────────────────────────────────────────
    message = messaging.MulticastMessage(
        notification=messaging.Notification(title=title, body=body),
        data={k: str(v) for k, v in {**metadata, "category": category}.items()},
        tokens=tokens,
    )
    try:
        response = messaging.send_multicast(message)
        logger.info(
            "FCM push sent | user=%s | success=%d fail=%d",
            user_id,
            response.success_count,
            response.failure_count,
        )

        # ── Deactivate invalid tokens ────────────────────────────
        if response.failure_count:
            _cleanup_invalid_tokens(tokens, response.responses)

    except Exception as exc:
        logger.exception("FCM push failed | user=%s | %s", user_id, exc)
        raise


# ─────────────────────────────────────────────────────────────────
#  Bulk notification task  (e.g., platform announcements)
# ─────────────────────────────────────────────────────────────────
@shared_task(
    queue="default",
    name="notifications.broadcast",
)
def broadcast_notification_task(
    *,
    user_ids: list,
    channel: str,
    title: str,
    body: str,
    level: str = "info",
    category: str = "general",
):
    """
    Multiple users ko ek saath notify karo.
    Admin panel ya management command se call karo.
    """
    for uid in user_ids:
        send_notification_task.delay(
            user_id=uid,
            channel=channel,
            title=title,
            body=body,
            level=level,
            category=category,
        )
    logger.info("Broadcast queued | users=%d | title=%s", len(user_ids), title)


# ─────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────
def _get_prefs(user):
    from apps.notifications.models import NotificationPreference

    prefs, _ = NotificationPreference.objects.get_or_create(user=user)
    return prefs


def _save_notification(*, user, title, body, level, category, metadata):
    from apps.notifications.models import Notification

    return Notification.objects.create(
        user=user,
        title=title,
        body=body,
        level=level,
        category=category,
        metadata=metadata,
    )


def _render_email_html(*, title: str, body: str, category: str, user) -> str:
    """
    Simple inline HTML email template.
    Django template engine se render karna ho toh:
      render_to_string("notifications/email.html", ctx)
    """
    return f"""
    <html>
    <body style="font-family: Arial, sans-serif; max-width: 600px; margin: auto; padding: 24px;">
      <h2 style="color: #1a1a2e;">{title}</h2>
      <p style="color: #333; line-height: 1.6;">{body}</p>
      <hr style="border: none; border-top: 1px solid #eee; margin: 24px 0;">
      <p style="color: #888; font-size: 12px;">
        You received this because you have <strong>{category}</strong> notifications enabled.<br>
        <a href="#">Manage preferences</a>
      </p>
    </body>
    </html>
    """


def _cleanup_invalid_tokens(tokens: list, responses: list):
    """FCM invalid token response aane par DB se delete karo."""
    try:
        from firebase_admin.messaging import SenderIdMismatchError, UnregisteredError

        from apps.notifications.models import FCMDevice

        invalid_tokens = [
            token
            for token, resp in zip(tokens, responses)
            if not resp.success
            and isinstance(resp.exception, (UnregisteredError, SenderIdMismatchError))
        ]
        if invalid_tokens:
            FCMDevice.objects.filter(token__in=invalid_tokens).update(is_active=False)
            logger.info("Deactivated %d invalid FCM tokens", len(invalid_tokens))
    except Exception as exc:
        logger.warning("Token cleanup failed: %s", exc)

@shared_task(bind=True, queue="default", max_retries=5, default_retry_delay=10, name="notifications.send_urgent")
def send_urgent_notification(self, user_id: int, message: str):
    """Emergency notification for kill switch and critical errors."""
    try:
        send_notification_task.apply_async(
            kwargs={
                "user_id": user_id, "channel": "all",
                "title": "URGENT: Trading Alert", "body": message,
                "level": "error", "category": "system",
                "metadata": {"urgent": True},
            },
            priority=10,
        )
        logger.critical("URGENT notification sent | user=%s | msg=%s", user_id, message)
    except Exception as exc:
        raise self.retry(exc=exc)