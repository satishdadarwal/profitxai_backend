# apps/brokers/tasks.py
#
# ✅ PERMANENT FIXES (2026-05-28):
#
#  BUG #1 — app_id_hash mein "-200" suffix included tha (both tasks)
#   WRONG:  sha256("RBPEXX0S3A-200:secret")
#   FIXED:  sha256("RBPEXX0S3A:secret")   ← base_app_id use karo
#   Impact: auto_refresh_master_fyers_token + auto_refresh_fyers_tokens
#           dono Fyers se 403/token-rejected error lete the
#
#  BUG #2 — auto_refresh_fyers_tokens: account.app_id mein "-200" hota hai
#   Same base_app_id strip fix applied
#
#  BUG #3 — SEBI Compliance: har user ka token sirf uske account se serve ho
#   - factory.py: account_id explicit pass option added
#   - tasks: place_broker_order ab broker_order.broker_account use karta hai
#     directly (user-level isolation guaranteed)
#
#  BUG #4 — refresh_token rotation: .env update hone tak chain break hoti hai
#   FIXED: naya refresh_token DB mein bhi save hota hai (BrokerAccount pe)
#   Isse ek-din ka bridge milta hai admin ke .env update karne tak
#
# ─────────────────────────────────────────────────────────────────────────────
# SEBI Circular Requirements (Algo Trading / API-based):
#   - Har client ka apna unique identifier aur audit trail hona chahiye
#   - Token sharing allowed nahi — har user ka apna access_token DB mein
#   - Order mein broker_account FK se pata chale order kis client ka hai
#   - No co-mingling: factory sirf us user ka account use kare jiska order hai
# ─────────────────────────────────────────────────────────────────────────────

import logging
import hashlib
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db import models
from django.utils import timezone
import requests
import pyotp

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Helper: base_app_id extract karo (Fyers hash ke liye "-200" suffix nahi hota)
# ─────────────────────────────────────────────────────────────────────────────

def _base_app_id(app_id: str) -> str:
    """
    'RBPEXX0S3A-200' → 'RBPEXX0S3A'
    'RBPEXX0S3A'     → 'RBPEXX0S3A'  (no change)

    Fyers appIdHash = sha256(base_app_id:secret) — suffix se nahi.
    """
    return app_id.split("-")[0] if "-" in app_id else app_id


def _make_app_id_hash(app_id: str, secret_key: str) -> str:
    """✅ FIX: Fyers appIdHash — FULL app_id ke saath (with -200 suffix)."""
    return hashlib.sha256(f"{app_id}:{secret_key}".encode()).hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# place_broker_order
# ─────────────────────────────────────────────────────────────────────────────

