# broker_adapters/factory.py
#
# ✅ PERMANENT FIXES + SEBI COMPLIANCE (2026-05-28):
#
#  FIX #1 — get_adapter_for_account() method added
#   SEBI requires: order ka adapter sirf us user ke broker_account se bane
#   jo order mein directly linked hai — kisi aur user ka account use nahi hona chahiye.
#   place_broker_order task ab directly broker_account pass karta hai.
#
#  FIX #2 — User-based lookup mein explicit account_id support
#   get_adapter(user, account_id=X) se specific account target karo
#
#  FIX #3 — Credentials validation: app_id blank hone pe settings fallback
#
# SEBI Circular on Algo Trading (2022):
#   - Har order uniquely ek client se traced hona chahiye
#   - Broker API credentials per-client honi chahiye (no shared tokens for orders)
#   - Factory: broker_account FK directly follow karo, user se guess nahi

from __future__ import annotations
import logging

logger = logging.getLogger(__name__)


class BrokerAdapterFactory:
    """
    Central broker adapter factory.

    Usage patterns:
        # 1. BrokerAccount object se (RECOMMENDED — SEBI compliant)
        adapter = BrokerAdapterFactory.get_adapter_for_account(broker_account)

        # 2. User object se (views mein — pehla active account)
        adapter = BrokerAdapterFactory.get_adapter(request.user)

        # 3. User + specific account_id se (multi-account users)
        adapter = BrokerAdapterFactory.get_adapter(request.user, account_id=53)

        # 4. Broker name + credentials dict se (legacy / testing)
        adapter = BrokerAdapterFactory.get_adapter("fyers", {"app_id": ..., "access_token": ...})
    """

    @staticmethod
    def get_adapter_for_account(broker_account):
        """
        ✅ SEBI COMPLIANT — BrokerAccount ORM object se directly adapter banao.

        Ye method place_broker_order task use karta hai — order.broker_account
        directly pass hota hai. Koi user-level lookup nahi, cross-account risk zero.

        Args:
            broker_account: apps.brokers.models.BrokerAccount instance

        Returns:
            BaseBrokerAdapter subclass

        Raises:
            ValueError: agar credentials missing ya broker unsupported
        """
        from broker_adapters.fyers.adapter import FyersAdapter
        from broker_adapters.delta.adapter import DeltaAdapter
        from django.conf import settings

        broker = broker_account.broker.lower()

        if broker == "fyers":
            app_id = broker_account.app_id or getattr(settings, "FYERS_APP_ID", "")
            token  = broker_account.access_token or ""

            if not app_id:
                raise ValueError(
                    f"BrokerAccount {broker_account.id}: app_id missing. "
                    "FYERS_APP_ID .env mein set karo."
                )
            if not token:
                raise ValueError(
                    f"BrokerAccount {broker_account.id} (user={broker_account.user_id}): "
                    "access_token missing. User ko dobara Fyers login karna hoga."
                )

            adapter = FyersAdapter({"app_id": app_id, "access_token": token})
            adapter.broker_account = broker_account
            adapter.broker_name    = "fyers"

            logger.info(
                "BrokerAdapterFactory.get_adapter_for_account: Fyers | "
                "account=%s | user=%s",
                broker_account.id, broker_account.user_id,
            )
            return adapter

        if broker == "delta":
            api_key    = getattr(broker_account, "api_key", "") or ""
            api_secret = getattr(broker_account, "api_secret", "") or ""

            if not api_key or not api_secret:
                raise ValueError(
                    f"BrokerAccount {broker_account.id}: Delta api_key/api_secret missing."
                )

            adapter = DeltaAdapter({"api_key": api_key, "api_secret": api_secret})
            adapter.broker_account = broker_account
            adapter.broker_name    = "delta"

            logger.info(
                "BrokerAdapterFactory.get_adapter_for_account: Delta | "
                "account=%s | user=%s",
                broker_account.id, broker_account.user_id,
            )
            return adapter

        if broker == "dhan":
            client_id    = getattr(broker_account, "dhan_client_id", "") or ""
            access_token = getattr(broker_account, "dhan_access_token", "") or ""

            if not client_id or not access_token:
                raise ValueError(
                    f"BrokerAccount {broker_account.id}: Dhan dhan_client_id/dhan_access_token missing."
                )

            from broker_adapters.dhan.adapter import DhanAdapter
            adapter = DhanAdapter({
                "dhan_client_id":    client_id,
                "dhan_access_token": access_token,
            })
            adapter.broker_account = broker_account
            adapter.broker_name    = "dhan"

            logger.info(
                "BrokerAdapterFactory.get_adapter_for_account: Dhan | "
                "account=%s | user=%s",
                broker_account.id, broker_account.user_id,
            )
            return adapter

        if broker == "zerodha":
            api_key      = getattr(broker_account, "api_key", "") or ""
            access_token = getattr(broker_account, "access_token", "") or ""

            if not api_key or not access_token:
                raise ValueError(
                    f"BrokerAccount {broker_account.id}: Zerodha api_key/access_token missing."
                )

            from broker_adapters.zerodha.adapter import ZerodhaAdapter
            adapter = ZerodhaAdapter({
                "api_key":      api_key,
                "access_token": access_token,
            })
            adapter.broker_account = broker_account
            adapter.broker_name    = "zerodha"

            logger.info(
                "BrokerAdapterFactory.get_adapter_for_account: Zerodha | "
                "account=%s | user=%s",
                broker_account.id, broker_account.user_id,
            )
            return adapter

        raise ValueError(
            f"Unsupported broker: {broker_account.broker} "
            f"(account={broker_account.id})"
        )

    @staticmethod
    def get_adapter(user_or_broker, credentials: dict | None = None, account_id: int | None = None):
        """
        Flexible factory — User object, broker string, ya credentials dict accept karta hai.

        Args:
            user_or_broker: Django User, broker name string ("fyers"), ya credentials dict
            credentials:    Credentials dict (sirf broker string case mein)
            account_id:     Specific account ID (optional, multi-account users ke liye)
        """
        from broker_adapters.fyers.adapter import FyersAdapter
        from broker_adapters.delta.adapter import DeltaAdapter

        # ── Case 1: Django User object ────────────────────────────────────────
        try:
            from django.contrib.auth import get_user_model
            User = get_user_model()
            if isinstance(user_or_broker, User):
                return BrokerAdapterFactory._get_adapter_for_user(
                    user_or_broker,
                    account_id=account_id,
                )
        except Exception:
            pass

        # ── Case 2: Broker name string ────────────────────────────────────────
        broker = str(user_or_broker).lower().strip()
        creds  = credentials or {}

        if broker == "fyers":
            return FyersAdapter(creds)
        if broker == "delta":
            return DeltaAdapter(creds)

        raise ValueError(f"Unsupported broker: {user_or_broker}")

    @staticmethod
    def _get_adapter_for_user(user, account_id: int | None = None):
        """
        User ka active + verified BrokerAccount fetch karo.

        Args:
            user:       Django User instance
            account_id: Specific account ID target karna ho toh pass karo

        Returns:
            Configured adapter for the user's account

        Raises:
            ValueError: agar koi active verified account nahi mila
        """
        from apps.brokers.models import BrokerAccount
        from broker_adapters.fyers.adapter import FyersAdapter
        from broker_adapters.delta.adapter import DeltaAdapter
        from django.conf import settings

        # ── Specific account_id se dhundho ────────────────────────────────────
        if account_id:
            try:
                account = BrokerAccount.objects.get(
                    id=account_id,
                    user=user,
                    is_active=True,
                    is_verified=True,
                )
                logger.info(
                    "BrokerAdapterFactory: specific account=%s | user=%s",
                    account_id, user.id,
                )
                return BrokerAdapterFactory.get_adapter_for_account(account)
            except BrokerAccount.DoesNotExist:
                logger.warning(
                    "BrokerAdapterFactory: account_id=%s not found for user=%s",
                    account_id, user.id,
                )
                raise ValueError(
                    f"Account {account_id} not found for user {user.id}."
                )

        # ── ✅ FIX: Priority order: Dhan → Delta → Fyers ─────────────────────────
        # User Dhan + Delta use karta hai, Fyers token frequently expire hota hai.
        # Dhan pehle try karo — fresh token, reliable.

        # ── Dhan (Indian — priority) ──────────────────────────────────────────
        account = (
            BrokerAccount.objects.filter(
                user=user,
                broker="dhan",
                is_active=True,
                is_verified=True,
            )
            .exclude(dhan_access_token__isnull=True)
            .exclude(dhan_access_token="")
            .order_by("-updated_at")
            .first()
        )

        if account:
            from broker_adapters.dhan.adapter import DhanAdapter
            logger.info(
                "BrokerAdapterFactory: Dhan ✅ | user=%s | account=%s",
                user.id, account.id,
            )
            adapter = DhanAdapter({
                "dhan_client_id":    account.dhan_client_id or "",
                "dhan_access_token": account.dhan_access_token or "",
            })
            adapter.broker_account = account
            adapter.broker_name    = "dhan"
            return adapter

        # ── Delta (Crypto) ────────────────────────────────────────────────────
        account = (
            BrokerAccount.objects.filter(
                user=user,
                broker="delta",
                is_active=True,
                is_verified=True,
            )
            .exclude(access_token__isnull=True)
            .exclude(access_token="")
            .order_by("-updated_at")
            .first()
        )

        if account:
            logger.info(
                "BrokerAdapterFactory: Delta ✅ | user=%s | account=%s",
                user.id, account.id,
            )
            adapter = DeltaAdapter({
                "api_key":    getattr(account, "api_key", "") or "",
                "api_secret": getattr(account, "api_secret", "") or "",
            })
            adapter.broker_account = account
            adapter.broker_name    = "delta"
            return adapter

        # ── Fyers (fallback — token expire hota rehta hai) ────────────────────
        account = (
            BrokerAccount.objects.filter(
                user=user,
                broker="fyers",
                is_active=True,
                is_verified=True,
            )
            .exclude(access_token__isnull=True)
            .exclude(access_token="")
            .order_by("-updated_at")
            .first()
        )

        if account:
            logger.info(
                "BrokerAdapterFactory: Fyers (fallback) | user=%s | account=%s",
                user.id, account.id,
            )
            app_id = account.app_id or getattr(settings, "FYERS_APP_ID", "")
            adapter = FyersAdapter({
                "app_id":       app_id,
                "access_token": account.access_token or "",
            })
            adapter.broker_account = account
            adapter.broker_name    = "fyers"
            return adapter

        raise ValueError(
            f"No active verified broker account found for user {user.id}. "
            "Please connect Fyers/Dhan account in Settings > Broker."
        )