# apps/brokers/services.py — REPLACE karo poora

from .models import BrokerAccount


def get_user_broker_adapter(user, broker_slug=None):
    query = BrokerAccount.objects.filter(user=user, is_active=True)
    if broker_slug:
        query = query.filter(broker=broker_slug)
    account = query.first()

    if not account:
        raise Exception("No active broker connected")

    if account.broker == "zerodha":
        from broker_adapters.zerodha.adapter import ZerodhaAdapter
        return ZerodhaAdapter({
            "api_key": account.api_key,
            "access_token": account.access_token,
        })

    elif account.broker == "fyers":
        from fyers_apiv3 import fyersModel
        fyers = fyersModel.FyersModel(
            client_id=account.app_id,
            token=account.access_token,
            log_path="",
            is_async=False,
        )
        return fyers

    elif account.broker == "delta":
        return account  # Direct account return — signal_router handle karega

    raise Exception(f"Unsupported broker: {account.broker}")