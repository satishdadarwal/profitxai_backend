# apps/brokers/tasks.py

 

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


@shared_task(bind=True, max_retries=0)
def place_broker_order(self, broker_order_id: str):
    """
    Ek BrokerOrder broker ko bhejo.
    Called by: OptionTrade signal ya view.

    ✅ FIX: adapter.place_order(order) was wrong — adapters expect positional args:
       adapter.place_order(symbol, side, qty, order_type, price, **kwargs)
       NOT a BrokerOrder ORM object.
    """
    from .models import BrokerOrder
    from .utils import get_adapter_for_account

    try:
        order = BrokerOrder.objects.select_related(
            "broker_account", "option_trade"
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

    # ✅ FIX #2: sent_to_broker_at check — double send rokta hai
    if order.sent_to_broker_at is not None:
        logger.warning(
            "⚠️  Already sent to broker at %s | broker_order=%s — skipping to prevent duplicate",
            order.sent_to_broker_at, broker_order_id,
        )
        return

    # Broker call se PEHLE timestamp mark karo (atomic guard)
    order.sent_to_broker_at = timezone.now()
    order.save(update_fields=["sent_to_broker_at"])

    # ── ✅ FIX (BLOCKER #3): Paper mode detection ─────────────────────────────
    # Paper orders ke liye FakeFyersAdapter use karo — real Fyers API mat maaro.
    # Isse pura lifecycle test hota hai: place → OPEN → fill → PnL → wallet
    # Real money risk zero, aur agar symbol format / lot size galat hai toh
    # yahan WARN log aayega — live pe jaane se pehle fix kar sako.
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
            # Paper mode: FakeFyersAdapter use karo (no real API call)
            from broker_adapters.paper.adapter import FakeFyersAdapter
            adapter = FakeFyersAdapter({})
            logger.info(
                "📝 Paper mode order | broker_order=%s | symbol=%s",
                broker_order_id, order.symbol,
            )
        else:
            adapter = get_adapter_for_account(order.broker_account)

        order_type = "market"
        if order.order_type and order.order_type.lower() in ("limit", "sl"):
            order_type = "limit"

        result = adapter.place_order(
            symbol=order.symbol,
            side=order.direction.lower(),
            qty=float(order.quantity),
            order_type=order_type,
            price=float(order.price) if order.price else 0.0,
            stop_price=float(order.stop_loss) if order.stop_loss else 0.0,
            take_profit=float(order.take_profit) if order.take_profit else 0.0,
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
                    reason="Broker returned success=True but order_id was empty. "
                           "Possible silent rejection — check broker dashboard."
                )
                return

            order.mark_sent(
                exchange_order_id=exchange_id,
                broker_response=result.raw,
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


@shared_task
def retry_pending_orders():
    """
    Celery Beat se har 1 minute mein chalaao — missed retries pick karo.
    """
    from .models import BrokerOrder

    due_orders = BrokerOrder.objects.filter(
        status=BrokerOrder.Status.FAILED,
        next_retry_at__lte=timezone.now(),
        retry_count__lt=models.F("max_retries"),
    ).values_list("id", flat=True)

    for oid in due_orders:
        place_broker_order.delay(str(oid))
        logger.info("retry_pending_orders: queued %s", oid)


@shared_task
def start_all_active_feeds():
    from apps.brokers.feed_manager import start_feed_for_account
    from apps.brokers.models import BrokerAccount

    accounts = BrokerAccount.objects.filter(
        broker="fyers",
        is_active=True,
        # ✅ is_verified=True HATA DIYA — fyers_feed.py jaisa
    ).exclude(access_token__isnull=True).exclude(access_token="")

    for account in accounts:
        start_feed_for_account(account.id)
        logger.info("Feed started | account=%s | user=%s", account.id, account.user_id)


@shared_task
def stop_all_feeds():
    from apps.brokers.feed_manager import get_all_feed_ids, stop_feed_for_account

    for account_id in get_all_feed_ids():
        stop_feed_for_account(account_id)


@shared_task(bind=True, max_retries=2, default_retry_delay=300)
def auto_refresh_master_fyers_token(self):
    """
    Master Fyers account ka token refresh karo.
    
    Ye account sabhi users ko feed provide karta hai.
    Individual user accounts sirf trading ke liye hain.
    
    Flow:
    1. TOTP generate karo (.env se secret)
    2. Fyers API se token refresh karo
    3. WebSocket feed restart karo naye token ke saath
    4. Sabhi users ko uninterrupted feed milta rahega
    """
    FYERS_API_BASE = "https://api-t1.fyers.in/api/v3"
    
    # .env se master account credentials
    app_id = getattr(settings, "FYERS_APP_ID", "")
    secret_key = getattr(settings, "FYERS_SECRET_KEY", "")
    refresh_token = getattr(settings, "FYERS_MASTER_REFRESH_TOKEN", "")
    totp_secret = getattr(settings, "FYERS_MASTER_TOTP_SECRET", "")
    
    # Validation — saare credentials chahiye
    if not all([app_id, secret_key, refresh_token, totp_secret]):
        missing = []
        if not app_id:
            missing.append("FYERS_APP_ID")
        if not secret_key:
            missing.append("FYERS_SECRET_KEY")
        if not refresh_token:
            missing.append("FYERS_MASTER_REFRESH_TOKEN")
        if not totp_secret:
            missing.append("FYERS_MASTER_TOTP_SECRET")
        
        logger.error(
            "❌ Master Fyers credentials incomplete in .env | missing: %s",
            ", ".join(missing)
        )
        return {
            "status": "failed",
            "reason": "incomplete_credentials",
            "missing": missing,
        }
    
    # Step 1: TOTP generate karo
    try:
        totp = pyotp.TOTP(totp_secret)
        current_otp = totp.now()
        
        logger.info(
            "✅ TOTP generated for master account | otp_prefix=%s",
            current_otp[:2] + "****"
        )
    except Exception as totp_err:
        logger.error(
            "❌ TOTP generation failed for master account | error=%s",
            totp_err
        )
        return {
            "status": "failed",
            "reason": "totp_generation_failed",
            "error": str(totp_err),
        }
    
    # Step 2: Token refresh API call
    app_id_hash = hashlib.sha256(
        f"{app_id}:{secret_key}".encode()
    ).hexdigest()
    
    try:
        resp = requests.post(
            f"{FYERS_API_BASE}/validate-refresh-token",
            json={
                "grant_type": "refresh_token",
                "appIdHash": app_id_hash,
                "refresh_token": refresh_token,
                "pin": current_otp,  # ✅ TOTP as PIN
            },
            timeout=15,
        )
        data = resp.json()
        
        if data.get("s") == "ok":
            new_access_token = data["access_token"]
            new_refresh_token = data.get("refresh_token", refresh_token)
            
            logger.info(
                "✅ Master Fyers token refreshed successfully | "
                "token_prefix=%s | refresh_token_changed=%s",
                new_access_token[:8] + "...",
                new_refresh_token != refresh_token,
            )
            
            # Step 3: WebSocket feed restart karo naye token ke saath
            try:
                _restart_master_feed(new_access_token, app_id)
            except Exception as ws_err:
                logger.error(
                    "❌ Master feed restart failed | error=%s",
                    ws_err
                )
                # Token toh refresh ho gaya, lekin feed restart nahi hua
                # Next cycle mein auto-retry hoga
                # ✅ FIX #1: Partial failure — admin ko urgent alert bhejo
                try:
                    from django.contrib.auth import get_user_model
                    from apps.notifications.tasks import send_urgent_notification
                    User = get_user_model()
                    admin = User.objects.filter(is_superuser=True).first()
                    if admin:
                        send_urgent_notification.apply_async(
                            args=[
                                admin.id,
                                f"⚠️ Fyers master token refresh hua lekin WebSocket feed restart FAIL hua.\n"
                                f"Error: {ws_err}\n"
                                f"Action: Manually restart feed ya celery worker restart karo.",
                            ]
                        )
                except Exception as notif_err:
                    logger.error("Partial-failure alert send karna fail hua | %s", notif_err)

                return {
                    "status": "partial",
                    "reason": "feed_restart_failed",
                    "new_token_prefix": new_access_token[:8],
                    "error": str(ws_err),
                }
            
            # ✅ FIX #2: Refresh token rotation — sirf log nahi, admin ko alert bhi bhejo
            # Fyers kabhi kabhi naya refresh_token return karta hai.
            # Agar .env update nahi kiya toh kal ka refresh fail hoga — chain break.
            if new_refresh_token != refresh_token:
                logger.warning(
                    "⚠️  New refresh_token received for master account.\n"
                    "Action required: Update .env file manually:\n"
                    "FYERS_MASTER_REFRESH_TOKEN=%s\n"
                    "Or use AWS Secrets Manager / Vault in production.",
                    new_refresh_token
                )
                try:
                    from django.contrib.auth import get_user_model
                    from apps.notifications.tasks import send_urgent_notification
                    User = get_user_model()
                    admin = User.objects.filter(is_superuser=True).first()
                    if admin:
                        send_urgent_notification.apply_async(
                            args=[
                                admin.id,
                                f"🔑 Fyers Master Refresh Token badal gaya!\n"
                                f".env mein IMMEDIATELY update karo:\n"
                                f"FYERS_MASTER_REFRESH_TOKEN={new_refresh_token}\n"
                                f"Agar update nahi kiya toh kal 8:30 AM ka token refresh FAIL hoga.",
                            ]
                        )
                except Exception as notif_err:
                    logger.error(
                        "Refresh token rotation alert send karna fail hua | %s", notif_err
                    )
            
            return {
                "status": "success",
                "new_token_prefix": new_access_token[:8],
                "refresh_token_changed": new_refresh_token != refresh_token,
                "new_refresh_token": new_refresh_token if new_refresh_token != refresh_token else None,
            }
        
        else:
            # Fyers API ne reject kiya
            error_msg = data.get("message", str(data))
            error_code = data.get("code", "unknown")
            
            logger.error(
                "❌ Fyers API rejected master token refresh | "
                "code=%s | message=%s",
                error_code, error_msg
            )
            
            return {
                "status": "failed",
                "reason": "fyers_rejection",
                "code": error_code,
                "message": error_msg,
            }
    
    except requests.exceptions.Timeout:
        logger.error("❌ Fyers API timeout during master token refresh")
        return {"status": "failed", "reason": "timeout"}
    
    except requests.exceptions.RequestException as req_err:
        logger.error(
            "❌ Fyers API request failed | error=%s",
            req_err
        )
        return {
            "status": "failed",
            "reason": "request_exception",
            "error": str(req_err),
        }
    
    except Exception as exc:
        logger.exception("❌ Unexpected error in master token refresh")
        return {
            "status": "failed",
            "reason": "exception",
            "error": str(exc),
        }


def _restart_master_feed(new_access_token: str, app_id: str):
    """
    Master feed ko naye token ke saath restart karo.
    
    Existing subscriptions maintain hongi — users ko
    seamless feed milta rahega (reconnect lag hoga sirf).
    """
    try:
        from apps.websocket.fyers_feed import feed_manager
        
        new_token_str = f"{app_id}:{new_access_token}"
        
        # Feed already running hai toh restart, nahi toh fresh start
        if feed_manager._started or feed_manager._connected:
            feed_manager.restart_with_new_token(new_token_str)
            logger.info("✅ Master feed restarted with new token")
        else:
            feed_manager.start(token=new_token_str)
            logger.info("✅ Master feed started fresh with new token")
    
    except Exception as e:
        logger.error(
            "❌ _restart_master_feed failed | error=%s",
            e,
            exc_info=True
        )
        raise


# ════════════════════════════════════════════════════════════════════
#  ✅ INDIVIDUAL ACCOUNTS: auto_refresh_fyers_tokens
#
#  Har user ka apna Fyers account refresh karo.
#  Use case: Jab har user apna feed + trading dono khud manage karta hai.
#
#  Multi-user support:
#  - Har active+verified Fyers account process hota hai
#  - Ek account fail hone se doosre affected nahi hote
#  - PIN/TOTP na ho toh skip + warn
#
#  WS restart flow:
#  1. Token refresh Fyers API se (refresh_token + PIN/TOTP)
#  2. account.access_token DB mein update
#  3. FyersFeedManager.restart_with_new_token() call
# ════════════════════════════════════════════════════════════════════
@shared_task(bind=True, max_retries=2, default_retry_delay=300)
def auto_refresh_fyers_tokens(self):
    """
    Sabhi individual Fyers accounts ka token refresh karo.
    
    Ye tab use karo jab:
    - Har user ka apna Fyers account hai
    - Trading + feed dono individual accounts se
    - PIN/TOTP DB mein encrypted stored hai
    """
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
    failed = 0
    results = []

    for account in accounts:

        pin = getattr(account, "fyers_pin", "") or ""
        totp_secret = getattr(account, "totp_secret", "") or ""

        # TOTP preference: agar TOTP secret hai toh use karo, warna PIN
        if totp_secret:
            try:
                totp = pyotp.TOTP(totp_secret)
                pin_or_totp = totp.now()

                logger.info(
                    "auto_refresh_fyers_tokens: Using TOTP | "
                    "account=%s | user=%s | otp_prefix=%s",
                    account.id,
                    account.user_id,
                    pin_or_totp[:2] + "****"
                )

            except Exception as totp_err:
                logger.error(
                    "auto_refresh_fyers_tokens: TOTP generation failed | "
                    "account=%s | error=%s",
                    account.id,
                    totp_err,
                )

                results.append({
                    "account_id": account.id,
                    "user_id": account.user_id,
                    "status": "totp_generation_failed",
                    "error": str(totp_err),
                })

                failed += 1
                continue

        elif pin:
            pin_or_totp = str(pin)

            logger.info(
                "auto_refresh_fyers_tokens: Using PIN | "
                "account=%s | user=%s",
                account.id,
                account.user_id,
            )

        else:
            logger.warning(
                "auto_refresh_fyers_tokens: No auth method | "
                "account=%s | user=%s — skipping. "
                "User needs to save PIN/TOTP in app.",
                account.id,
                account.user_id,
            )

            results.append({
                "account_id": account.id,
                "user_id": account.user_id,
                "status": "skipped_no_auth",
            })

            failed += 1
            continue

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
                    "pin": pin_or_totp,  # ✅ TOTP ya PIN dono work karenge
                },
                timeout=15,
            )

            data = resp.json()

            if data.get("s") == "ok":
                new_token = data["access_token"]

                account.access_token = new_token

                if data.get("refresh_token"):
                    account.refresh_token = data["refresh_token"]

                # ✅ FIX #3: token_expiry bhi update karo — Fyers token 1 din mein expire hota hai
                account.token_expiry = timezone.now() + timedelta(hours=24)

                account.save(
                    update_fields=[
                        "access_token",
                        "refresh_token",
                        "token_expiry",
                    ]
                )

                refreshed += 1

                results.append({
                    "account_id": account.id,
                    "user_id": account.user_id,
                    "status": "ok",
                })

                logger.info(
                    "✅ Token refreshed | account=%s | user=%s",
                    account.id,
                    account.user_id,
                )

                # ✅ CRITICAL: Token update ke baad WS bhi restart karo
                try:
                    _restart_ws_for_account(
                        account.id,
                        new_token,
                        account.app_id,
                    )

                except Exception as ws_err:
                    logger.warning(
                        "WS restart failed | account=%s | error=%s — "
                        "will retry on next tick",
                        account.id,
                        ws_err,
                    )

            else:
                failed += 1

                err_msg = data.get("message", str(data))
                err_code = data.get("code", "unknown")

                results.append({
                    "account_id": account.id,
                    "user_id": account.user_id,
                    "status": "fyers_error",
                    "code": err_code,
                    "msg": err_msg,
                })

                logger.error(
                    "Token refresh failed | account=%s | code=%s | msg=%s",
                    account.id,
                    err_code,
                    err_msg,
                )

        except requests.exceptions.Timeout:
            failed += 1

            logger.error(
                "Token refresh timeout | account=%s",
                account.id,
            )

            results.append({
                "account_id": account.id,
                "status": "timeout",
            })

        except Exception as exc:
            failed += 1

            logger.exception(
                "Token refresh exception | account=%s",
                account.id,
            )

            results.append({
                "account_id": account.id,
                "status": "exception",
                "msg": str(exc),
            })

    summary = {
        "refreshed": refreshed,
        "failed": failed,
        "details": results,
    }

    logger.info("auto_refresh_fyers_tokens: DONE | summary=%s", summary)

    return summary


def _restart_ws_for_account(
    account_id: int,
    new_access_token: str,
    app_id: str,
):
    """
    Individual account ke liye feed restart karo.
    
    Note: Agar centralized master account hai toh ye function
    use nahi hoga — sirf _restart_master_feed chalega.
    """
    try:
        from apps.websocket.fyers_feed import (
            feed_manager as fyers_feed_manager
        )

        new_token_str = f"{app_id}:{new_access_token}"

        fyers_feed_manager.restart_with_new_token(new_token_str)

        logger.info(
            "✅ WS restarted | account=%s",
            account_id,
        )

    except Exception as e:
        logger.error(
            "_restart_ws_for_account failed | account=%s | error=%s",
            account_id,
            e,
        )
        raise