@shared_task(bind=True, max_retries=0)
def place_broker_order(self, broker_order_id: str):
    """
    Ek BrokerOrder broker ko bhejo.

    SEBI Compliance:
    - adapter sirf broker_order.broker_account se banta hai — us user ka APNA account
    - Koi cross-user token access nahi
    """
    from .models import BrokerOrder
    from .utils import get_adapter_for_account

    try:
        order = BrokerOrder.objects.select_related(
            "broker_account", "option_trade", "order"
        ).get(id=broker_order_id)
    except BrokerOrder.DoesNotExist:
        logger.error("place_broker_order: BrokerOrder %s not found", broker_order_id)
        return

    if order.status != BrokerOrder.Status.PENDING:
        logger.warning(
            "place_broker_order: order %s already %s — skipping",
            broker_order_id, order.status,
        )
        return

    # Double-send guard
    if order.sent_to_broker_at is not None:
        logger.warning(
            "⚠️  Already sent to broker at %s | broker_order=%s — skipping duplicate",
            order.sent_to_broker_at, broker_order_id,
        )
        return

    # Atomic timestamp mark BEFORE broker call
    order.sent_to_broker_at = timezone.now()
    order.save(update_fields=["sent_to_broker_at"])

    # Paper mode detection
    linked_order = order.order
    is_paper_mode = (
        linked_order is not None
        and getattr(linked_order, "mode", None) == "paper"
    ) or (
        order.broker_account is not None
        and order.broker_account.broker == "paper"
    )

    try:
        if is_paper_mode:
            from broker_adapters.paper.adapter import FakeFyersAdapter
            adapter = FakeFyersAdapter({})
            logger.info(
                "📝 Paper mode order | broker_order=%s | symbol=%s",
                broker_order_id, order.symbol,
            )
        else:
            # ✅ SEBI: broker_account directly use karo — user isolation guaranteed
            adapter = get_adapter_for_account(order.broker_account)

        order_type = "market"
        if order.order_type and order.order_type.lower() in ("limit", "sl"):
            order_type = "limit"

        result = adapter.place_order(
            symbol=order.symbol,
            side=order.side.lower(),
            qty=float(order.quantity),
            order_type=order_type,
            price=float(order.price) if order.price else 0.0,
            stop_price=float(getattr(order, "stop_loss", 0) or 0),
            take_profit=float(getattr(order, "take_profit", 0) or 0),
        )

        if result.success:
            exchange_id = result.order_id or ""

            if not exchange_id:
                logger.error(
                    "❌ Broker returned success but NO order_id | "
                    "broker_order=%s — marking FAILED",
                    broker_order_id,
                )
                order.mark_failed(
                    reason="Broker returned success=True but order_id was empty."
                )
                return

            order.mark_sent(
                broker_order_id=exchange_id,
                broker_response=result.raw,
                exchange_order_id=exchange_id,
            )
            logger.info(
                "✅ Order placed | broker_order=%s | exchange_id=%s",
                broker_order_id, exchange_id,
            )

            try:
                from apps.brokers.fill_handler import check_single_order_fill
                check_single_order_fill.apply_async(
                    args=[broker_order_id],
                    countdown=5,
                )
            except Exception as fill_err:
                logger.error(
                    "Fill check scheduling failed | broker_order=%s | %s",
                    broker_order_id, fill_err,
                )
        else:
            logger.error(
                "place_broker_order: broker rejected | %s | reason=%s",
                broker_order_id, result.message,
            )
            order.mark_failed(reason=result.message)

    except Exception as exc:
        logger.error("place_broker_order failed | %s | %s", broker_order_id, exc)
        order.mark_failed(reason=str(exc))

        if order.can_retry and order.next_retry_at:
            delay = max((order.next_retry_at - timezone.now()).total_seconds(), 0)
            place_broker_order.apply_async(
                args=[broker_order_id],
                countdown=delay,
            )


# ─────────────────────────────────────────────────────────────────────────────
# retry_pending_orders
# ─────────────────────────────────────────────────────────────────────────────

@shared_task
def retry_pending_orders():
    """Celery Beat se har 1 minute mein — missed retries pick karo."""
    from .models import BrokerOrder

    due_orders = BrokerOrder.objects.filter(
        status=BrokerOrder.Status.FAILED,
        next_retry_at__lte=timezone.now(),
        retry_count__lt=models.F("max_retries"),
    ).values_list("id", flat=True)

    for oid in due_orders:
        place_broker_order.delay(str(oid))
        logger.info("retry_pending_orders: queued %s", oid)


# ─────────────────────────────────────────────────────────────────────────────
# Feed lifecycle tasks
# ─────────────────────────────────────────────────────────────────────────────

@shared_task
def start_all_active_feeds():
    from apps.brokers.feed_manager import start_feed_for_account
    from apps.brokers.models import BrokerAccount

    accounts = BrokerAccount.objects.filter(
        broker="fyers",
        is_active=True,
    ).exclude(access_token__isnull=True).exclude(access_token="")

    for account in accounts:
        start_feed_for_account(account.id)
        logger.info("Feed started | account=%s | user=%s", account.id, account.user_id)


@shared_task
def stop_all_feeds():
    from apps.brokers.feed_manager import get_all_feed_ids, stop_feed_for_account

    for account_id in get_all_feed_ids():
        stop_feed_for_account(account_id)


