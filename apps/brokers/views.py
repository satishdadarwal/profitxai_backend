# apps/brokers/views.py
#
# ✅ FIX 1: _exchange_auth_code ke baad feed start nahi hota tha
#           Ab OAuth callback complete hone par turant feed start hota hai.
#
# ✅ FIX 2: FyersTokenRefreshView — manual refresh ke baad bhi
#           restart_with_new_token() call hota hai (naya token WS ko milta hai).
#
# ✅ FIX 3: _start_feed_after_token helper — background thread mein
#           HTTP response block nahi hoti.
#
# ✅ FIX 4: Master setup support — state="master_setup" handle karta hai
#
# ✅ FIX 5: pyotp import added for TOTP validation
#
# ✅ FIX 6: filter().first() instead of get() — MultipleObjectsReturned handle hota hai

import hashlib
import logging
import threading

from django.contrib.auth import get_user_model
from django.http import HttpResponse
from django.shortcuts import redirect as django_redirect
from django.conf import settings

import pyotp  # ✅ FIX: Import added
import requests
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import BrokerAccount
from .serializers import BrokerAccountSerializer

logger = logging.getLogger(__name__)

FYERS_API_BASE = "https://api-t1.fyers.in/api/v3"
FLUTTER_BASE   = settings.FLUTTER_DEEP_LINK       # .env → profitxai://fyers-callback
REDIRECT_URI   = settings.FYERS_REDIRECT_URI      # .env → http://27.59.119.101:8000/api/v1/brokers/fyers/callback/


# ─────────────────────────────────────────────────────────────────
# Helper — token save ke baad feed start / restart karo
# ─────────────────────────────────────────────────────────────────

def _start_feed_after_token(new_access_token: str, app_id: str, account_id: int):
    """
    Token save hone ke baad call karo (OAuth ya manual refresh dono ke baad).

    Logic:
    - Feed already running hai → restart_with_new_token (naya token + reconnect)
    - Feed nahi chal rahi   → fresh start

    Background thread mein karta hai — HTTP response block nahi hoti.
    """
    def _run():
        try:
            from apps.websocket.fyers_feed import feed_manager
            token_str = f"{app_id}:{new_access_token}"

            if feed_manager._started or feed_manager._connected:
                feed_manager.restart_with_new_token(token_str)
                logger.info(
                    "Feed restarted with new token | account=%s", account_id
                )
            else:
                feed_manager.start(token=token_str)
                logger.info(
                    "Feed started fresh after token save | account=%s", account_id
                )
        except Exception as e:
            logger.error(
                "_start_feed_after_token failed | account=%s | %s", account_id, e
            )

    threading.Thread(target=_run, daemon=True).start()


# ─────────────────────────────────────────────────────────────────
# Broker List / Create
# ─────────────────────────────────────────────────────────────────

class BrokerAccountListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        accounts = BrokerAccount.objects.filter(user=request.user)
        data = [
            {
                "id": a.id,
                "broker": a.broker,
                "label": a.label,
                "is_active": a.is_active,
                "is_verified": a.is_verified,
                "created_at": a.created_at,
            }
            for a in accounts
        ]
        return Response(data)


class BrokerAccountCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = BrokerAccountSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(user=request.user)
            return Response(serializer.data, status=201)
        return Response(serializer.errors, status=400)


# ─────────────────────────────────────────────────────────────────
# Fyers — Step 1: Auth URL generate karo
# ─────────────────────────────────────────────────────────────────

