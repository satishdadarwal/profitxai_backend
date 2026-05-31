# apps/brokers/apps.py
#
# ✅ Auto-sync: Server start hone ke baad .env se master BrokerAccount update hota hai
#    — FYERS_MASTER_TOTP_SECRET, FYERS_APP_ID, FYERS_SECRET_KEY, FYERS_REDIRECT_URI
#    — Django warning fix: DB access background thread mein hota hai

import logging
import threading

from django.apps import AppConfig

logger = logging.getLogger(__name__)


class BrokersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.brokers"

    def ready(self):
        """
        Background thread mein sync karta hai — Django ka
        'Accessing DB during app init' warning avoid hota hai.
        """
        t = threading.Thread(target=self._sync_master_account, daemon=True)
        t.start()

    def _sync_master_account(self):
        import os, time
        # Thoda wait karo — Django ORM fully ready ho jaye
        time.sleep(2)

        totp_secret  = os.getenv("FYERS_MASTER_TOTP_SECRET", "").strip()
        app_id       = os.getenv("FYERS_APP_ID", "").strip()
        secret_key   = os.getenv("FYERS_SECRET_KEY", "").strip()
        redirect_uri = os.getenv("FYERS_REDIRECT_URI", "").strip()

        if not any([totp_secret, app_id, secret_key, redirect_uri]):
            return

        try:
            from django.contrib.auth import get_user_model
            from apps.brokers.models import BrokerAccount

            User = get_user_model()
            superuser = User.objects.filter(is_superuser=True).order_by("date_joined").first()
            if not superuser:
                return

            account = (
                BrokerAccount.objects
                .filter(user=superuser, broker="fyers", label="fyers masters")
                .first()
            )
            if not account:
                logger.debug("BrokersConfig: No 'fyers masters' account found — skipping sync")
                return

            update_fields = []

            if totp_secret and account.totp_secret != totp_secret:
                account.totp_secret = totp_secret
                update_fields.append("totp_secret")

            if app_id and account.app_id != app_id:
                account.app_id = app_id
                update_fields.append("app_id")

            if secret_key and account.secret_key != secret_key:
                account.secret_key = secret_key
                update_fields.append("secret_key")

            if redirect_uri and account.redirect_uri != redirect_uri:
                account.redirect_uri = redirect_uri
                update_fields.append("redirect_uri")

            if update_fields:
                account.save(update_fields=update_fields)
                logger.info(
                    "✅ Master account synced from .env | account=%s | fields=%s",
                    account.id, update_fields,
                )
            else:
                logger.debug("Master account already up-to-date | account=%s", account.id)

        except Exception as exc:
            logger.warning("BrokersConfig sync skipped — %s", exc)