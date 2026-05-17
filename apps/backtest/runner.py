from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

# ── Global algo registry — strategies/views.py yahan se read karta hai
_REGISTRY: Dict[str, type] = {}


def register_algo(name: str):
    """Decorator — algo class ko registry mein daalo."""

    def decorator(cls):
        _REGISTRY[name] = cls
        return cls

    return decorator


class BaseAlgo(ABC):
    """Sabhi algos yeh inherit karein."""

    def __init__(self, parameters: Optional[dict] = None):
        self.parameters = parameters or {}

    @abstractmethod
    def generate_signal(self, candles: List[Any]) -> Dict[str, Any]:
        """
        Candle list lo, signal dict return karo.
        Return: {
            'action': 'buy'|'sell'|None,
            'symbol': str,
            'price': float,
            'reason': str,
            'quantity': int
        }
        """
        raise NotImplementedError
