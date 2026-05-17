# broker_adapters/base.py
#
# Har naya broker add karne ke liye sirf yeh steps:
# 1. Ek nayi directory banao: broker_adapters/<broker>/adapter.py
# 2. BaseBrokerAdapter inherit karo
# 3. BROKER_SLUG, BROKER_NAME, REQUIRED_CREDENTIAL_FIELDS set karo
# 4. Abstract methods implement karo
# 5. BrokerRegistry.register() call karo (adapter.py ke bottom mein)
# 6. Bas — koi aur change nahi chahiye

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ─── Standard response shapes ────────────────────────────────
@dataclass
class OrderResult:
    success: bool
    order_id: Optional[str] = None
    message: str = ""
    raw: Dict = field(default_factory=dict)


@dataclass
class PositionInfo:
    symbol: str
    side: str  # 'long' | 'short'
    qty: float
    entry_price: float
    current_price: float
    pnl: float
    raw: Dict = field(default_factory=dict)


@dataclass
class FundsInfo:
    available: float
    used: float
    total: float
    currency: str = "INR"
    raw: Dict = field(default_factory=dict)


@dataclass
class CandleBar:
    timestamp: int  # unix seconds
    open: float
    high: float
    low: float
    close: float
    volume: float


# ─── Base Adapter ─────────────────────────────────────────────
class BaseBrokerAdapter(ABC):
    """
    Abstract base class for all broker adapters.
    Subclass this and implement every abstract method.
    """

    # ── Class-level metadata (override in each subclass) ────
    BROKER_SLUG: str = ""  # e.g. 'fyers', 'delta', 'zerodha'
    BROKER_NAME: str = ""  # e.g. 'Fyers', 'Delta India', 'Zerodha'
    SUPPORTS_OPTIONS: bool = False
    SUPPORTS_FUTURES: bool = False
    SUPPORTS_EQUITY: bool = True
    SUPPORTS_CRYPTO: bool = False

    # Fields that must be present in credentials dict
    REQUIRED_CREDENTIAL_FIELDS: List[str] = []

    def __init__(self, credentials: Dict[str, Any]):
        """
        credentials: decrypted dict from BrokerCredential.
        e.g. {'api_key': '...', 'api_secret': '...', 'access_token': '...'}
        """
        self.credentials = credentials
        self._validate_credentials()

    def _validate_credentials(self):
        missing = [
            f
            for f in self.REQUIRED_CREDENTIAL_FIELDS
            if f not in self.credentials or not self.credentials[f]
        ]
        if missing:
            raise ValueError(f"[{self.BROKER_SLUG}] Missing credentials: {missing}")

    # ── Must implement ───────────────────────────────────────

    @abstractmethod
    def verify_connection(self) -> Dict:
        """
        Test if credentials are valid.
        Returns: {'success': bool, 'message': str, 'profile': dict}
        """
        ...

    @abstractmethod
    def get_funds(self) -> FundsInfo:
        """Return available funds/margin."""
        ...

    @abstractmethod
    def get_positions(self) -> List[PositionInfo]:
        """Return list of open positions."""
        ...

    @abstractmethod
    def get_orders(self, status: str = "all") -> List[Dict]:
        """Return order list. status: 'open'|'closed'|'all'"""
        ...

    @abstractmethod
    def place_order(
        self,
        symbol: str,
        side: str,  # 'buy' | 'sell'
        qty: float,
        order_type: str = "market",  # 'market' | 'limit'
        price: float = 0,
        **kwargs,
    ) -> OrderResult:
        """Place an order."""
        ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> OrderResult:
        """Cancel an existing order."""
        ...

    @abstractmethod
    def get_quote(self, symbol: str) -> Dict:
        """
        Get live quote for a symbol.
        Returns: {'ltp': float, 'bid': float, 'ask': float, 'volume': int, ...}
        """
        ...

    @abstractmethod
    def get_candles(
        self,
        symbol: str,
        resolution: str,  # '1m','5m','15m','1h','1d'
        from_ts: int,  # unix seconds
        to_ts: int,
    ) -> List[CandleBar]:
        """Return OHLCV candle data."""
        ...

    # ── Optional override ────────────────────────────────────

    def refresh_token(self) -> Dict:
        """
        Refresh access token if broker supports it.
        Override in brokers that need token refresh (e.g. Fyers).
        Returns: {'success': bool, 'access_token': str}
        """
        return {"success": False, "message": "Token refresh not supported"}

    def get_option_chain(self, symbol: str, expiry: str) -> List[Dict]:
        """Override for brokers that support options."""
        raise NotImplementedError(f"{self.BROKER_NAME} does not support option chain")

    @classmethod
    def meta(cls) -> Dict:
        """Return broker metadata for UI display."""
        return {
            "slug": cls.BROKER_SLUG,
            "name": cls.BROKER_NAME,
            "supports_options": cls.SUPPORTS_OPTIONS,
            "supports_futures": cls.SUPPORTS_FUTURES,
            "supports_equity": cls.SUPPORTS_EQUITY,
            "supports_crypto": cls.SUPPORTS_CRYPTO,
            "required_fields": cls.REQUIRED_CREDENTIAL_FIELDS,
        }