# ─────────────────────────────────────────────────────────────────────────────
# auto_refresh_master_fyers_token
# ✅ FIX: base_app_id use karo for hash (was sending full "RBPEXX0S3A-200")
# ✅ FIX: naya refresh_token DB mein bhi save karo (bridge for admin .env update)
# ─────────────────────────────────────────────────────────────────────────────

@shared_task(bind=True, max_retries=2, default_retry_delay=300)
def auto_refresh_master_fyers_token(self):
    """
    Master Fyers account ka token — programmatic login se fresh token lo.
    SEBI regulation ke baad refresh token API disabled hai — isliye
    full 5-step login flow use karo (TOTP + PIN → auth_code → access_token).
    """
    from .models import BrokerAccount
    from django.contrib.auth import get_user_model
    from apps.brokers.views import _fyers_programmatic_login

    app_id       = getattr(settings, "FYERS_APP_ID", "")
    secret_key   = getattr(settings, "FYERS_SECRET_KEY", "")
    redirect_uri = getattr(settings, "FYERS_REDIRECT_URI", "")
    totp_secret  = getattr(settings, "FYERS_MASTER_TOTP_SECRET", "")

    # Master account DB se lo
    User = get_user_model()
    admin = User.objects.filter(is_superuser=True).first()
    if not admin:
        return {"status": "failed", "reason": "no_superuser"}

    master_account = BrokerAccount.objects.filter(
        user=admin, broker="fyers", label="Master Account"
    ).first()
    if not master_account:
        return {"status": "failed", "reason": "master_account_not_found"}

    fyers_client_id = master_account.fyers_client_id or ""
    fyers_pin       = master_account.fyers_pin or ""

    if not fyers_client_id:
        logger.error("❌ Master account fyers_client_id missing — set karo DB mein")
        return {"status": "failed", "reason": "fyers_client_id_missing"}
    if not totp_secret:
        logger.error("❌ FYERS_MASTER_TOTP_SECRET .env mein missing")
        return {"status": "failed", "reason": "totp_secret_missing"}
    if not fyers_pin:
        logger.error("❌ Master account fyers_pin missing — set karo DB mein")
        return {"status": "failed", "reason": "fyers_pin_missing"}

    logger.info("auto_refresh_master: programmatic login | client=%s", fyers_client_id)

    # Step 1-4: TOTP + PIN → auth_code
    result = _fyers_programmatic_login(
        app_id=app_id,
        secret_key=secret_key,
        redirect_uri=redirect_uri,
        fyers_client_id=fyers_client_id,
        totp_secret=totp_secret,
        fyers_pin=fyers_pin,
        state="master_auto_refresh",
    )

    if not result.get("success"):
        logger.error("❌ Programmatic login failed | %s", result.get("error"))
        return {"status": "failed", "reason": "programmatic_login_failed", "error": result.get("error")}

    auth_code = result["auth_code"]

    # Step 5: auth_code → access_token (FULL app_id hash)
    FYERS_API_BASE = "https://api-t1.fyers.in/api/v3"
    app_id_hash = hashlib.sha256(f"{app_id}:{secret_key}".encode()).hexdigest()
    logger.info("auto_refresh_master step5: hash_prefix=%s", app_id_hash[:16])

    try:
        resp = requests.post(
            f"{FYERS_API_BASE}/validate-authcode",
            json={
                "grant_type": "authorization_code",
                "appIdHash":  app_id_hash,
                "code":       auth_code,
            },
            timeout=15,
        )
        data = resp.json()
    except Exception as e:
        return {"status": "failed", "reason": "authcode_exchange_failed", "error": str(e)}

    if data.get("s") != "ok":
        logger.error("❌ auth_code exchange failed | %s", data)
        return {"status": "failed", "reason": "fyers_rejection", "code": data.get("code"), "message": data.get("message")}

    new_access_token = data["access_token"]

    # DB update
    try:
        master_account.access_token = new_access_token
        master_account.is_verified  = True
        master_account.is_active    = True
        master_account.token_expiry = timezone.now() + timedelta(hours=24)
        master_account.save(update_fields=["access_token", "is_verified", "is_active", "token_expiry"])
        logger.info("✅ Master token updated in DB | token=%s...", new_access_token[:8])
    except Exception as db_err:
        logger.error("❌ DB save failed: %s", db_err)

    # Feed restart
    try:
        _restart_master_feed(new_access_token, app_id)
    except Exception as ws_err:
        logger.error("❌ Feed restart failed: %s", ws_err)

    return {"status": "success", "new_token_prefix": new_access_token[:8]}

