# apps/users/tasks.py

import logging

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def send_otp_email(self, email: str, code: str, purpose: str):
    """OTP email bhejo — failure pe 3 baar retry karega."""
    subjects = {
        "verify": "Verify your email — ProfitX",
        "reset": "Password Reset OTP — ProfitX",
        "login": "Login OTP — ProfitX",
    }
    messages = {
        "verify": (
            f"Welcome to ProfitX!\n\n"
            f"Your verification OTP: {code}\n"
            f"Expires in 15 minutes.\n\n"
            f"If you did not register, ignore this email."
        ),
        "reset": (
            f"Your password reset OTP: {code}\n"
            f"Expires in 15 minutes.\n\n"
            f"If you did not request this, ignore this email."
        ),
        "login": (f"Your login OTP: {code}\n" f"Expires in 15 minutes."),
    }
    try:
        sent = send_mail(
            subject=subjects.get(purpose, "OTP — ProfitX"),
            message=messages.get(purpose, f"Your OTP: {code}"),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[email],
            fail_silently=False,
        )
        logger.info(
            "OTP email sent | email=%s | purpose=%s | sent=%s", email, purpose, sent
        )
        return {"sent": sent, "email": email}
    except Exception as exc:
        logger.error("OTP email FAILED | email=%s | error=%s", email, exc)
        raise self.retry(exc=exc)


@shared_task
def clean_expired_otps():
    """
    Expire ho chuke OTPs delete karo — hourly Celery beat se call hota hai.
    settings.py ke CELERY_BEAT_SCHEDULE mein registered hai.
    """
    from .models import OTPCode

    deleted, _ = OTPCode.objects.filter(
        expires_at__lt=timezone.now(),
        is_used=False,
    ).delete()
    logger.info("Cleaned %d expired OTPs", deleted)
    return {"deleted": deleted}


@shared_task(bind=True, max_retries=2)
def send_welcome_email(self, email: str, full_name: str):
    """Registration ke baad welcome email."""
    try:
        send_mail(
            subject="Welcome to ProfitX!",
            message=(
                f'Hi {full_name or "Trader"},\n\n'
                f"Welcome to ProfitX — your algo trading platform.\n\n"
                f"Get started:\n"
                f"  1. Verify your email\n"
                f"  2. Connect your broker\n"
                f"  3. Choose a strategy\n\n"
                f"Happy trading!\n— Team ProfitX"
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[email],
            fail_silently=False,
        )
        logger.info("Welcome email sent | email=%s", email)
    except Exception as exc:
        raise self.retry(exc=exc, countdown=60)