class FyersAuthURLView(APIView):
    """
    Flutter se sirf label aata hai — app_id/secret settings se aata hai.
    Fyers dashboard pe registered credentials use hote hain.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        app_id     = settings.FYERS_APP_ID
        secret_key = settings.FYERS_SECRET_KEY
        label      = request.data.get("label", "My Fyers")

        if not app_id or not secret_key:
            return Response(
                {"error": "FYERS_APP_ID / FYERS_SECRET_KEY .env mein set nahi hain"},
                status=500,
            )

        if not REDIRECT_URI:
            return Response(
                {"error": "FYERS_REDIRECT_URI .env mein set nahi hai"},
                status=500,
            )

        account, _ = BrokerAccount.objects.update_or_create(
            user=request.user,
            broker="fyers",
            label=label,
            defaults={
                "app_id": app_id,
                "secret_key": secret_key,
                "redirect_uri": REDIRECT_URI,
                "is_verified": False,
            },
        )

        auth_url = (
            f"https://api-t1.fyers.in/api/v3/generate-authcode"
            f"?client_id={app_id}"
            f"&redirect_uri={REDIRECT_URI}"
            f"&response_type=code"
            f"&state={request.user.id}"
        )

        logger.info(
            "FyersAuthURL generated | user=%s | app_id=%s | redirect=%s",
            request.user.id, app_id, REDIRECT_URI,
        )

        return Response(
            {
                "auth_url": auth_url,
                "label": label,
                "account_id": account.id,
            }
        )


# ─────────────────────────────────────────────────────────────────
# Fyers — Step 2: Callback (GET = browser redirect, POST = Flutter)
# ─────────────────────────────────────────────────────────────────

class FyersCallbackView(APIView):

    def get_permissions(self):
        if self.request.method == "GET":
            return [AllowAny()]
        return [IsAuthenticated()]

    def get(self, request):
        """
        Fyers server tumhare REDIRECT_URI pe redirect karta hai.
        URL: /api/v1/brokers/fyers/callback/?auth_code=xxx&state=user_id_or_master_setup

        ✅ FIX: Master setup support — state can be "master_setup" or UUID
        """
        auth_code = request.query_params.get("auth_code")
        state     = request.query_params.get("state")

        if not auth_code:
            logger.error("FyersCallback GET: no auth_code in params")
            return django_redirect(f"{FLUTTER_BASE}?error=no_auth_code")

        if not state:
            logger.error("FyersCallback GET: no state in params")
            return django_redirect(f"{FLUTTER_BASE}?error=invalid_state")

        # ✅ FIX: Handle both UUID and "master_setup" string
        User = get_user_model()

        # Check if state is "master_setup" (setup script se aaya hai)
        if state == "master_setup":
            # Master account setup — use first superuser or any user
            try:
                user = User.objects.filter(is_superuser=True).first()
                if not user:
                    # Agar superuser nahi hai toh first user use karo
                    user = User.objects.first()

                if not user:
                    logger.error("FyersCallback GET: No users in database for master_setup")
                    return HttpResponse(
                        "<html><body style='font-family: sans-serif; text-align: center; padding: 50px;'>"
                        "<h1>❌ Error: No User Found</h1>"
                        "<p>Please create a user first:</p>"
                        "<code>python manage.py createsuperuser</code>"
                        "</body></html>",
                        status=500
                    )

                logger.info("FyersCallback GET: master_setup mode, using user=%s", user.id)

            except Exception as e:
                logger.error("FyersCallback GET: error finding user for master_setup: %s", e)
                return HttpResponse(
                    f"<html><body style='font-family: sans-serif; text-align: center; padding: 50px;'>"
                    f"<h1>❌ Error</h1>"
                    f"<p>{str(e)}</p>"
                    f"</body></html>",
                    status=500
                )

        else:
            # Normal flow — state is user_id (UUID)
            try:
                user = User.objects.get(id=state)
            except User.DoesNotExist:
                logger.error("FyersCallback GET: user not found for state=%s", state)
                return django_redirect(f"{FLUTTER_BASE}?error=user_not_found")
            except ValueError:
                # state is not a valid UUID
                logger.error("FyersCallback GET: invalid state format: %s", state)
                return django_redirect(f"{FLUTTER_BASE}?error=invalid_state")

        request.user = user

        # Get or create account for this user
        # ✅ FIX: Use filter().first() instead of get() to handle multiple accounts
        account = (
            BrokerAccount.objects.filter(user=user, broker="fyers")
            .order_by("-created_at")
            .first()
        )

        if not account:
            # Master setup — create account
            if state == "master_setup":
                app_id = getattr(settings, "FYERS_APP_ID", "")
                secret_key = getattr(settings, "FYERS_SECRET_KEY", "")

                account = BrokerAccount.objects.create(
                    user=user,
                    broker="fyers",
                    label="Master Account",
                    app_id=app_id,
                    secret_key=secret_key,
                    redirect_uri=REDIRECT_URI,
                    is_verified=False,
                )
                logger.info("FyersCallback GET: Created master BrokerAccount for user=%s", user.id)
            else:
                logger.error("FyersCallback GET: BrokerAccount not found for user=%s", user.id)
                return django_redirect(f"{FLUTTER_BASE}?error=account_not_found")

        result = self._exchange_auth_code(request, auth_code, label=account.label)

        if result.status_code == 200:
            logger.info("FyersCallback GET: success | user=%s", user.id)

            # ✅ Master setup success — show simple HTML message
            if state == "master_setup":
                return HttpResponse(
                    "<html><body style='font-family: sans-serif; text-align: center; padding: 50px;'>"
                    "<h1 style='color: #4CAF50;'>✅ Fyers Master Account Connected!</h1>"
                    "<p style='font-size: 18px;'>Refresh token has been saved to database.</p>"
                    "<p style='color: #666;'>You can close this window and return to the terminal.</p>"
                    "<hr style='margin: 40px auto; width: 300px;'>"
                    "<p style='font-size: 14px; color: #999;'>Next steps:</p>"
                    "<ol style='text-align: left; display: inline-block; color: #666;'>"
                    "<li>Copy refresh_token from database to .env</li>"
                    "<li>Test: python manage.py shell</li>"
                    "<li>Start Celery Beat for daily auto-refresh</li>"
                    "</ol>"
                    "</body></html>"
                )

            # Normal Flutter flow
            return django_redirect(
                f"{FLUTTER_BASE}?status=success&label={account.label}"
            )
        else:
            error_msg = result.data.get("error", "unknown_error")
            logger.error("FyersCallback GET: failed | user=%s | error=%s", user.id, error_msg)

            if state == "master_setup":
                return HttpResponse(
                    f"<html><body style='font-family: sans-serif; text-align: center; padding: 50px;'>"
                    f"<h1 style='color: #f44336;'>❌ Error</h1>"
                    f"<p>{error_msg}</p>"
                    f"</body></html>",
                    status=400
                )

            return django_redirect(f"{FLUTTER_BASE}?error={error_msg}")

    def post(self, request):
        """Flutter WebView se auth_code directly POST karta hai."""
        auth_code = request.data.get("auth_code")
        label     = request.data.get("label", "My Fyers")

        if not auth_code:
            return Response({"error": "auth_code required"}, status=400)

        return self._exchange_auth_code(request, auth_code, label=label)

    def _exchange_auth_code(self, request, auth_code, label="My Fyers"):
        try:
            account = (
                BrokerAccount.objects.filter(user=request.user, broker="fyers")
                .order_by("-created_at")
                .first()
            )
            if not account:
                return Response(
                    {"error": "No Fyers account found. Call /fyers/auth-url/ first."},
                    status=400,
                )
        except Exception as e:
            return Response({"error": f"DB error: {str(e)}"}, status=500)

        app_id_hash = hashlib.sha256(
            f"{account.app_id}:{account.secret_key}".encode()
        ).hexdigest()

        logger.info(
            "_exchange_auth_code | user=%s | app_id=%s | hash_prefix=%s",
            request.user.id, account.app_id, app_id_hash[:8],
        )

        try:
            resp = requests.post(
                f"{FYERS_API_BASE}/validate-authcode",
                json={
                    "grant_type": "authorization_code",
                    "appIdHash": app_id_hash,
                    "code": auth_code,
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.Timeout:
            return Response({"error": "Fyers API timeout. Try again."}, status=504)
        except requests.exceptions.RequestException as e:
            return Response({"error": f"Fyers API unreachable: {str(e)}"}, status=502)
        except Exception as e:
            return Response({"error": f"Unexpected error: {str(e)}"}, status=500)

        if data.get("s") != "ok":
            logger.error("Fyers token exchange failed: %s", data)
            return Response(
                {
                    "error": "Token generation failed",
                    "fyers_code": data.get("code"),
                    "fyers_message": data.get("message", data),
                    "hint": (
                        "Common causes: auth_code expired (60s), "
                        "redirect_uri mismatch with Fyers dashboard, "
                        "wrong app_id/secret_key"
                    ),
                },
                status=400,
            )

        new_token = data.get("access_token")
        account.access_token  = new_token
        account.refresh_token = data.get("refresh_token", "")
        account.is_active     = True
        account.is_verified   = True
        account.save()

        logger.info("Fyers token saved | user=%s | account=%s", request.user.id, account.id)

        # ✅ FIX: OAuth complete hone ke baad feed start karo
        _start_feed_after_token(new_token, account.app_id, account.id)

        return Response(
            {
                "success": True,
                "message": "Fyers connected successfully!",
                "broker": "fyers",
                "label": account.label,
            }
        )


# ─────────────────────────────────────────────────────────────────
# Fyers — Token Refresh (manual, PIN required)
# ─────────────────────────────────────────────────────────────────

class FyersTokenRefreshView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        label = request.data.get("label", "My Fyers")
        pin   = request.data.get("pin", "")

        if not pin:
            return Response(
                {"error": "pin (4-digit Fyers PIN) required for token refresh"},
                status=400,
            )

        try:
            account = (
                BrokerAccount.objects.filter(
                    user=request.user, broker="fyers", is_active=True
                )
                .order_by("-created_at")
                .first()
            )
            if not account:
                return Response(
                    {"error": "Active Fyers account not found"},
                    status=404,
                )
        except Exception as e:
            return Response({"error": str(e)}, status=500)

        if not account.refresh_token:
            return Response(
                {"error": "No refresh token stored. Please login again via /auth-url/"},
                status=400,
            )

        app_id_hash = hashlib.sha256(
            f"{account.app_id}:{account.secret_key}".encode()
        ).hexdigest()

        try:
            resp = requests.post(
                f"{FYERS_API_BASE}/validate-refresh-token",
                json={
                    "grant_type": "refresh_token",
                    "appIdHash": app_id_hash,
                    "refresh_token": account.refresh_token,
                    "pin": str(pin),
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.Timeout:
            return Response({"error": "Fyers API timeout."}, status=504)
        except requests.exceptions.RequestException as e:
            return Response({"error": f"Fyers API error: {str(e)}"}, status=502)

        if data.get("s") != "ok":
            return Response(
                {
                    "error": "Token refresh failed",
                    "fyers_code": data.get("code"),
                    "fyers_message": data.get("message", "Unknown error"),
                },
                status=400,
            )

        new_token = data.get("access_token")
        account.access_token = new_token
        if data.get("refresh_token"):
            account.refresh_token = data["refresh_token"]
        account.save(update_fields=["access_token", "refresh_token"])

        logger.info(
            "Manual token refresh | user=%s | account=%s",
            request.user.id, account.id,
        )

        # ✅ FIX: Naya token WS ko bhi do
        _start_feed_after_token(new_token, account.app_id, account.id)

        return Response(
            {
                "success": True,
                "message": "Token refreshed successfully!",
                "broker": "fyers",
                "label": label,
            }
        )


# ─────────────────────────────────────────────────────────────────
# Fyers — Save PIN/TOTP (daily auto-refresh ke liye)
# ─────────────────────────────────────────────────────────────────

class FyersSavePinView(APIView):
    """
    POST /api/v1/brokers/fyers/save-pin/
    Body:
      { "pin": "1234", "account_id": 1 }  (4-digit PIN)
      OR
      { "totp_secret": "BASE32STRING", "account_id": 1 }  (TOTP authenticator)

    TOTP ko preference dete hain — agar dono ho toh TOTP save hoga.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        pin          = request.data.get("pin", "")
        totp_secret  = request.data.get("totp_secret", "")
        account_id   = request.data.get("account_id")

        # Validation: PIN ya TOTP mein se ek zaruri hai
        if not pin and not totp_secret:
            return Response(
                {"error": "Either 4-digit PIN or TOTP secret required"},
                status=400
            )

        # PIN validation (agar PIN diya ho)
        if pin and (not str(pin).isdigit() or len(str(pin)) != 4):
            return Response(
                {"error": "PIN must be 4-digit numeric"},
                status=400
            )

        # TOTP validation (agar TOTP secret diya ho)
        if totp_secret:
            try:
                # Validate TOTP secret by trying to generate a code
                pyotp.TOTP(totp_secret).now()
            except Exception as e:
                return Response(
                    {"error": f"Invalid TOTP secret: {str(e)}"},
                    status=400
                )

        try:
            if account_id:
                account = BrokerAccount.objects.get(
                    id=account_id, user=request.user, broker="fyers"
                )
            else:
                account = (
                    BrokerAccount.objects.filter(
                        user=request.user, broker="fyers", is_active=True
                    )
                    .order_by("-created_at")
                    .first()
                )
            if not account:
                return Response({"error": "Fyers account not found"}, status=404)
        except BrokerAccount.DoesNotExist:
            return Response({"error": "Account not found"}, status=404)

        # TOTP ko preference — agar dono ho toh TOTP save hoga
        if totp_secret:
            account.totp_secret = totp_secret
            account.fyers_pin = ""  # Clear PIN agar TOTP set ho raha hai
            account.save(update_fields=["totp_secret", "fyers_pin"])

            return Response(
                {
                    "success": True,
                    "message": "TOTP authenticator linked! Daily auto-refresh enabled! 🎉",
                    "auto_refresh_time": "8:30 AM IST daily",
                    "auth_method": "totp",
                }
            )
        else:
            account.fyers_pin = str(pin)
            account.totp_secret = ""  # Clear TOTP agar PIN set ho raha hai
            account.save(update_fields=["fyers_pin", "totp_secret"])

            return Response(
                {
                    "success": True,
                    "message": "PIN saved. Daily auto-refresh enabled! 🎉",
                    "auto_refresh_time": "8:30 AM IST daily",
                    "auth_method": "pin",
                }
            )