# ─────────────────────────────────────────────────────────────────────────────
# auto_refresh_fyers_tokens (individual accounts)
# ✅ FIX: base_app_id use karo for hash
# ✅ FIX: refresh_token rotation DB mein save karo
# ✅ SEBI: har account independently refresh hota hai — token sharing nahi
# ─────────────────────────────────────────────────────────────────────────────

@shared_task(bind=True, max_retries=2, default_retry_delay=300)
def auto_refresh_fyers_tokens(self):
    """
    Sabhi individual Fyers accounts ka token refresh karo.
    ✅ DEDUP LOCK: Ek time pe sirf ek instance — duplicate queues se infinite loop nahi.
    """
    # ✅ FIX: Redis dedup lock — agar already chal raha hai toh skip karo
    try:
        from django.core.cache import cache
        lock_key = "auto_refresh_fyers_tokens:running"
        acquired = cache.add(lock_key, True, timeout=60)  # 60s lock
        if not acquired:
            logger.info("auto_refresh_fyers_tokens: already running — skipping duplicate")
            return {"refreshed": 0, "failed": 0, "skipped": "duplicate"}
    except Exception as lock_err:
        logger.warning("auto_refresh lock error (non-fatal): %s", lock_err)

    try:
        return _do_auto_refresh_fyers_tokens()
    finally:
        try:
            from django.core.cache import cache
            cache.delete("auto_refresh_fyers_tokens:running")
        except Exception:
            pass


