# apps/brokers/utils.py  -- NEW FILE (was missing, caused ImportError)
import logging
from .models import BrokerAccount

logger = logging.getLogger(__name__)


def get_adapter_for_account(broker_account: BrokerAccount):
    from broker_adapters.registry import BrokerRegistry
    credentials = {
        "access_token": broker_account.access_token,
        "api_key":      broker_account.api_key,
        "api_secret":   broker_account.api_secret,
        "app_id":       broker_account.app_id,
        "secret_key":   broker_account.secret_key,
    }
    try:
        adapter = BrokerRegistry.make(broker_account.broker, credentials)
        logger.info("Adapter created | broker=%s | account=%s", broker_account.broker, broker_account.id)
        return adapter
    except Exception as e:
        logger.error("Adapter creation failed | broker=%s | %s", broker_account.broker, e)
        raise ValueError(f"Unsupported broker: {broker_account.broker}") from e