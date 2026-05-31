# broker_adapters/registry.py
#
# Plugin registry — naya broker add karna = sirf register() call karo.
# Baaki sab automatically kaam karega.

from typing import Dict, List, Type
from .base import BaseBrokerAdapter


class _BrokerRegistry:
    def __init__(self):
        self._adapters: Dict[str, Type[BaseBrokerAdapter]] = {}

    def register(self, adapter_cls: Type[BaseBrokerAdapter]):
        """
        Decorator ya direct call — dono kaam karte hain.

        Usage as decorator:
            @BrokerRegistry.register
            class MyAdapter(BaseBrokerAdapter): ...

        Usage direct:
            BrokerRegistry.register(MyAdapter)
        """
        slug = adapter_cls.BROKER_SLUG
        if not slug:
            raise ValueError(f"{adapter_cls.__name__} must set BROKER_SLUG")
        self._adapters[slug] = adapter_cls
        return adapter_cls

    def get(self, slug: str) -> Type[BaseBrokerAdapter]:
        """Return adapter class for a broker slug."""
        if slug not in self._adapters:
            raise KeyError(
                f'Broker "{slug}" not registered. ' f"Available: {list(self._adapters)}"
            )
        return self._adapters[slug]

    def has(self, slug: str) -> bool:
        return slug in self._adapters

    def list_brokers(self) -> List[Dict]:
        """Return metadata for all registered brokers — used in API."""
        return [cls.meta() for cls in self._adapters.values()]

    def make(self, slug: str, credentials: dict) -> BaseBrokerAdapter:
        """Instantiate an adapter with credentials."""
        return self.get(slug)(credentials)

    def all_slugs(self) -> List[str]:
        return list(self._adapters.keys())


# Singleton
BrokerRegistry = _BrokerRegistry()


# ─── Auto-discover all adapters ───────────────────────────────
# Yahan import karo taaki registry populate ho jaye at startup.
# Naya broker add karne par sirf yahan ek import line add karo.


def _autodiscover():
    # pylint: disable=import-outside-toplevel
    from broker_adapters.fyers import adapter as _fyers  # noqa
    from broker_adapters.delta import adapter as _delta  # noqa
    from broker_adapters.zerodha import adapter as _zerodha  # noqa
    from broker_adapters.paper import adapter as _paper  # noqa  ← BLOCKER #3 fix

    # Naya broker: bas yahan import karo
    from broker_adapters.dhan    import adapter as _dhan   # noqa  ✅ Dhan registered
    # from broker_adapters.angel   import adapter as _angel  # noqa
    # from broker_adapters.upstox  import adapter as _upstox # noqa


_autodiscover()