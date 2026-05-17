from broker_adapters.fyers.adapter import FyersAdapter
from broker_adapters.delta.adapter import DeltaAdapter


class BrokerAdapterFactory:
    """
    Central broker adapter factory
    """

    @staticmethod
    def get_adapter(broker_name: str, credentials: dict | None = None):
        credentials = credentials or {}

        broker = broker_name.lower().strip()

        if broker == "fyers":
            return FyersAdapter(credentials)

        if broker == "delta":
            return DeltaAdapter(credentials)

        raise ValueError(f"Unsupported broker: {broker_name}")