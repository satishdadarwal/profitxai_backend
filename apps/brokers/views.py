# apps/brokers/views.py
#
# ✅ MULTI-USER FIX:
#   - BrokerAccount ab fyers_client_id field se identify hoga
#   - Har user ka APNA account — dono ek hi App ID (master) use karte hain
#     lekin alag alag tokens store hote hain
#   - FyersAutoLoginView: user ke fyers_client_id se account match hoga,
#     kisi aur ka account accidentally pick nahi hoga
#   - BrokerAccountListView: is_verified=False wale bhi dikhenge (status display ke liye)
#     lekin "connected" sirf is_verified=True + has_token=True pe maana jayega
#
# Flow:
#   Master App (RBPEXX0S3A-200) → Auth URL generate karta hai sabke liye
#   Chanchal login karta hai → sirf Chanchal ka BrokerAccount update hota hai
#   Rahul login karta hai   → sirf Rahul ka BrokerAccount update hota hai
#   Koi bhi                 → apna token, alag alag

import hashlib
import logging
import threading

from django.contrib.auth import get_user_model
from django.http import HttpResponse, HttpResponseRedirect
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from django.conf import settings


def flutter_redirect(url: str) -> HttpResponseRedirect:
    class FlutterRedirect(HttpResponseRedirect):
        allowed_schemes = ["http", "https", "profitxai"]
    return FlutterRedirect(url)


import pyotp
import requests
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import BrokerAccount
from .serializers import BrokerAccountSerializer

logger = logging.getLogger(__name__)

FYERS_API_BASE  = "https://api-t1.fyers.in/api/v3"  # ✅ SDK confirmed: api-t1/api/v3/generate-authcode
FYERS_AUTH_BASE = "https://api-t1.fyers.in/api/v3"  # ✅ validate-authcode & validate-refresh-token → api-t1 confirmed
FLUTTER_BASE   = settings.FLUTTER_DEEP_LINK   # e.g. profitxai://fyers-callback
REDIRECT_URI   = settings.FYERS_REDIRECT_URI


# ─────────────────────────────────────────────────────────────────
# Helper — token save ke baad feed start / restart karo
# ─────────────────────────────────────────────────────────────────

def _start_feed_after_token(new_access_token: str, app_id: str, account_id: int):
    def _run():
        try:
            from apps.websocket.fyers_feed import feed_manager
            token_str = f"{app_id}:{new_access_token}"
            if feed_manager._started or feed_manager._connected:
                feed_manager.restart_with_new_token(token_str)
                logger.info("Feed restarted with new token | account=%s", account_id)
            else:
                feed_manager.start(token=token_str)
                logger.info("Feed started fresh after token save | account=%s", account_id)
        except Exception as e:
            logger.error("_start_feed_after_token failed | account=%s | %s", account_id, e)

    threading.Thread(target=_run, daemon=True).start()


# ─────────────────────────────────────────────────────────────────
# Helper — Dhan token save ke baad feed turant update karo
# ─────────────────────────────────────────────────────────────────

def _restart_dhan_feed_after_token(client_id: str, access_token: str, account_id: int):
    """
    DhanConnectView token save ke baad yeh call hota hai.
    dhan_feed_manager ko naya token in-memory deta hai — 90s wait nahi karna padta.
    """
    def _run():
        try:
            from apps.websocket.dhan_feed import dhan_feed_manager
            dhan_feed_manager.restart_with_new_token(client_id, access_token)
            logger.info(
                "_restart_dhan_feed_after_token: ✅ feed token updated | account=%s | client=%s",
                account_id, client_id,
            )
        except Exception as e:
            logger.error(
                "_restart_dhan_feed_after_token: ❌ failed | account=%s | %s",
                account_id, e,
            )

    threading.Thread(target=_run, daemon=True).start()


# ─────────────────────────────────────────────────────────────────
# Broker List / Create
# ─────────────────────────────────────────────────────────────────

class BrokerAccountListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        # ✅ FIX: "Master Account" label wale exclude karo (admin-only accounts)
        # Baaki sab dikhao — is_verified=False wale bhi (status display ke liye)
        accounts = BrokerAccount.objects.filter(
            user=request.user,
            is_active=True,
        ).exclude(label="Master Account")

        from django.utils import timezone as _tz

        def _token_valid(a):
            """Token hai AND expire nahi hua."""
            if a.broker == "delta":
                return bool(getattr(a, "api_key", None))
            # ✅ Dhan — dhan_access_token field use karta hai
            if a.broker == "dhan":
                return bool(getattr(a, "dhan_access_token", None))
            if not a.access_token:
                return False
            if a.token_expiry and a.token_expiry < _tz.now():
                return False   # expired (known expiry)
            return True

        def _token_expired(a) -> bool:
            """
            Token expire hua hai ya nahi.

            Cases:
            1. token_expiry set hai aur past mein → True (expired)
            2. token_expiry null hai + token purana laga raha hai →
               updated_at se estimate karo (Fyers token ~1 din chalta hai)
            3. Delta/Dhan → expire nahi hota (API key / manual token)
            """
            if a.broker not in ("fyers", "dhan"):
                return False
            # ✅ Dhan — 24hr validity, updated_at se check karo
            if a.broker == "dhan":
                if not getattr(a, "dhan_access_token", None):
                    return False
                if a.updated_at:
                    import datetime
                    age = _tz.now() - a.updated_at
                    if age > datetime.timedelta(hours=23):
                        return True  # 24hr validity expire
                return False
            if a.broker != "fyers":
                return False
            if not a.access_token:
                return False  # token hi nahi — disconnected, not expired

            # Case 1: expiry field set hai
            if a.token_expiry:
                return a.token_expiry < _tz.now()

            # Case 2: expiry null hai — updated_at se estimate karo
            # Fyers OAuth token ~1 din (23-24 hrs) valid hota hai
            if a.updated_at:
                import datetime
                age = _tz.now() - a.updated_at
                if age > datetime.timedelta(hours=23):
                    return True  # likely expired

            return False

        data = [
            {
                "id":              a.id,
                "broker":          a.broker,
                "label":           a.label or a.broker.capitalize(),
                "is_active":       a.is_active,
                "is_verified":     a.is_verified,
                "has_token":       _token_valid(a),
                # ✅ token_expired: Flutter mein "Reconnect" badge dikhane ke liye
                # token_expiry null bhi ho toh updated_at se detect karo
                "token_expired":   _token_expired(a),
                "fyers_client_id": getattr(a, "fyers_client_id", "") or "",
                "created_at":      a.created_at,
                "updated_at":      a.updated_at,
            }
            for a in accounts
        ]
        return Response(data)


class BrokerAccountCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = BrokerAccountSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=400)

        broker = request.data.get("broker", "").lower()

        # ── Delta: API key verify karke is_verified=True set karo ──
        if broker == "delta":
            api_key    = request.data.get("api_key", "").strip()
            api_secret = request.data.get("api_secret", "").strip()

            if not api_key or not api_secret:
                return Response(
                    {"error": "Delta ke liye api_key aur api_secret dono zaroori hain."},
                    status=400,
                )

            # Delta API ping karke verify karo
            try:
                import time, hmac, hashlib as _hl, requests as _req
                timestamp  = str(int(time.time()))
                method     = "GET"
                path       = "/v2/profile"
                sig_data   = method + timestamp + path
                signature  = hmac.new(
                    api_secret.encode(), sig_data.encode(), _hl.sha256
                ).hexdigest()
                resp = _req.get(
                    f"https://api.india.delta.exchange{path}",
                    headers={
                        "api-key":       api_key,
                        "timestamp":     timestamp,
                        "signature":     signature,
                        "Content-Type":  "application/json",
                    },
                    timeout=10,
                )
                if resp.status_code == 200 and resp.json().get("success"):
                    account = serializer.save(
                        user=request.user,
                        api_secret=api_secret,
                        is_verified=True,
                        is_active=True,
                    )
                    logger.info("Delta verified | user=%s | account=%s", request.user.id, account.id)
                    return Response({
                        "id":         account.id,
                        "broker":     account.broker,
                        "label":      account.label,
                        "is_verified": True,
                        "message":    "Delta Exchange connected! ✅",
                    }, status=201)
                else:
                    return Response(
                        {
                            "error": "Delta API key invalid hai. Credentials check karo.",
                            "delta_message": resp.json().get("error", {}).get("message", ""),
                        },
                        status=400,
                    )
            except Exception as e:
                logger.error("Delta verify error | user=%s | %s", request.user.id, e)
                return Response(
                    {"error": f"Delta API se connect nahi ho pa raha: {e}"},
                    status=502,
                )

        # ── Other brokers (generic save) ───────────────────────────
        account = serializer.save(user=request.user)
        return Response(serializer.data, status=201)


# ─────────────────────────────────────────────────────────────────
# Fyers — Step 1: Auth URL generate karo
# ─────────────────────────────────────────────────────────────────