# ─────────────────────────────────────────────────────────────────
# Fyers — Auto-Refresh Status
# ─────────────────────────────────────────────────────────────────

class FyersAutoRefreshStatusView(APIView):
    """
    GET /api/v1/brokers/fyers/refresh-status/
    Flutter app open hone pe call karo.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        accounts = BrokerAccount.objects.filter(
            user=request.user, broker="fyers", is_active=True
        )

        result = []
        for account in accounts:
            has_pin     = bool(account.fyers_pin)
            has_totp    = bool(getattr(account, 'totp_secret', ''))
            has_token   = bool(account.access_token)
            has_refresh = bool(account.refresh_token)

            # TOTP ko preference — agar dono set hain toh TOTP use hoga
            auth_method = "totp" if has_totp else ("pin" if has_pin else None)
            auto_refresh_enabled = (has_pin or has_totp) and has_refresh

            result.append(
                {
                    "account_id": account.id,
                    "label": account.label,
                    "has_access_token": has_token,
                    "has_refresh_token": has_refresh,
                    "auto_refresh_enabled": auto_refresh_enabled,
                    "auth_method": auth_method,
                    "needs_auth": not (has_pin or has_totp),
                    "needs_relogin": not has_refresh,
                }
            )

        return Response({"accounts": result})


# ─────────────────────────────────────────────────────────────────
# Broker Remove
# ─────────────────────────────────────────────────────────────────

class BrokerRemoveView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, pk):
        try:
            account = BrokerAccount.objects.get(pk=pk, user=request.user)
            account.delete()
            return Response({"success": True, "message": "Broker removed"})
        except BrokerAccount.DoesNotExist:
            return Response({"success": False, "message": "Not found"}, status=404)