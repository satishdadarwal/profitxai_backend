# apps/brokers/utils.py
import logging
from .models import BrokerAccount

logger = logging.getLogger(__name__)


def get_adapter_for_account(broker_account: BrokerAccount):
    """
    BrokerAccount se correct adapter banao.

    Fyers  → fyers_apiv3.FyersModel
    Dhan   → DhanAdapter
    Zerodha→ ZerodhaAdapter (via registry)
    Delta  → DeltaAdapter (via registry)
    """
    broker = broker_account.broker

    # ── Dhan — registry se banao (BrokerAccount ke dhan_* fields use karo) ───
    if broker == "dhan":
        from broker_adapters.registry import BrokerRegistry
        credentials = {
            "dhan_client_id":    broker_account.dhan_client_id,
            "dhan_access_token": broker_account.dhan_access_token,
        }
        logger.info(
            "DhanAdapter created via registry | account=%s | client=%s",
            broker_account.id, broker_account.dhan_client_id,
        )
        return BrokerRegistry.make("dhan", credentials)

    # ── Fyers ─────────────────────────────────────────────────────────────────
    if broker == "fyers":
        from fyers_apiv3 import fyersModel
        fyers = fyersModel.FyersModel(
            client_id=broker_account.app_id,
            token=broker_account.access_token,
            log_path="",
            is_async=False,
        )
        logger.info("FyersModel created | account=%s", broker_account.id)
        return fyers

    # ── Registry (Zerodha, Delta, etc.) ──────────────────────────────────────
    try:
        from broker_adapters.registry import BrokerRegistry
        credentials = {
            "access_token": broker_account.access_token,
            "api_key":      broker_account.api_key,
            "api_secret":   broker_account.api_secret,
            "app_id":       broker_account.app_id,
            "secret_key":   broker_account.secret_key,
        }
        adapter = BrokerRegistry.make(broker, credentials)
        logger.info(
            "Adapter created via registry | broker=%s | account=%s",
            broker, broker_account.id
        )
        return adapter
    except Exception as e:
        logger.error(
            "Adapter creation failed | broker=%s | account=%s | %s",
            broker, broker_account.id, e
        )
        raise ValueError(f"Unsupported broker: {broker}") from e