class FyersAuthURLView(APIView):
    """
    Flutter request:
      { "label": "My Fyers", "account_id": 53 }   ← account_id optional

    Backend:
      1. Master App ID (settings) use karta hai — user se nahi maangta
      2. User ka existing BrokerAccount dhundha ya naya banata hai
      3. state = "{user_id}__{account_id}" — callback mein exact account resolve hoga
      4. Auth URL return karta hai

    ✅ MULTI-USER: Har user ka apna account_id state mein encode hota hai.
       Fyers → redirect → callback mein us exact account ka token save hoga.
       Chanchal ka login Rahul ke account ko touch nahi karega.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        account_id = request.data.get("account_id")
        label      = request.data.get("label", "My Fyers")

        master_app_id     = getattr(settings, "FYERS_APP_ID", "").strip()
        master_secret_key = getattr(settings, "FYERS_SECRET_KEY", "").strip()

        if not master_app_id:
            return Response(
                {"error": "FYERS_APP_ID .env mein set nahi hai. Admin se contact karo."},
                status=500,
            )
        if not master_secret_key:
            return Response(
                {"error": "FYERS_SECRET_KEY .env mein set nahi hai. Admin se contact karo."},
                status=500,
            )
        if not REDIRECT_URI:
            return Response(
                {"error": "FYERS_REDIRECT_URI .env mein set nahi hai"},
                status=500,
            )

        # ── Account dhundho ya banao ──────────────────────────────

        account = None

        if account_id:
            try:
                # ✅ FIX: strict user check — sirf apna account
                account = BrokerAccount.objects.get(
                    id=account_id, user=request.user, broker="fyers"
                )
                logger.info("FyersAuthURL: account found by id=%s | user=%s", account_id, request.user.id)
            except BrokerAccount.DoesNotExist:
                logger.warning("FyersAuthURL: account_id=%s not found for user=%s", account_id, request.user.id)
                return Response({"error": f"Broker account ID {account_id} not found"}, status=404)

        if not account:
            # ✅ FIX: label match karo SIRF is user ke accounts mein
            account = BrokerAccount.objects.filter(
                user=request.user, broker="fyers", label=label
            ).first()

        if not account:
            # Create new account for this user
            account = BrokerAccount.objects.create(
                user=request.user,
                broker="fyers",
                label=label,
                app_id=master_app_id,
                secret_key=master_secret_key,
                redirect_uri=REDIRECT_URI,
                is_verified=False,
                fyers_client_id="",  # ✅ FIX: NOT NULL constraint
            )
            logger.info("FyersAuthURL: created new account=%s for user=%s", account.id, request.user.id)
        else:
            # Update master credentials on existing account
            update_fields = []
            if account.app_id != master_app_id:
                account.app_id = master_app_id
                update_fields.append("app_id")
            if account.secret_key != master_secret_key:
                account.secret_key = master_secret_key
                update_fields.append("secret_key")
            if not account.redirect_uri:
                account.redirect_uri = REDIRECT_URI
                update_fields.append("redirect_uri")
            if update_fields:
                account.save(update_fields=update_fields)
                logger.info("FyersAuthURL: updated account=%s fields=%s", account.id, update_fields)

        # ── Auth URL banao ────────────────────────────────────────
        # ✅ FIX: state = "{user_id}__{account_id}"
        # Callback mein EXACT is account ka token save hoga
        # Chanchal ka state alag hoga, Rahul ka alag

        # ✅ SDK-style URL generation — urllib.parse.urlencode (same as fyers_apiv3 SDK)
        import base64, urllib.parse as _urlparse
        raw_state = f"{request.user.id}:{account.id}"
        state_value = base64.urlsafe_b64encode(raw_state.encode()).decode().rstrip("=")

        auth_params = _urlparse.urlencode({
            "client_id":     master_app_id,
            "redirect_uri":  REDIRECT_URI,
            "response_type": "code",
            "state":         state_value,
        })
        auth_url = f"{FYERS_API_BASE}/generate-authcode?{auth_params}"

        logger.info(
            "FyersAuthURL generated | user=%s | app_id=%s | account=%s | state=%s",
            request.user.id, master_app_id, account.id, state_value,
        )

        return Response({
            "auth_url":   auth_url,
            "label":      account.label,
            "account_id": account.id,
        })


# ─────────────────────────────────────────────────────────────────
# Fyers — Step 2: Callback (GET = browser/WebView redirect, POST = Flutter direct)
# ─────────────────────────────────────────────────────────────────

@method_decorator(csrf_exempt, name='dispatch')
class FyersCallbackView(APIView):

    def get_permissions(self):
        if self.request.method == "GET":
            return [AllowAny()]
        return [IsAuthenticated()]

    # ──────────────────────────────────────────────────────────────
    # GET — Fyers server yahan redirect karta hai
    # state = "{user_id}__{account_id}" se EXACT account resolve hoga
    # ✅ MULTI-USER: Chanchal aur Rahul ka state alag — cross-contamination impossible
    # ──────────────────────────────────────────────────────────────

    def get(self, request):
        auth_code = request.query_params.get("auth_code")
        state     = request.query_params.get("state")

        if not auth_code:
            logger.error("FyersCallback GET: no auth_code | params=%s", dict(request.query_params))
            return flutter_redirect(f"{FLUTTER_BASE}?error=no_auth_code")

        if not state:
            logger.error("FyersCallback GET: no state | params=%s", dict(request.query_params))
            return HttpResponse(
                "<html><body style='font-family:sans-serif;text-align:center;padding:50px'>"
                "<h2 style='color:#f44336'>❌ Login Failed</h2>"
                "<p>State parameter missing. Please try again from the app.</p>"
                "</body></html>",
                status=400,
            )

        User = get_user_model()
        admin_account_id = None

        # ── State parse karo ──────────────────────────────────────

        if state == "master_setup":
            try:
                user = User.objects.filter(is_superuser=True).first() or User.objects.first()
                if not user:
                    return HttpResponse(
                        "<html><body style='font-family:sans-serif;text-align:center;padding:50px'>"
                        "<h1>❌ No User Found</h1>"
                        "<p>python manage.py createsuperuser pehle chalao</p>"
                        "</body></html>",
                        status=500,
                    )
                logger.info("FyersCallback GET: master_setup | user=%s", user.id)
            except Exception as e:
                return HttpResponse(
                    f"<html><body><h1>❌ Error</h1><p>{e}</p></body></html>",
                    status=500,
                )

        elif state in ("sample", "sample_state", "state"):
            return HttpResponse(
                "<html><body style='font-family:sans-serif;text-align:center;padding:50px'>"
                "<h1 style='color:#4CAF50'>✅ Fyers App Permission Granted!</h1>"
                "<p>App successfully connected. Ab Admin panel mein jaao aur Login karo.</p>"
                "</body></html>"
            )

        else:
            # ✅ MAIN FLOW: state = base64(user_id:account_id)
            # Fyers state mein hyphens/underscores "invalid appId" dete the — ab base64 use karo
            import base64 as _b64
            try:
                # base64 decode karo (with padding)
                padded = state + "=" * (4 - len(state) % 4)
                raw = _b64.urlsafe_b64decode(padded).decode()

                if ":" in raw:
                    # New format: base64(user_id:account_id)
                    user_id_str, acc_id_str = raw.split(":", 1)
                elif "__" in raw:
                    # Old format fallback: user_id__account_id
                    user_id_str, acc_id_str = raw.split("__", 1)
                else:
                    raise ValueError(f"Unknown decoded state format: {raw!r}")

                user = User.objects.get(id=user_id_str)
                admin_account_id = int(acc_id_str)
                logger.info(
                    "FyersCallback GET: user=%s | account=%s",
                    user.id, admin_account_id,
                )
            except Exception as e:
                logger.error("FyersCallback GET: state parse failed | state=%s | err=%s", state, e)
                return flutter_redirect(f"{FLUTTER_BASE}?error=invalid_state")

        request.user = user

        # ── Account resolve karo ──────────────────────────────────
        # ✅ FIX: STRICTLY is user ka account — dusre user ka nahi

        account = None
        if admin_account_id:
            try:
                # user=user check MANDATORY — cross-user access rokta hai
                account = BrokerAccount.objects.get(
                    pk=admin_account_id, user=user, broker="fyers"
                )
                logger.info("FyersCallback: found exact account=%s | user=%s", account.id, user.id)
            except BrokerAccount.DoesNotExist:
                logger.warning("FyersCallback GET: account %s not found for user %s", admin_account_id, user.id)

        if not account:
            # ✅ FIX: master_setup ke liye "Master Account" label wala dhundho
            # Regular users ke liye exclude karo (unka apna label hoga)
            if state == "master_setup":
                account = BrokerAccount.objects.filter(
                    user=user, broker="fyers", label="Master Account"
                ).first()
            else:
                account = BrokerAccount.objects.filter(
                    user=user, broker="fyers"
                ).exclude(label="Master Account").order_by("-updated_at").first()

        if not account:
            # Naya banao is user ke liye — get_or_create se duplicate safe
            target_label = "Master Account" if state == "master_setup" else "My Fyers"
            account, created = BrokerAccount.objects.get_or_create(
                user=user,
                broker="fyers",
                label=target_label,
                defaults=dict(
                    app_id=getattr(settings, "FYERS_APP_ID", ""),
                    secret_key=getattr(settings, "FYERS_SECRET_KEY", ""),
                    redirect_uri=REDIRECT_URI,
                    is_verified=False,
                    fyers_client_id="",
                ),
            )
            if created:
                logger.info("FyersCallback: created new account=%s | user=%s | label=%s", account.id, user.id, target_label)
            else:
                logger.info("FyersCallback: get_or_create found existing account=%s | user=%s", account.id, user.id)
        else:
            # Blank credentials fill karo
            update_fields = []
            if not account.app_id:
                account.app_id = getattr(settings, "FYERS_APP_ID", "")
                update_fields.append("app_id")
            if not account.secret_key:
                account.secret_key = getattr(settings, "FYERS_SECRET_KEY", "")
                update_fields.append("secret_key")
            if update_fields:
                account.save(update_fields=update_fields)

        # ── Token exchange karo ───────────────────────────────────

        result = self._exchange_auth_code(request, auth_code, label=account.label, account_id=account.id)

        # ── Response decide karo ─────────────────────────────────

        if state == "master_setup":
            if result.status_code == 200:
                return HttpResponse(
                    "<html><body style='font-family:sans-serif;text-align:center;padding:50px'>"
                    "<h1 style='color:#4CAF50'>✅ Fyers Master Account Connected!</h1>"
                    "<p>Token saved. You can close this window.</p>"
                    "<script>setTimeout(() => { window.close(); }, 3000);</script>"
                    "</body></html>"
                )
            else:
                error_msg = result.data.get("error", "unknown_error")
                hint_msg  = result.data.get("hint", "")
                fyers_msg = result.data.get("fyers_message", "")
                fyers_p   = "<p style='color:#888'>" + fyers_msg + "</p>" if fyers_msg else ""
                hint_p    = "<p style='background:#fff3cd;padding:10px;border-radius:6px'>" + hint_msg + "</p>" if hint_msg else ""
                error_html = (
                    "<html><body style='font-family:sans-serif;text-align:center;padding:50px'>"
                    "<h1 style='color:#f44336'>Token Exchange Failed</h1>"
                    "<p><strong>" + error_msg + "</strong></p>"
                    + fyers_p + hint_p
                    + "<br><p style='color:#666;font-size:14px'>Auth code 60 seconds mein expire hota hai."
                    " Admin panel se dobara Login button click karo.</p>"
                    "<a href='/admin/brokers/brokeraccount/' style='display:inline-block;"
                    "margin-top:20px;padding:10px 24px;background:#417690;color:white;"
                    "border-radius:6px;text-decoration:none'>Back to Admin Panel</a>"
                    "</body></html>"
                )
                return HttpResponse(error_html, status=400)

        # ✅ FIX: Teen scenarios handle karo:
        #   1. Web popup  → window.opener.postMessage + close
        #   2. Mobile WebView (InAppBrowser) → profitxai:// deep link
        #   3. Web same-tab fallback → ngrok/production URL pe redirect
        flutter_deep_link  = getattr(settings, "FLUTTER_DEEP_LINK", "profitxai://fyers-callback")
        flutter_web_base   = getattr(settings, "FLUTTER_WEB_BASE_URL", "").strip().rstrip("/")

        if result.status_code == 200:
            logger.info(
                "FyersCallback GET: ✅ success | user=%s | account=%s",
                user.id, account.id,
            )
            web_fallback_url = f"{flutter_web_base}/#/fyers-callback?success=true" if flutter_web_base else ""
            deep_link_url    = f"{flutter_deep_link}?success=true"

            return HttpResponse(f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <title>Fyers Connected ✅</title>
  <style>
    body {{ font-family: sans-serif; background: #0D1117; color: white;
           display: flex; align-items: center; justify-content: center;
           min-height: 100vh; margin: 0; flex-direction: column; text-align: center; }}
    .icon {{ font-size: 64px; margin-bottom: 16px; }}
    h2 {{ color: #3FB950; margin: 0 0 8px; }}
    p {{ color: #8B949E; font-size: 14px; }}
  </style>
</head>
<body>
  <div class="icon">✅</div>
  <h2>Fyers Connected!</h2>
  <p>App pe wapas ja raha hai...</p>
  <script>
    var deepLink   = '{deep_link_url}';
    var webFallback = '{web_fallback_url}';

    // Case 1: Web popup — parent ko notify karo aur close karo
    if (window.opener) {{
      try {{
        window.opener.postMessage({{ type: 'FYERS_AUTH_SUCCESS' }}, '*');
      }} catch(e) {{}}
      setTimeout(function() {{ window.close(); }}, 1200);
    }}
    // Case 2 & 3: WebView ya same-tab
    else {{
      // Pehle deep link try karo (mobile Flutter app pakad lega)
      window.location.href = deepLink;

      // 2.5s baad agar still browser mein hain → web URL pe jao
      setTimeout(function() {{
        if (webFallback) {{
          window.location.replace(webFallback);
        }}
      }}, 2500);
    }}
  </script>
</body>
</html>""")
        else:
            error_msg = result.data.get("error", "token_exchange_failed")
            fyers_msg = result.data.get("fyers_message", "")
            hint_msg  = result.data.get("hint", "")
            logger.error(
                "FyersCallback GET: ❌ failed | user=%s | error=%s | fyers=%s",
                user.id, error_msg, fyers_msg,
            )
            deep_link_url = f"{flutter_deep_link}?error=login_failed"
            fyers_detail  = f"<p class='fyers-msg'>Fyers: {fyers_msg}</p>" if fyers_msg else ""
            hint_detail   = f"<p class='hint'>💡 {hint_msg}</p>" if hint_msg else ""

            return HttpResponse(f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <title>Login Failed</title>
  <style>
    body {{ font-family: sans-serif; background: #0D1117; color: white;
           display: flex; align-items: center; justify-content: center;
           min-height: 100vh; margin: 0; flex-direction: column; text-align: center; padding: 20px; }}
    .icon {{ font-size: 64px; margin-bottom: 16px; }}
    h2 {{ color: #FF7B72; margin: 0 0 8px; }}
    .err {{ background: #161B22; border: 1px solid #30363D; border-radius: 8px;
             padding: 12px 16px; margin-top: 12px; max-width: 420px; word-break: break-word;
             color: #FF7B72; font-size: 14px; font-weight: 500; }}
    .fyers-msg {{ color: #8B949E; font-size: 13px; margin-top: 8px; }}
    .hint {{ background: #1C2128; border: 1px solid #388BFD44; border-radius: 8px;
              padding: 10px 14px; margin-top: 12px; max-width: 420px;
              color: #79C0FF; font-size: 13px; }}
    p {{ color: #8B949E; font-size: 14px; margin-top: 16px; }}
  </style>
</head>
<body>
  <div class="icon">❌</div>
  <h2>Fyers Login Failed</h2>
  <div class="err">{error_msg}</div>
  {fyers_detail}
  {hint_detail}
  <p>App band karo aur dobara "Connect Fyers" try karo.</p>
  <script>
    if (!window.opener) {{
      window.location.href = '{deep_link_url}';
    }} else {{
      try {{
        window.opener.postMessage({{ type: 'FYERS_AUTH_ERROR', error: '{error_msg}' }}, '*');
      }} catch(e) {{}}
      setTimeout(function() {{ window.close(); }}, 4000);
    }}
  </script>
</body>
</html>""", status=400)

    # ──────────────────────────────────────────────────────────────
    # POST — Flutter authenticated request
    # ──────────────────────────────────────────────────────────────

    def post(self, request):
        auth_code  = request.data.get("auth_code")
        label      = request.data.get("label", "My Fyers")
        account_id = request.data.get("account_id")

        if not auth_code:
            return Response({"error": "auth_code required"}, status=400)

        if not request.user or not request.user.is_authenticated:
            return Response(
                {"error": "Authentication required. Please login first."},
                status=401,
            )

        if account_id:
            try:
                # ✅ FIX: user=request.user — dusre user ka account access nahi
                account = BrokerAccount.objects.get(
                    id=account_id, user=request.user, broker="fyers"
                )
                label = account.label

                update_fields = []
                if not account.app_id:
                    account.app_id = getattr(settings, "FYERS_APP_ID", "")
                    update_fields.append("app_id")
                if not account.secret_key:
                    account.secret_key = getattr(settings, "FYERS_SECRET_KEY", "")
                    update_fields.append("secret_key")
                if update_fields:
                    account.save(update_fields=update_fields)

            except BrokerAccount.DoesNotExist:
                return Response({"error": f"Broker account {account_id} not found"}, status=404)

        return self._exchange_auth_code(request, auth_code, label=label, account_id=account_id)

    # ──────────────────────────────────────────────────────────────
    # _exchange_auth_code — common helper
    # ✅ FIX: STRICT user ownership check — kisi aur ka account nahi
    # ──────────────────────────────────────────────────────────────

    def _exchange_auth_code(self, request, auth_code, label="My Fyers", account_id=None):
        try:
            if account_id:
                # ✅ CRITICAL: user=request.user check MANDATORY
                account = BrokerAccount.objects.get(
                    id=account_id, user=request.user, broker="fyers"
                )
            else:
                # ✅ FIX: SIRF is user ke accounts mein dhundho
                account = (
                    BrokerAccount.objects.filter(user=request.user, broker="fyers")
                    .exclude(label="Master Account")
                    .order_by("-updated_at")
                    .first()
                )
            if not account:
                return Response(
                    {"error": "No Fyers account found. Call /fyers/auth-url/ first."},
                    status=400,
                )
        except BrokerAccount.DoesNotExist:
            return Response({"error": f"Account {account_id} not found"}, status=404)
        except Exception as e:
            return Response({"error": f"DB error: {str(e)}"}, status=500)

        app_id     = account.app_id     or getattr(settings, "FYERS_APP_ID", "")
        secret_key = account.secret_key or getattr(settings, "FYERS_SECRET_KEY", "")

        if not app_id or not secret_key:
            return Response(
                {"error": "App ID ya Secret Key missing. Admin se contact karo."},
                status=500,
            )

        # ✅ FIX: Fyers appIdHash = sha256(FULL_app_id:secret) — "-200" suffix RAKHNA hai
        # Browser OAuth flow mein Fyers full app_id se hash validate karta hai
        app_id_hash = hashlib.sha256(f"{app_id}:{secret_key}".encode()).hexdigest()

        logger.info(
            "_exchange_auth_code | user=%s | account=%s | app_id=%s | hash=%s",
            request.user.id, account.id, app_id, app_id_hash[:16],
        )

        try:
            resp = requests.post(
                f"{FYERS_AUTH_BASE}/validate-authcode",
                json={
                    "grant_type": "authorization_code",
                    "appIdHash":  app_id_hash,
                    "code":       auth_code,
                },
                timeout=15,
            )
        except requests.exceptions.Timeout:
            return Response({"error": "Fyers API timeout. Try again."}, status=504)
        except requests.exceptions.RequestException as e:
            return Response({"error": f"Fyers network error: {str(e)}"}, status=502)
        except Exception as e:
            return Response({"error": f"Unexpected error: {str(e)}"}, status=500)

        # ✅ FIX: Pehle text dekho, phir JSON try karo
        # Fyers 403 pe plain text/HTML return karta hai — JSON parse fail hota tha
        # aur fallback raise_for_status() se "API unreachable" error aata tha
        resp_text = resp.text
        try:
            data = resp.json()
        except Exception:
            data = {}

        logger.info(
            "_exchange_auth_code: fyers_status=%s | app_id=%s | body_prefix=%s",
            resp.status_code, app_id, resp_text[:200],
        )

        if resp.status_code == 403:
            logger.error(
                "_exchange_auth_code: 403 Forbidden | app_id=%s | body=%s",
                app_id, resp_text[:300],
            )
            return Response(
                {
                    "error": "Fyers 403 Forbidden — appIdHash ya redirect_uri mismatch",
                    "fyers_message": data.get("message", resp_text[:200]),
                    "hint": (
                        "Fix karo: (1) Fyers dashboard mein redirect_uri bilkul same ho "
                        f"({REDIRECT_URI}), "
                        "(2) FYERS_APP_ID aur FYERS_SECRET_KEY .env mein sahi ho, "
                        "(3) auth_code 60s mein expire hota hai — jaldi login karo."
                    ),
                },
                status=400,
            )

        if resp.status_code not in (200, 201):
            logger.error(
                "_exchange_auth_code: unexpected status=%s | body=%s",
                resp.status_code, resp_text[:300],
            )
            return Response(
                {
                    "error": f"Fyers returned HTTP {resp.status_code}",
                    "fyers_message": data.get("message", resp_text[:200]),
                },
                status=400,
            )

        if data.get("s") != "ok":
            logger.error("Fyers token exchange failed: %s", data)
            return Response(
                {
                    "error":         "Token generation failed",
                    "fyers_code":    data.get("code"),
                    "fyers_message": data.get("message", data),
                    "hint": (
                        "Common causes: auth_code expired (60s limit), "
                        "redirect_uri mismatch with Fyers dashboard, "
                        "wrong app_id/secret_key"
                    ),
                },
                status=400,
            )

        new_token = data.get("access_token")
        # ✅ FIX: SIRF is account ka token update karo
        account.access_token  = new_token
        account.refresh_token = data.get("refresh_token", "")
        account.is_active     = True
        account.is_verified   = True
        account.save()

        logger.info(
            "Fyers token saved ✅ | user=%s | account=%s | label=%s",
            request.user.id, account.id, account.label,
        )
        _start_feed_after_token(new_token, app_id, account.id)

        return Response({
            "success":    True,
            "message":    "Fyers connected successfully!",
            "broker":     "fyers",
            "label":      account.label,
            "account_id": account.id,
        })


# ─────────────────────────────────────────────────────────────────
# Fyers — Token Refresh (manual, PIN required)
# ─────────────────────────────────────────────────────────────────

class FyersTokenRefreshView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        label      = request.data.get("label", "My Fyers")
        pin        = request.data.get("pin", "")
        account_id = request.data.get("account_id")

        if not pin:
            return Response(
                {"error": "pin (4-digit Fyers PIN) required for token refresh"},
                status=400,
            )

        try:
            if account_id:
                # ✅ FIX: user check mandatory
                account = BrokerAccount.objects.get(
                    id=account_id, user=request.user, broker="fyers", is_active=True
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
                return Response({"error": "Active Fyers account not found"}, status=404)
        except BrokerAccount.DoesNotExist:
            return Response({"error": "Account not found"}, status=404)
        except Exception as e:
            return Response({"error": str(e)}, status=500)

        if not account.refresh_token:
            return Response(
                {"error": "No refresh token stored. Please login again via /auth-url/"},
                status=400,
            )

        app_id      = account.app_id     or getattr(settings, "FYERS_APP_ID", "")
        secret_key  = account.secret_key or getattr(settings, "FYERS_SECRET_KEY", "")
        # ✅ FIX: FULL app_id se hash banao — "-200" suffix RAKHNA hai
        app_id_hash = hashlib.sha256(f"{app_id}:{secret_key}".encode()).hexdigest()

        try:
            resp = requests.post(
                f"{FYERS_AUTH_BASE}/validate-refresh-token",
                json={
                    "grant_type":    "refresh_token",
                    "appIdHash":     app_id_hash,
                    "refresh_token": account.refresh_token,
                    "pin":           str(pin),
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
                    "error":         "Token refresh failed",
                    "fyers_code":    data.get("code"),
                    "fyers_message": data.get("message", "Unknown error"),
                },
                status=400,
            )

        new_token = data.get("access_token")
        account.access_token = new_token
        if data.get("refresh_token"):
            account.refresh_token = data["refresh_token"]
        account.save(update_fields=["access_token", "refresh_token"])

        logger.info("Manual token refresh | user=%s | account=%s", request.user.id, account.id)
        _start_feed_after_token(new_token, app_id, account.id)

        return Response({
            "success": True,
            "message": "Token refreshed successfully!",
            "broker":  "fyers",
            "label":   label,
        })


# ─────────────────────────────────────────────────────────────────
# Fyers — Save PIN/TOTP
# ─────────────────────────────────────────────────────────────────

class FyersSavePinView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        pin         = request.data.get("pin", "")
        totp_secret = request.data.get("totp_secret", "")
        account_id  = request.data.get("account_id")

        if not pin and not totp_secret:
            return Response({"error": "Either 4-digit PIN or TOTP secret required"}, status=400)

        if pin and (not str(pin).isdigit() or len(str(pin)) != 4):
            return Response({"error": "PIN must be 4-digit numeric"}, status=400)

        if totp_secret:
            try:
                pyotp.TOTP(totp_secret).now()
            except Exception as e:
                return Response({"error": f"Invalid TOTP secret: {str(e)}"}, status=400)

        try:
            if account_id:
                # ✅ FIX: user check
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

        if totp_secret:
            account.totp_secret = totp_secret
            account.fyers_pin   = ""
            account.save(update_fields=["totp_secret", "fyers_pin"])
            return Response({
                "success": True,
                "message": "TOTP authenticator linked! Daily auto-refresh enabled! 🎉",
                "auto_refresh_time": "8:30 AM IST daily",
                "auth_method": "totp",
            })
        else:
            account.fyers_pin   = str(pin)
            account.totp_secret = ""
            account.save(update_fields=["fyers_pin", "totp_secret"])
            return Response({
                "success": True,
                "message": "PIN saved. Daily auto-refresh enabled! 🎉",
                "auto_refresh_time": "8:30 AM IST daily",
                "auth_method": "pin",
            })


# ─────────────────────────────────────────────────────────────────
# Fyers — Auto-Refresh Status
# ─────────────────────────────────────────────────────────────────

class FyersAutoRefreshStatusView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        # ✅ FIX: is user ke accounts — dusre ka nahi
        accounts = BrokerAccount.objects.filter(
            user=request.user, broker="fyers", is_active=True
        )
        result = []
        for account in accounts:
            has_pin     = bool(account.fyers_pin)
            has_totp    = bool(getattr(account, "totp_secret", ""))
            has_token   = bool(account.access_token)
            has_refresh = bool(account.refresh_token)
            auth_method = "totp" if has_totp else ("pin" if has_pin else None)
            result.append({
                "account_id":           account.id,
                "label":                account.label,
                "fyers_client_id":      getattr(account, "fyers_client_id", "") or "",
                "has_access_token":     has_token,
                "has_refresh_token":    has_refresh,
                "auto_refresh_enabled": (has_pin or has_totp) and has_refresh,
                "auth_method":          auth_method,
                "needs_auth":           not (has_pin or has_totp),
                "needs_relogin":        not has_refresh,
            })
        return Response({"accounts": result})


# ─────────────────────────────────────────────────────────────────
# Broker Remove
# ─────────────────────────────────────────────────────────────────

class BrokerRemoveView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, pk):
        try:
            account = BrokerAccount.objects.get(pk=pk, user=request.user)
        except BrokerAccount.DoesNotExist:
            return Response({"success": False, "message": "Not found"}, status=404)

        try:
            # ✅ FIX: PROTECT constraint — linked BrokerOrders pehle delete karo
            from apps.brokers.models import BrokerOrder
            deleted_orders, _ = BrokerOrder.objects.filter(broker_account=account).delete()
            if deleted_orders:
                logger.info("BrokerRemove: deleted %s linked orders | account=%s", deleted_orders, pk)
            account.delete()
            return Response({"success": True, "message": "Broker removed"})
        except Exception as e:
            logger.error("BrokerRemove error | account=%s | %s", pk, e)
            return Response({"success": False, "message": f"Delete failed: {str(e)}"}, status=500)


# ─────────────────────────────────────────────────────────────────
# Broker Funds View
# ─────────────────────────────────────────────────────────────────

class BrokerFundsView(APIView):
    """
    ✅ MULTI-BROKER: Fyers, Dhan, Delta — sab ka balance ek call mein.
    ?broker=fyers/dhan/delta se specific broker request karo.
    Default: priority order dhan → fyers → delta.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        broker_slug = (request.query_params.get("broker") or "").lower().strip()

        if not broker_slug:
            # Priority: Dhan → Zerodha → Delta → Fyers
            for slug in ("dhan", "zerodha", "delta", "fyers"):
                if self._get_account(request.user, slug):
                    broker_slug = slug
                    break

        if broker_slug == "dhan":
            return self._dhan_funds(request)
        elif broker_slug == "zerodha":
            return self._zerodha_funds(request)
        elif broker_slug == "delta":
            return self._delta_funds(request)
        else:
            return self._fyers_funds(request)

    def _get_account(self, user, broker):
        """Active account with valid token."""
        if broker == "dhan":
            return BrokerAccount.objects.filter(
                user=user, broker="dhan", is_active=True, is_verified=True,
            ).exclude(dhan_access_token="").exclude(dhan_access_token__isnull=True).first()
        if broker == "zerodha":
            return BrokerAccount.objects.filter(
                user=user, broker="zerodha", is_active=True, is_verified=True,
            ).exclude(access_token="").exclude(access_token__isnull=True).order_by("-updated_at").first()
        if broker == "delta":
            return BrokerAccount.objects.filter(
                user=user, broker="delta", is_active=True,
            ).exclude(api_key="").exclude(api_key__isnull=True).first()
        # fyers default
        return BrokerAccount.objects.filter(
            user=user, broker="fyers", is_active=True, is_verified=True,
        ).exclude(access_token="").exclude(access_token__isnull=True).order_by("-updated_at").first()
    
    def _dhan_funds(self, request):
        account = self._get_account(request.user, "dhan")
        if not account:
            account = BrokerAccount.objects.filter(
                broker="dhan", is_active=True, is_verified=True,
            ).exclude(dhan_access_token="").exclude(dhan_access_token__isnull=True).first()
        if not account:
            return Response({
                "source": "dhan", "available": 0, "used_margin": 0,
                "total": 0, "token_valid": False,
                "error": "Dhan account connected nahi hai",
            }, status=200)
        try:
            import requests as _req
            resp = _req.get(
                "https://api.dhan.co/v2/fundlimit",
                headers={
                    "access-token": account.dhan_access_token,
                    "client-id":    account.dhan_client_id,
                    "Content-Type": "application/json",
                },
                timeout=10,
            )
            if resp.status_code == 401:
                return Response({
                    "source": "dhan", "available": 0, "used_margin": 0,
                    "total": 0, "token_valid": False,
                    "error": "Dhan token expire ho gaya — app mein reconnect karo",
                }, status=200)
            resp.raise_for_status()
            data = resp.json()
            available = float(data.get("availabelBalance", 0) or 0)
            used      = float(data.get("utilizedAmount",  0) or 0)
            total     = float(data.get("sodLimit",        0) or 0)
            if total == 0:
                total = available + used
            return Response({
                "source": "dhan", "available": available,
                "used_margin": used, "total": total,
                "token_valid": True, "error": None,
            })
        except Exception as e:
            logger.error("BrokerFundsView dhan | user=%s | %s", request.user.id, e)
            return Response({
                "source": "dhan", "available": 0, "used_margin": 0,
                "total": 0, "token_valid": False, "error": str(e),
            }, status=200)

    def _fyers_funds(self, request):
        account = self._get_account(request.user, "fyers")
        if not account:
            return Response({
                "source": "fyers", "available": 0, "used_margin": 0,
                "total": 0, "token_valid": False,
                "error": "Fyers account connected nahi hai",
            }, status=200)
        app_id = account.app_id or getattr(settings, "FYERS_APP_ID", "")
        try:
            from fyers_apiv3 import fyersModel as fyers_api
            fyers = fyers_api.FyersModel(
                client_id=app_id, token=account.access_token,
                is_async=False, log_path="",
            )
            data = fyers.funds()
            if data.get("s") != "ok":
                return Response({
                    "source": "fyers", "available": 0, "used_margin": 0,
                    "total": 0, "token_valid": False,
                    "error": data.get("message", "Fyers funds API error"),
                }, status=200)
            available = used_margin = total = 0.0
            for item in data.get("fund_limit", []):
                title = item.get("title", "")
                val = float(item.get("equityAmount") or item.get("commodityAmount") or 0)
                if title == "Available Balance":
                    available = val
                elif title == "Utilized Amount":
                    used_margin = val
                elif title == "Total Balance":
                    total = val
            return Response({
                "source": "fyers", "available": available,
                "used_margin": used_margin, "total": total,
                "token_valid": True, "error": None,
            })
        except Exception as e:
            logger.error("BrokerFundsView fyers | user=%s | %s", request.user.id, e)
            return Response({
                "source": "fyers", "available": 0, "used_margin": 0,
                "total": 0, "token_valid": False, "error": str(e),
            }, status=200)

    def _zerodha_funds(self, request):
        account = self._get_account(request.user, "zerodha")
        if not account:
            return Response({
                "source": "zerodha", "available": 0, "used_margin": 0,
                "total": 0, "token_valid": False,
                "error": "Zerodha account connected nahi hai",
            }, status=200)
        try:
            import requests as _req
            resp = _req.get(
                "https://api.kite.trade/user/margins",
                headers={
                    "X-Kite-Version": "3",
                    "Authorization":  f"token {account.api_key}:{account.access_token}",
                },
                timeout=10,
            )
            if resp.status_code in (401, 403):
                return Response({
                    "source": "zerodha", "available": 0, "used_margin": 0,
                    "total": 0, "token_valid": False,
                    "error": "Zerodha token expire — app mein reconnect karo",
                }, status=200)
            resp.raise_for_status()
            equity    = resp.json().get("data", {}).get("equity", {})
            available = float(equity.get("available", {}).get("cash", 0) or 0)
            used      = float(equity.get("utilised", {}).get("debits", 0) or 0)
            total     = float(equity.get("net", 0) or available + used)
            return Response({
                "source": "zerodha", "available": available,
                "used_margin": used, "total": total,
                "token_valid": True, "error": None,
            })
        except Exception as e:
            logger.error("BrokerFundsView zerodha | user=%s | %s", request.user.id, e)
            return Response({
                "source": "zerodha", "available": 0, "used_margin": 0,
                "total": 0, "token_valid": False, "error": str(e),
            }, status=200)

    def _delta_funds(self, request):
        try:
            from broker_adapters.factory import BrokerAdapterFactory
            # ✅ FIX: _get_account() use karo — get_adapter(user) Dhan return kar deta tha
            account = self._get_account(request.user, "delta")
            if account is None:
                return Response({
                    "source": "delta", "available": 0, "used_margin": 0,
                    "total": 0, "token_valid": False,
                    "error": "Delta account connected nahi hai",
                }, status=200)
            adapter = BrokerAdapterFactory.get_adapter_for_account(account)
            funds = adapter.get_funds()
            return Response({
                "source": "delta",
                "available": round(float(funds.available) * 84.0, 2),
                "used_margin": round(float(funds.used) * 84.0, 2),
                "total": round(float(funds.total) * 84.0, 2),
                "currency": "INR",
                "available_usdt": funds.available,
                "token_valid": True, "error": None,
            })
        except Exception as e:
            logger.error("BrokerFundsView delta | user=%s | %s", request.user.id, e)
            return Response({
                "source": "delta", "available": 0, "used_margin": 0,
                "total": 0, "token_valid": False, "error": str(e),
            }, status=200)


# ══════════════════════════════════════════════════════════════════
# Fyers — Automatic Login (TOTP + PIN → direct token)
# ✅ MULTI-USER FIX: fyers_client_id se account uniquely identify hoga
# ══════════════════════════════════════════════════════════════════

def _fyers_programmatic_login(
    *,
    app_id: str,
    secret_key: str,
    redirect_uri: str,
    fyers_client_id: str,
    totp_secret: str,
    fyers_pin: str,
    state: str = "auto",
) -> dict:
    """
    Fyers API v3 — 5-step programmatic login.

    Step 1: POST /vagator/v2/send_login_otp   { fy_id, app_id: "2" }  → request_key
    Step 2: POST /vagator/v2/verify_otp        { request_key, otp }   → request_key2
    Step 3: POST /vagator/v2/verify_pin        { request_key2, identity_type, identifier } → data.access_token
    Step 4: POST /api/v3/token                 Bearer: trade_token    → HTTP 308 + Url (auth_code)
    Step 5: POST /api/v3/validate-authcode     { appIdHash, code }    → access_token
    """
    VAGATOR_BASE = "https://api-t2.fyers.in/vagator/v2"
    TOKEN_BASE   = "https://api-t1.fyers.in/api/v3"
    TIMEOUT      = (5, 15)

    # ✅ FIX: FULL app_id se hash banao — "-200" suffix RAKHNA hai
    # JLQLCHMNSR-200 → hash = sha256(JLQLCHMNSR-200:secret)
    base_app_id = app_id.split("-")[0]  if "-" in app_id else app_id  # sirf step4 ke liye
    app_type    = app_id.split("-")[-1] if "-" in app_id else "100"
    app_id_hash = hashlib.sha256(f"{app_id}:{secret_key}".encode()).hexdigest()

    session = requests.Session()
    session.headers.update({"Content-Type": "application/json", "Accept": "application/json"})

    # Step 1
    logger.info("FyersAutoLogin step1: send_login_otp | client=%s", fyers_client_id)
    try:
        r1 = session.post(
            f"{VAGATOR_BASE}/send_login_otp",
            json={"fy_id": fyers_client_id, "app_id": "2"},
            timeout=TIMEOUT,
        )
        logger.info("step1 status=%s body=%s", r1.status_code, r1.text[:300])
        d1 = r1.json()
    except requests.exceptions.Timeout:
        return {"success": False, "error": "Fyers API timeout (step 1 send_login_otp)"}
    except Exception as e:
        return {"success": False, "error": f"Step 1 network error: {e}"}

    if r1.status_code != 200:
        return {
            "success": False,
            "error": d1.get("message", f"Step 1 failed: HTTP {r1.status_code}"),
            "raw": d1,
        }

    request_key = d1.get("request_key")
    if not request_key:
        return {"success": False, "error": "Step 1: request_key missing", "raw": d1}

    # Step 2: TOTP
    logger.info("FyersAutoLogin step2: verify_otp (TOTP)")
    try:
        otp_code = pyotp.TOTP(totp_secret.strip().replace(" ", "")).now()
    except Exception as e:
        return {
            "success": False,
            "error": f"TOTP generate failed: {e}",
            "hint": "TOTP secret format galat hai. Base32 string chahiye.",
        }

    try:
        r2 = session.post(
            f"{VAGATOR_BASE}/verify_otp",
            json={"request_key": request_key, "otp": otp_code},
            timeout=TIMEOUT,
        )
        logger.info("step2 status=%s body=%s", r2.status_code, r2.text[:300])
        d2 = r2.json()
    except requests.exceptions.Timeout:
        return {"success": False, "error": "Fyers API timeout (step 2 verify_otp)"}
    except Exception as e:
        return {"success": False, "error": f"Step 2 network error: {e}"}

    if r2.status_code != 200:
        return {
            "success": False,
            "error": d2.get("message", f"Step 2 TOTP verify failed: HTTP {r2.status_code}"),
            "hint": "TOTP galat ya phone ka time sync off hai.",
            "raw": d2,
        }

    request_key2 = d2.get("request_key")
    if not request_key2:
        return {"success": False, "error": "Step 2: request_key missing after TOTP verify", "raw": d2}

    # Step 3: PIN
    logger.info("FyersAutoLogin step3: verify_pin")
    try:
        r3 = session.post(
            f"{VAGATOR_BASE}/verify_pin",
            json={
                "request_key":   request_key2,
                "identity_type": "pin",
                "identifier":    str(fyers_pin),
            },
            timeout=TIMEOUT,
        )
        logger.info("step3 status=%s body=%s", r3.status_code, r3.text[:300])
        d3 = r3.json()
    except requests.exceptions.Timeout:
        return {"success": False, "error": "Fyers API timeout (step 3 verify_pin)"}
    except Exception as e:
        return {"success": False, "error": f"Step 3 network error: {e}"}

    if r3.status_code != 200:
        return {
            "success": False,
            "error": d3.get("message", f"Step 3 PIN verify failed: HTTP {r3.status_code}"),
            "hint": "4-digit Fyers PIN galat hai.",
            "raw": d3,
        }

    trade_token = d3.get("data", {}).get("access_token")
    if not trade_token:
        return {
            "success": False,
            "error": "Step 3: trade access_token missing in verify_pin response",
            "raw": d3,
        }

    # Step 4: /token → auth_code
    logger.info("FyersAutoLogin step4: /token | app_id=%s | app_type=%s", base_app_id, app_type)
    try:
        r4 = session.post(
            f"{TOKEN_BASE}/token",
            json={
                "fyers_id":       fyers_client_id,
                "app_id":         base_app_id,
                "redirect_uri":   redirect_uri,
                "appType":        app_type,
                "code_challenge": "",
                "state":          state,
                "scope":          "",
                "nonce":          "",
                "response_type":  "code",
                "create_cookie":  True,
            },
            headers={"Authorization": f"Bearer {trade_token}"},
            timeout=TIMEOUT,
            allow_redirects=False,
        )
        logger.info("step4 status=%s body=%s", r4.status_code, r4.text[:400])
        d4 = r4.json()
    except requests.exceptions.Timeout:
        return {"success": False, "error": "Fyers API timeout (step 4 token)"}
    except Exception as e:
        return {"success": False, "error": f"Step 4 network error: {e}"}

    if r4.status_code != 308:
        return {
            "success": False,
            "error": d4.get("message", f"Step 4 token failed: HTTP {r4.status_code}"),
            "hint": "redirect_uri Fyers dashboard mein registered honi chahiye bilkul same.",
            "raw": d4,
        }

    url_field = d4.get("Url", "")
    if not url_field:
        return {"success": False, "error": "Step 4: Url field missing in token response", "raw": d4}

    try:
        from urllib.parse import urlparse, parse_qs
        parsed   = urlparse(url_field)
        params   = parse_qs(parsed.query)
        auth_code = (
            params.get("auth_code", [None])[0]
            or params.get("code", [None])[0]
        )
    except Exception as e:
        return {"success": False, "error": f"Step 4: auth_code parse failed: {e}", "raw": d4}

    if not auth_code:
        return {"success": False, "error": "Step 4: auth_code not found in redirect URL", "raw": d4}

    logger.info("FyersAutoLogin: ✅ auth_code obtained | prefix=%s...", auth_code[:8])
    return {"success": True, "auth_code": auth_code}


class FyersAutoLoginView(APIView):
    """
    POST /api/v1/brokers/fyers/auto-login/

    ✅ MULTI-USER FIX:
      - fyers_client_id field ab BrokerAccount mein save hota hai
      - Account lookup: pehle fyers_client_id se, phir label se, phir naya banao
      - Chanchal ka login sirf Chanchal ke account ko update karega
      - Rahul ka login sirf Rahul ke account ko update karega

    Request:
        {
            "fyers_client_id": "YC00329",        ← Chanchal ka Fyers ID
            "totp_secret":     "JBSWY3DPEHPK3PXP",
            "fyers_pin":       "1234",
            "label":           "My Fyers",        (optional)
            "app_id":          "7PXFFNUPJ6-100",  (optional — apna app hai toh dena)
            "secret_key":      "K7UNO63532",       (optional — apna secret)
            "redirect_uri":    "https://..."       (optional — apne app ki redirect URI)
        }

    ✅ app_id priority order:
       1. Request mein diya → use karo (user ka apna app)
       2. Nahi diya → master app (settings) use karo
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        fyers_client_id  = (request.data.get("fyers_client_id") or "").strip().upper()
        totp_secret      = (request.data.get("totp_secret") or "").strip().replace(" ", "")
        fyers_pin        = str(request.data.get("fyers_pin") or "").strip()
        label            = (request.data.get("label") or "My Fyers").strip()
        # ✅ User apna App ID de sakta hai — master app ki dependency nahi
        user_app_id      = (request.data.get("app_id") or "").strip()
        user_secret_key  = (request.data.get("secret_key") or "").strip()
        user_redirect    = (request.data.get("redirect_uri") or "").strip()

        # ── Validation ────────────────────────────────────────────
        if not fyers_client_id:
            return Response({"error": "fyers_client_id required"}, status=400)
        if not totp_secret:
            return Response({"error": "totp_secret required (Google Authenticator Base32 key)"}, status=400)
        if not fyers_pin:
            return Response({"error": "fyers_pin required (4-digit Fyers PIN)"}, status=400)
        if not fyers_pin.isdigit() or len(fyers_pin) != 4:
            return Response({"error": "fyers_pin must be exactly 4 digits"}, status=400)

        try:
            pyotp.TOTP(totp_secret).now()
        except Exception:
            return Response(
                {"error": "Invalid TOTP secret format. Base32 string chahiye."},
                status=400,
            )

        # ── Credentials: user ka app > master app ─────────────────
        master_app_id    = getattr(settings, "FYERS_APP_ID", "").strip()
        master_secret    = getattr(settings, "FYERS_SECRET_KEY", "").strip()
        master_redirect  = getattr(settings, "FYERS_REDIRECT_URI", "").strip()

        # User ka apna app diya → use karo; nahi diya → master
        app_id       = user_app_id    or master_app_id
        secret_key   = user_secret_key or master_secret
        redirect_uri = user_redirect   or master_redirect

        if not app_id:
            return Response({"error": "App ID missing. Apna Fyers App ID daalo ya admin se contact karo."}, status=400)
        if not secret_key:
            return Response({"error": "Secret Key missing. Apna Fyers Secret daalo ya admin se contact karo."}, status=400)
        if not redirect_uri:
            return Response({"error": "Redirect URI missing. Apni app ki redirect URI daalo."}, status=400)

        logger.info(
            "FyersAutoLogin: starting | user=%s | client=%s | label=%s",
            request.user.id, fyers_client_id, label,
        )

        # ── Step A: Programmatic login → auth_code ────────────────
        result = _fyers_programmatic_login(
            app_id=app_id,
            secret_key=secret_key,
            redirect_uri=redirect_uri,
            fyers_client_id=fyers_client_id,
            totp_secret=totp_secret,
            fyers_pin=fyers_pin,
            state=str(request.user.id),
        )

        if not result.get("success"):
            logger.error("FyersAutoLogin: failed | user=%s | %s", request.user.id, result.get("error"))
            return Response(
                {
                    "error": result.get("error", "Auto-login failed"),
                    "hint":  result.get("hint", ""),
                },
                status=400,
            )

        auth_code = result["auth_code"]

        # ── Step B: auth_code → access_token ─────────────────────
        # ✅ FIX: Fyers SDK uses FULL client_id (with suffix) for hash
        # sha256(JLQLCHMNSR-200:secret) — NOT sha256(JLQLCHMNSR:secret)
        app_id_hash = hashlib.sha256(f"{app_id}:{secret_key}".encode()).hexdigest()
        logger.info(
            "AutoLogin Step B: validate-authcode | app_id=%s | hash=%s",
            app_id, app_id_hash[:16],
        )
        try:
            resp = requests.post(
                f"{FYERS_AUTH_BASE}/validate-authcode",
                json={
                    "grant_type": "authorization_code",
                    "appIdHash":  app_id_hash,
                    "code":       auth_code,
                },
                timeout=(5, 15),
            )
            resp_text = resp.text
            try:
                data = resp.json()
            except Exception:
                data = {}
            logger.info("AutoLogin Step B response: status=%s body=%s", resp.status_code, resp_text[:300])
        except requests.exceptions.Timeout:
            return Response({"error": "Token exchange timeout. Try again."}, status=504)
        except requests.exceptions.RequestException as e:
            return Response({"error": f"Token exchange network error: {e}"}, status=502)

        if resp.status_code not in (200, 201):
            return Response({
                "error": f"validate-authcode failed: HTTP {resp.status_code}",
                "fyers_message": data.get("message", resp_text[:300]),
                "hint": "auth_code 60 seconds mein expire hota hai. Dobara try karo.",
            }, status=400)

        if data.get("s") != "ok":
            logger.error("FyersAutoLogin: token exchange failed | %s", data)
            return Response(
                {
                    "error":         "Token exchange failed",
                    "fyers_message": data.get("message", str(data)),
                    "hint":          "Auth code expire ho gaya (60s limit). Dobara try karo.",
                },
                status=400,
            )

        new_access_token  = data.get("access_token", "")
        new_refresh_token = data.get("refresh_token", "")

        # ── Step C: Save / update BrokerAccount ──────────────────
        # ✅ MULTI-USER FIX: Account find karne ka order:
        #   1. is user ka account jiska fyers_client_id match kare
        #   2. is user ka account jiska label match kare
        #   3. Naya banao — kisi aur user ka account KABHI nahi

        account = BrokerAccount.objects.filter(
            user=request.user,       # ← ALWAYS is user ka
            broker="fyers",
            fyers_client_id=fyers_client_id,   # ← exact client ID match
        ).first()

        if not account:
            # Label se dhundho (same user, same label)
            account = BrokerAccount.objects.filter(
                user=request.user,
                broker="fyers",
                label=label,
            ).first()

        if account:
            # ✅ Existing account update karo
            account.label           = label
            account.fyers_client_id = fyers_client_id   # ← save for future lookups
            account.app_id          = app_id
            account.secret_key      = secret_key
            account.redirect_uri    = redirect_uri
            account.access_token    = new_access_token
            account.refresh_token   = new_refresh_token
            account.totp_secret     = totp_secret
            account.fyers_pin       = fyers_pin
            account.is_active       = True
            account.is_verified     = True
            account.save()
            logger.info(
                "FyersAutoLogin: updated account=%s | user=%s | client=%s",
                account.id, request.user.id, fyers_client_id,
            )
        else:
            # ✅ Naya account banao — sirf is user ke liye
            account = BrokerAccount.objects.create(
                user            = request.user,
                broker          = "fyers",
                label           = label,
                fyers_client_id = fyers_client_id,   # ← store from day 1
                app_id          = app_id,
                secret_key      = secret_key,
                redirect_uri    = redirect_uri,
                access_token    = new_access_token,
                refresh_token   = new_refresh_token,
                totp_secret     = totp_secret,
                fyers_pin       = fyers_pin,
                is_active       = True,
                is_verified     = True,
            )
            logger.info(
                "FyersAutoLogin: created account=%s | user=%s | client=%s",
                account.id, request.user.id, fyers_client_id,
            )

        logger.info(
            "FyersAutoLogin: ✅ success | user=%s | account=%s | client=%s | token=%s...",
            request.user.id, account.id, fyers_client_id, new_access_token[:8],
        )

        # ── Step D: Start feed ────────────────────────────────────
        try:
            _start_feed_after_token(new_access_token, app_id, account.id)
        except Exception as fe:
            logger.warning("FyersAutoLogin: feed start failed (non-critical) | %s", fe)

        return Response({
            "success":    True,
            "message":    "Fyers connected! 🎉 Roz 8:30 AM pe auto-refresh hoga.",
            "broker":     "fyers",
            "label":      account.label,
            "account_id": account.id,
        })

# ─────────────────────────────────────────────────────────────────
# Dhan — Connect / Token Save
# ─────────────────────────────────────────────────────────────────
#
# Flow:
#   Flutter app → user Dhan se manually token generate karta hai
#   (web.dhan.co → My Profile → Access Token)
#   → Flutter POST /api/brokers/dhan/connect/
#     { "client_id": "1000000001", "access_token": "eyJ..." }
#   → Backend saves to BrokerAccount
#
# SEBI Note:
#   Dhan access token 24hr valid hota hai (SEBI mandate).
#   User ko daily reconnect karna padega ya auto-refresh setup karo.
#   Static IP: VPS ka IP web.dhan.co pe whitelist hona chahiye.
# ─────────────────────────────────────────────────────────────────

class DhanConnectView(APIView):
    """
    Flutter → Dhan token save karo.

    POST body:
    {
        "client_id":    "1000000001",
        "access_token": "eyJhbGciOi...",
        "label":        "My Dhan"       ← optional
    }
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        client_id    = (request.data.get("client_id") or "").strip()
        access_token = (request.data.get("access_token") or "").strip()
        label        = (request.data.get("label") or "My Dhan").strip()

        if not client_id:
            return Response({"error": "client_id required hai"}, status=400)
        if not access_token:
            return Response({"error": "access_token required hai"}, status=400)

        # Token validate karo — Dhan API call
        try:
            import requests as _req
            verify_resp = _req.get(
                "https://api.dhan.co/v2/fundlimit",
                headers={
                    "access-token": access_token,
                    "client-id":    client_id,
                    "Content-Type": "application/json",
                },
                timeout=10,
            )
            if verify_resp.status_code == 401:
                return Response(
                    {"error": "Invalid token. Dhan se naya token generate karo."},
                    status=400,
                )
            if verify_resp.status_code == 403:
                return Response(
                    {
                        "error": (
                            "Access denied (403). Server ka static IP Dhan mein "
                            "whitelist nahi hua. web.dhan.co → My Profile → Static IP "
                            "mein VPS ka IP daalo."
                        )
                    },
                    status=400,
                )
            verify_resp.raise_for_status()
            logger.info(
                "DhanConnect: token verified | user=%s | client=%s",
                request.user.id, client_id,
            )
        except Exception as ve:
            logger.warning("DhanConnect: token verify failed | %s", ve)
            # Verify fail ho toh bhi save karo — token valid ho sakta hai
            # bas network issue ho temporarily

        # BrokerAccount save/update
        account, created = BrokerAccount.objects.update_or_create(
            user=request.user,
            broker="dhan",
            label=label,
            defaults={
                "dhan_client_id":    client_id,
                "dhan_access_token": access_token,
                "is_active":         True,
                "is_verified":       True,
            },
        )

        logger.info(
            "DhanConnect: %s | account=%s | user=%s | client=%s",
            "created" if created else "updated",
            account.id, request.user.id, client_id,
        )

        # ✅ FIX: Feed turant naya token use kare — 90s poll wait mat karo
        # Sirf master account ka token feed ke liye use hota hai
        master_client_id = getattr(settings, "DHAN_MASTER_CLIENT_ID", "").strip()
        if not master_client_id or client_id == master_client_id:
            _restart_dhan_feed_after_token(client_id, access_token, account.id)

        return Response({
            "success":    True,
            "message":    "Dhan connected! ✅ Token save ho gaya.",
            "broker":     "dhan",
            "label":      account.label,
            "account_id": account.id,
            "note":       (
                "Dhan token 24 ghante mein expire hoga (SEBI rule). "
                "Daily reconnect karo ya auto-refresh setup karo."
            ),
        })


class DhanTokenStatusView(APIView):
    """
    Dhan token status check karo — valid hai ya expired?
    Flutter app 'Reconnect' badge ke liye.

    GET /api/brokers/dhan/status/
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        account = BrokerAccount.objects.filter(
            user=request.user,
            broker="dhan",
            is_active=True,
        ).first()

        if not account:
            return Response({"connected": False, "message": "Dhan account nahi mila"})

        if not account.dhan_access_token:
            return Response({
                "connected": False,
                "account_id": account.id,
                "message": "Token nahi hai — Dhan se connect karo",
            })

        # Live token check
        try:
            import requests as _req
            resp = _req.get(
                "https://api.dhan.co/v2/fundlimit",
                headers={
                    "access-token": account.dhan_access_token,
                    "client-id":    account.dhan_client_id,
                },
                timeout=8,
            )
            token_valid = resp.status_code == 200
            status_msg = "Token valid ✅" if token_valid else f"Token expired (HTTP {resp.status_code})"
        except Exception as e:
            token_valid = False
            status_msg = f"Check failed: {e}"

        return Response({
            "connected":   token_valid,
            "account_id":  account.id,
            "client_id":   account.dhan_client_id,
            "label":       account.label,
            "message":     status_msg,
        })

# ══════════════════════════════════════════════════════════════════
# Zerodha — OAuth Connect
# ══════════════════════════════════════════════════════════════════
#
# Flow:
#   1. Flutter → GET /api/brokers/zerodha/auth-url/
#      → Backend returns kite.zerodha.com login URL
#   2. User browser mein login karta hai
#   3. Zerodha redirects to /api/brokers/zerodha/callback/?request_token=XXX
#   4. Backend: request_token → access_token (KiteConnect API)
#   5. access_token DB mein save, Flutter ko deep-link redirect
#
# Zerodha API docs: https://kite.trade/docs/connect/v3/
#
# SEBI note: Access token daily expire hota hai.
#            Roz manually login karo ya programmatic login setup karo.
# ══════════════════════════════════════════════════════════════════

class ZerodhaAuthURLView(APIView):
    """
    GET /api/brokers/zerodha/auth-url/
    Zerodha login page ka URL return karo.
    Flutter isko InAppWebView ya browser mein open karta hai.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        api_key = getattr(settings, "ZERODHA_API_KEY", "").strip()
        if not api_key:
            return Response(
                {"error": "ZERODHA_API_KEY .env mein set nahi hai"},
                status=500,
            )

        # ✅ State mein user_id encode karo — callback mein identify karne ke liye
        import base64, json
        state = base64.urlsafe_b64encode(
            json.dumps({"user_id": str(request.user.id)}).encode()
        ).decode()

        login_url = f"https://kite.zerodha.com/connect/login?api_key={api_key}&v=3"

        logger.info(
            "ZerodhaAuthURL: generated | user=%s | api_key=%s...",
            request.user.id, api_key[:6],
        )

        return Response({
            "auth_url": login_url,
            "api_key":  api_key,
        })


class ZerodhaCallbackView(APIView):
    """
    GET /api/brokers/zerodha/callback/?request_token=XXX&status=success
    Zerodha redirect ke baad yahan aata hai.
    request_token → access_token exchange karo → DB save → Flutter deep link.
    """
    permission_classes = []   # AllowAny — browser redirect hai, JWT nahi

    def get(self, request):
        from django.http import HttpResponseRedirect
        import hashlib

        request_token = request.query_params.get("request_token", "").strip()
        status_param  = request.query_params.get("status", "").strip()

        flutter_base = getattr(settings, "FLUTTER_WEB_BASE_URL",
                               "http://localhost:3000")

        if status_param != "success" or not request_token:
            error_msg = request.query_params.get("message", "Login cancelled")
            logger.warning("ZerodhaCallback: failed | status=%s", status_param)
            return HttpResponseRedirect(
                f"{flutter_base}/#/home?zerodha_error={error_msg}"
            )

        api_key    = getattr(settings, "ZERODHA_API_KEY", "").strip()
        api_secret = getattr(settings, "ZERODHA_API_SECRET", "").strip()

        if not api_key or not api_secret:
            logger.error("ZerodhaCallback: ZERODHA_API_KEY/SECRET missing in .env")
            return HttpResponseRedirect(
                f"{flutter_base}/#/home?zerodha_error=config_missing"
            )

        # ── Step 1: request_token → access_token ─────────────────
        # Zerodha checksum = sha256(api_key + request_token + api_secret)
        checksum = hashlib.sha256(
            f"{api_key}{request_token}{api_secret}".encode()
        ).hexdigest()

        try:
            import requests as _req
            resp = _req.post(
                "https://api.kite.trade/session/token",
                data={
                    "api_key":       api_key,
                    "request_token": request_token,
                    "checksum":      checksum,
                },
                headers={"X-Kite-Version": "3"},
                timeout=15,
            )
            data = resp.json()
        except Exception as e:
            logger.error("ZerodhaCallback: token exchange failed | %s", e)
            return HttpResponseRedirect(
                f"{flutter_base}/#/home?zerodha_error=token_exchange_failed"
            )

        if resp.status_code != 200 or "data" not in data:
            err = data.get("message", str(data))
            logger.error("ZerodhaCallback: Zerodha API error | %s", err)
            return HttpResponseRedirect(
                f"{flutter_base}/#/home?zerodha_error={err}"
            )

        token_data    = data["data"]
        access_token  = token_data.get("access_token", "")
        zerodha_user_id = token_data.get("user_id", "")  # e.g. "AB1234"
        user_name     = token_data.get("user_name", "")

        if not access_token:
            logger.error("ZerodhaCallback: access_token missing in response")
            return HttpResponseRedirect(
                f"{flutter_base}/#/home?zerodha_error=no_access_token"
            )

        # ── Step 2: User identify karo ────────────────────────────
        # Zerodha user_id se BrokerAccount dhundo, warna superuser use karo
        from django.contrib.auth import get_user_model
        User = get_user_model()

        # First: koi existing account hai is zerodha_user_id ke saath?
        existing = BrokerAccount.objects.filter(
            broker="zerodha",
            zerodha_user_id=zerodha_user_id,
        ).select_related("user").first()

        if existing:
            django_user = existing.user
        else:
            # Fallback: superuser ka account (single-user setup)
            django_user = User.objects.filter(is_superuser=True).first()
            if not django_user:
                logger.error("ZerodhaCallback: no user found for zerodha_user_id=%s", zerodha_user_id)
                return HttpResponseRedirect(
                    f"{flutter_base}/#/home?zerodha_error=user_not_found"
                )

        # ── Step 3: DB mein save karo ─────────────────────────────
        from django.utils import timezone
        from datetime import timedelta

        account, created = BrokerAccount.objects.update_or_create(
            user=django_user,
            broker="zerodha",
            zerodha_user_id=zerodha_user_id,
            defaults={
                "api_key":      api_key,
                "access_token": access_token,
                "is_active":    True,
                "is_verified":  True,
                "token_expiry": timezone.now() + timedelta(hours=8),  # Zerodha ~8am expire
                "label":        f"Zerodha {user_name or zerodha_user_id}",
            },
        )

        logger.info(
            "ZerodhaCallback: ✅ %s | account=%s | user=%s | zerodha_id=%s",
            "created" if created else "updated",
            account.id, django_user.id, zerodha_user_id,
        )

        # ── Step 4: Flutter ko redirect karo ─────────────────────
        return HttpResponseRedirect(
            f"{flutter_base}/#/home?zerodha_connected=true"
            f"&zerodha_user={zerodha_user_id}"
        )


class ZerodhaTokenStatusView(APIView):
    """
    GET /api/brokers/zerodha/status/
    Token valid hai ya nahi — Flutter reconnect badge ke liye.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        account = BrokerAccount.objects.filter(
            user=request.user,
            broker="zerodha",
            is_active=True,
        ).first()

        if not account:
            return Response({"connected": False, "message": "Zerodha account nahi mila"})

        if not account.access_token:
            return Response({
                "connected": False,
                "account_id": account.id,
                "message": "Token nahi hai — Zerodha se connect karo",
            })

        # Live check
        try:
            import requests as _req
            resp = _req.get(
                "https://api.kite.trade/user/profile",
                headers={
                    "X-Kite-Version": "3",
                    "Authorization":  f"token {account.api_key}:{account.access_token}",
                },
                timeout=8,
            )
            token_valid = resp.status_code == 200
            status_msg = "Token valid ✅" if token_valid else f"Token expired (HTTP {resp.status_code})"
        except Exception as e:
            token_valid = False
            status_msg = f"Check failed: {e}"

        return Response({
            "connected":       token_valid,
            "account_id":      account.id,
            "zerodha_user_id": getattr(account, "zerodha_user_id", ""),
            "label":           account.label,
            "message":         status_msg,
        })


class ZerodhaFundsView(APIView):
    """
    GET /api/brokers/zerodha/funds/
    Live margin balance — BrokerFundsView se call hota hai.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        account = BrokerAccount.objects.filter(
            user=request.user,
            broker="zerodha",
            is_active=True,
            is_verified=True,
        ).exclude(access_token="").exclude(access_token__isnull=True).first()

        if not account:
            return Response({
                "source": "zerodha", "available": 0, "used_margin": 0,
                "total": 0, "token_valid": False,
                "error": "Zerodha account connected nahi hai",
            })

        try:
            import requests as _req
            resp = _req.get(
                "https://api.kite.trade/user/margins",
                headers={
                    "X-Kite-Version": "3",
                    "Authorization":  f"token {account.api_key}:{account.access_token}",
                },
                timeout=10,
            )
            if resp.status_code == 403:
                return Response({
                    "source": "zerodha", "available": 0, "used_margin": 0,
                    "total": 0, "token_valid": False,
                    "error": "Zerodha token expire ho gaya — app mein reconnect karo",
                })
            resp.raise_for_status()
            data   = resp.json().get("data", {})
            equity = data.get("equity", {})
            net    = equity.get("net", 0)
            available = float(
                equity.get("available", {}).get("cash", 0) or
                equity.get("available", {}).get("live_balance", 0) or 0
            )
            used = float(equity.get("utilised", {}).get("debits", 0) or 0)
            return Response({
                "source":      "zerodha",
                "available":   available,
                "used_margin": used,
                "total":       float(net or available + used),
                "token_valid": True,
                "error":       None,
            })
        except Exception as e:
            logger.error("ZerodhaFundsView | user=%s | %s", request.user.id, e)
            return Response({
                "source": "zerodha", "available": 0, "used_margin": 0,
                "total": 0, "token_valid": False, "error": str(e),
            })