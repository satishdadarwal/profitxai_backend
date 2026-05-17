# apps/subscriptions/webhooks.py
#
#  Razorpay webhook endpoint.
#  URL: POST /api/subscriptions/webhook/razorpay/
#
#  Razorpay dashboard pe configure karo:
#    URL:    https://yourdomain.com/api/subscriptions/webhook/razorpay/
#    Events: payment.captured, payment.failed,
#            subscription.activated, subscription.cancelled, subscription.charged
#    Secret: settings.RAZORPAY_WEBHOOK_SECRET
#
#  Security:
#    ─ CSRF exempt (Razorpay raw POST bhejta hai)
#    ─ X-Razorpay-Signature HMAC-SHA256 verify hoti hai
#    ─ Idempotency: duplicate event_id silently ignore hoti hai
#    ─ Raw body Django ke request.body se padha jata hai (middleware se pehle)

import json
import logging

from django.http import HttpResponse, JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from .services import (
    DuplicateWebhookError,
    process_webhook_event,
    verify_webhook_signature,
)

logger = logging.getLogger(__name__)


@method_decorator(csrf_exempt, name="dispatch")
class RazorpayWebhookView(View):
    """
    Razorpay se aane wale webhook events handle karta hai.

    Flow:
        1. Signature verify
        2. JSON parse
        3. process_webhook_event() call
        4. 200 return (Razorpay 200 na mile toh retry karta hai)
    """

    def post(self, request):
        # ── 1. Read raw body (signature verification ke liye) ────
        raw_body = request.body
        rzp_sig = request.headers.get("X-Razorpay-Signature", "")

        if not rzp_sig:
            logger.warning("Webhook received without X-Razorpay-Signature header")
            return JsonResponse({"error": "Missing signature header"}, status=400)

        # ── 2. Verify HMAC signature ─────────────────────────────
        if not verify_webhook_signature(raw_body, rzp_sig):
            logger.warning(
                "Webhook signature verification FAILED | sig=%s", rzp_sig[:20]
            )
            return JsonResponse({"error": "Invalid signature"}, status=400)

        # ── 3. Parse JSON ────────────────────────────────────────
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.error("Webhook JSON parse error: %s", exc)
            return JsonResponse({"error": "Invalid JSON"}, status=400)

        event_id = payload.get("id", "")
        event_type = payload.get("event", "")

        if not event_id or not event_type:
            logger.warning("Webhook missing event id or type: %s", payload)
            return JsonResponse({"error": "Missing event id or type"}, status=400)

        logger.info("Webhook received | event=%s | id=%s", event_type, event_id)

        # ── 4. Process ───────────────────────────────────────────
        try:
            result = process_webhook_event(
                event_id=event_id,
                event_type=event_type,
                payload=payload,
            )
            logger.info(
                "Webhook result=%s | event=%s | id=%s", result, event_type, event_id
            )

        except DuplicateWebhookError:
            # Already processed — still return 200 so Razorpay stops retrying
            return JsonResponse({"status": "duplicate"}, status=200)

        except Exception as exc:
            # Return 500 → Razorpay will retry (which is what we want for transient errors)
            logger.exception(
                "Webhook processing error | event=%s | %s", event_type, exc
            )
            return JsonResponse({"error": "Internal processing error"}, status=500)

        return JsonResponse({"status": result}, status=200)

    def get(self, request):
        # Health check — useful for webhook URL verification
        return HttpResponse("Webhook endpoint active", content_type="text/plain")