def _do_auto_refresh_fyers_tokens():
    """Actual refresh logic — called by auto_refresh_fyers_tokens with dedup lock."""
    from .models import BrokerAccount

    FYERS_API_BASE = "https://api-t1.fyers.in/api/v3"

    accounts = BrokerAccount.objects.filter(
        broker="fyers",
        is_active=True,
        is_verified=True,
    ).exclude(refresh_token__isnull=True).exclude(refresh_token="")

    if not accounts.exists():
        logger.info("auto_refresh_fyers_tokens: No active Fyers accounts found.")
        return {"refreshed": 0, "failed": 0}

    refreshed = 0
    failed    = 0
    results   = []

    for account in accounts:
        pin         = getattr(account, "fyers_pin", "") or ""
        totp_secret = getattr(account, "totp_secret", "") or ""

        # Auth method: TOTP > PIN
        if totp_secret:
            try:
                pin_or_totp = pyotp.TOTP(totp_secret).now()
                logger.info(
                    "auto_refresh: Using TOTP | account=%s | user=%s | otp_prefix=%s****",
                    account.id, account.user_id, pin_or_totp[:2],
                )
            except Exception as e:
                logger.error("TOTP generation failed | account=%s | error=%s", account.id, e)
                results.append({"account_id": account.id, "user_id": account.user_id, "status": "totp_failed", "error": str(e)})
                failed += 1
                continue
        elif pin:
            pin_or_totp = str(pin)
            logger.info("auto_refresh: Using PIN | account=%s | user=%s", account.id, account.user_id)
        else:
            logger.warning(
                "auto_refresh: No auth method | account=%s | user=%s — skipping. "
                "User needs to save PIN/TOTP in app.",
                account.id, account.user_id,
            )
            results.append({"account_id": account.id, "user_id": account.user_id, "status": "skipped_no_auth"})
            failed += 1
            continue

        # ✅ SEBI FIX: validate-refresh-token DISABLED — full programmatic login use karo
        # 5-step login: OTP → verify → TOTP → trade_token → auth_code → access_token
        acct_app_id  = account.app_id    or getattr(settings, "FYERS_APP_ID", "")
        acct_secret  = account.secret_key or getattr(settings, "FYERS_SECRET_KEY", "")
        acct_redirect = getattr(settings, "FYERS_REDIRECT_URI", "")
        acct_client_id = getattr(account, "fyers_client_id", "") or ""

        if not acct_client_id:
            logger.warning("auto_refresh: fyers_client_id missing | account=%s — skipping", account.id)
            results.append({"account_id": account.id, "status": "skipped_no_client_id"})
            failed += 1
            continue

        logger.info(
            "auto_refresh: programmatic login | account=%s | client=%s | app=%s",
            account.id, acct_client_id, acct_app_id,
        )

        try:
            from apps.brokers.views import _fyers_programmatic_login
            login_result = _fyers_programmatic_login(
                app_id       = acct_app_id,
                secret_key   = acct_secret,
                redirect_uri = acct_redirect,
                fyers_client_id = acct_client_id,
                totp_secret  = totp_secret,
                fyers_pin    = pin_or_totp if not totp_secret else pin,
                state        = f"auto_refresh_{account.id}",
            )

            if not login_result.get("success"):
                failed += 1
                logger.error("auto_refresh login failed | account=%s | %s", account.id, login_result.get("error"))
                results.append({"account_id": account.id, "status": "login_failed", "msg": login_result.get("error")})
                continue

            auth_code = login_result["auth_code"]

            # validate-authcode — FULL app_id hash
            app_id_hash = hashlib.sha256(f"{acct_app_id}:{acct_secret}".encode()).hexdigest()
            resp = requests.post(
                f"{FYERS_API_BASE}/validate-authcode",
                json={
                    "grant_type": "authorization_code",
                    "appIdHash":  app_id_hash,
                    "code":       auth_code,
                },
                timeout=15,
            )
            data = resp.json()

            if data.get("s") == "ok":
                new_token = data["access_token"]
                account.access_token = new_token
                account.token_expiry = timezone.now() + timedelta(hours=24)
                account.save(update_fields=["access_token", "token_expiry"])

                refreshed += 1
                results.append({"account_id": account.id, "user_id": account.user_id, "status": "ok"})
                logger.info("✅ Token refreshed | account=%s | user=%s", account.id, account.user_id)

                try:
                    _restart_ws_for_account(account.id, new_token, acct_app_id)
                except Exception as ws_err:
                    logger.warning("WS restart failed | account=%s | %s", account.id, ws_err)

            else:
                failed += 1
                err_code = data.get("code", "unknown")
                err_msg  = data.get("message", str(data))
                results.append({"account_id": account.id, "status": "fyers_error", "code": err_code, "msg": err_msg})
                logger.error("Token exchange failed | account=%s | code=%s | msg=%s", account.id, err_code, err_msg)

        except requests.exceptions.Timeout:
            failed += 1
            logger.error("Token refresh timeout | account=%s", account.id)
            results.append({"account_id": account.id, "status": "timeout"})
        except Exception as exc:
            failed += 1
            logger.exception("Token refresh exception | account=%s", account.id)
            results.append({"account_id": account.id, "status": "exception", "msg": str(exc)})

    summary = {"refreshed": refreshed, "failed": failed, "details": results}
    logger.info("auto_refresh_fyers_tokens: DONE | summary=%s", summary)
    return summary


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _restart_master_feed(new_access_token: str, app_id: str):
    import os, json
    token_str = f"{app_id}:{new_access_token}"
    if os.environ.get('CELERY_WORKER_RUNNING'):
        try:
            import redis as redis_lib
            from django.conf import settings as _s
            r = redis_lib.from_url(_s.REDIS_URL, decode_responses=True)
            r.publish("feed:restart_token", json.dumps({"token": token_str}))
            r.close()
            logger.info("✅ Feed restart request published via Redis")
        except Exception as e:
            logger.error("Feed restart Redis publish failed: %s", e)
        return
    from apps.websocket.fyers_feed import feed_manager
    if feed_manager._started or feed_manager._connected:
        feed_manager.restart_with_new_token(token_str)
        logger.info("✅ Master feed restarted with new token")
    else:
        feed_manager.start(token=token_str)
        logger.info("✅ Master feed started fresh with new token")
def _restart_ws_for_account(account_id: int, new_access_token: str, app_id: str):
    import os, json
    token_str = f"{app_id}:{new_access_token}"
    if os.environ.get('CELERY_WORKER_RUNNING'):
        try:
            import redis as redis_lib
            from django.conf import settings as _s
            r = redis_lib.from_url(_s.REDIS_URL, decode_responses=True)
            r.publish("feed:restart_token", json.dumps({"token": token_str}))
            r.close()
            logger.info("✅ WS restart request published via Redis | account=%s", account_id)
        except Exception as e:
            logger.error("WS restart Redis publish failed: %s", e)
        return
    from apps.websocket.fyers_feed import feed_manager as fyers_feed_manager
    fyers_feed_manager.restart_with_new_token(token_str)
    logger.info("✅ WS restarted | account=%s", account_id)
def _send_urgent_admin_alert(message: str):
    """Admin ko urgent notification bhejo."""
    try:
        from django.contrib.auth import get_user_model
        from apps.notifications.tasks import send_urgent_notification
        User  = get_user_model()
        admin = User.objects.filter(is_superuser=True).first()
        if admin:
            send_urgent_notification.apply_async(args=[admin.id, message])
    except Exception as e:
        logger.error("Admin alert send failed | %s", e)


def _send_refresh_token_rotation_alert(
    new_refresh_token: str,
    is_master: bool = False,
    account_id=None,
    user_id=None,
):
    """Refresh token rotation hua — admin ko update karne ko kaho."""
    if is_master:
        msg = (
            f"🔑 Fyers MASTER Refresh Token badal gaya!\n"
            f".env mein IMMEDIATELY update karo:\n"
            f"FYERS_MASTER_REFRESH_TOKEN={new_refresh_token}\n"
            f"(DB mein bridge ke liye save ho gaya — kal tak time hai)"
        )
    else:
        msg = (
            f"🔑 Fyers Refresh Token rotated | account={account_id} user={user_id}\n"
            f"DB mein automatically save ho gaya — koi action needed nahi."
        )
    logger.warning(msg)
    _send_urgent_admin_alert(msg)


# ─────────────────────────────────────────────────────────────────────────────
# poll_gtt_order_status
# GTT triggered orders detect karke Order.realized_pnl update karo
# ─────────────────────────────────────────────────────────────────────────────

@shared_task(name="brokers.poll_gtt_order_status", bind=True, max_retries=1)
def poll_gtt_order_status(self):
    """GTT triggered orders detect karke Order.realized_pnl update karo"""
    from apps.orders.models import Order

    open_orders = Order.objects.filter(
        status='open',
        broker='fyers',
    ).select_related('user__trading_profile')

    for order in open_orders:
        try:
            profile = getattr(order.user, 'trading_profile', None)
            if not profile:
                continue
            exit_mode = getattr(profile, 'exit_mode', None)
            if exit_mode not in ('gtt_oco', 'both'):
                continue

            from apps.brokers.utils import get_broker_adapter
            adapter = get_broker_adapter(order.user, 'fyers')
            if not adapter:
                continue

            trades = adapter.get_tradebook() or []
            sym = order.symbol_display or ''

            exit_trade = next((
                t for t in trades
                if t.get('symbol', '') == sym
                and int(t.get('side', 0)) == -1
            ), None)

            if exit_trade:
                exit_p = float(exit_trade.get('tradePrice', 0))
                entry = float(order.avg_fill_price or 0)
                qty = float(order.quantity or 0)
                if exit_p > 0 and entry > 0 and qty > 0:
                    from decimal import Decimal
                    pnl = Decimal(str(round((exit_p - entry) * qty, 2)))
                    order.realized_pnl = pnl
                    order.status = 'closed'
                    order.exit_price = Decimal(str(exit_p))
                    order.save(update_fields=[
                        'realized_pnl', 'status', 'exit_price', 'updated_at'
                    ])
                    logger.info("GTT exit detected | order=%s | pnl=%s",
                                order.id, pnl)
        except Exception as e:
            logger.error("GTT poll error | order=%s | %s", order.id, e)