# apps/orders/serializers.py
from decimal import Decimal

from rest_framework import serializers
from .models import Order, TradeJournalEntry, Position

# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

_INDIAN_INSTRUMENT_TYPES = frozenset(["options", "futures", "equity", "index", ""])


def _order_market_type(obj: Order) -> str:
    """Map Order.instrument_type → legacy 'indian' / 'crypto' label."""
    return "crypto" if obj.instrument_type == "crypto" else "indian"


class OrderSerializer(serializers.ModelSerializer):
    symbol = serializers.SerializerMethodField()
    asset_name = serializers.CharField(source='asset.name', read_only=True)
    # Flutter reads stop_loss or sl; expose sl_price under both names
    stop_loss = serializers.DecimalField(source='sl_price', max_digits=20, decimal_places=6, read_only=True)

    def get_symbol(self, obj):
        if obj.symbol_display:
            return obj.symbol_display
        if obj.asset:
            return obj.asset.symbol
        return obj.symbol or ''

    class Meta:
        model = Order
        fields = [
            'id', 'user', 'asset', 'symbol', 'asset_name', 'strategy',
            'broker_account', 'side', 'order_type', 'status', 'mode',
            'quantity', 'filled_qty', 'remaining_qty', 'limit_price',
            'stop_price', 'stop_loss', 'avg_fill_price', 'sl_price', 'target_price',
            'exchange_order_id', 'execution_status', 'broker_response',
            'rejection_reason', 'notes',
            'entry_price', 'exit_price', 'realized_pnl', 'unrealized_pnl',
            'current_price', 'entry_time', 'exit_time', 'exit_reason',
            'position_size', 'symbol_display',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'user', 'created_at', 'updated_at', 'remaining_qty']


class TradeSerializer(serializers.ModelSerializer):
    """
    Wraps Order but returns the same JSON shape Flutter expects for Trade records.
    field aliases: price→avg_fill_price, notes→journal_notes, order/order_id→id
    """
    symbol = serializers.CharField(source='asset.symbol', read_only=True)
    asset_name = serializers.CharField(source='asset.name', read_only=True)
    # Order IS the trade — expose its own id under both names for compat
    order = serializers.UUIDField(source='id', read_only=True)
    order_id = serializers.UUIDField(source='id', read_only=True)
    # Derived / computed compat fields
    price = serializers.SerializerMethodField()
    amount = serializers.SerializerMethodField()
    fee = serializers.SerializerMethodField()
    market_type = serializers.SerializerMethodField()
    market_display = serializers.SerializerMethodField()
    net_pnl = serializers.SerializerMethodField()
    notes = serializers.CharField(source='journal_notes', read_only=True)
    strike = serializers.SerializerMethodField()
    leverage = serializers.SerializerMethodField()
    funding_fee = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = [
            'id', 'order', 'order_id', 'user', 'asset', 'symbol', 'asset_name',
            'market_type', 'market_display', 'side', 'mode',
            'quantity', 'price', 'amount', 'fee', 'realized_pnl', 'net_pnl',
            'notes', 'tags', 'emoji_reaction',
            'strike', 'lots', 'option_type',
            'leverage', 'funding_fee',
            'created_at',
        ]
        read_only_fields = ['id', 'user', 'created_at']

    def get_price(self, obj):
        return obj.avg_fill_price or obj.entry_price or obj.limit_price

    def get_amount(self, obj):
        price = obj.avg_fill_price or obj.entry_price or obj.limit_price
        if price is not None and obj.quantity:
            return float(Decimal(str(price)) * Decimal(str(obj.quantity)))
        return None

    def get_fee(self, obj):
        return "0.00000000"

    def get_market_type(self, obj):
        return _order_market_type(obj)

    def get_market_display(self, obj):
        return "Crypto Market" if obj.instrument_type == "crypto" else "Indian Market"

    def get_net_pnl(self, obj):
        return float(obj.realized_pnl) if obj.realized_pnl is not None else None

    def get_strike(self, obj):
        return None

    def get_leverage(self, obj):
        return None

    def get_funding_fee(self, obj):
        return None


class TradeUpdateSerializer(serializers.ModelSerializer):
    """PATCH journal fields — notes maps to Order.journal_notes for compat."""
    notes = serializers.CharField(source='journal_notes', required=False, allow_blank=True)

    class Meta:
        model = Order
        fields = ['notes', 'tags', 'emoji_reaction']

    def validate_tags(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError("Tags must be a list")
        if not all(isinstance(tag, str) for tag in value):
            raise serializers.ValidationError("All tags must be strings")
        return value

    def validate_emoji_reaction(self, value):
        if value and len(value) > 10:
            raise serializers.ValidationError("Emoji must be max 10 characters")
        return value


class TradeJournalEntrySerializer(serializers.ModelSerializer):
    trade_symbol = serializers.SerializerMethodField()

    class Meta:
        model = TradeJournalEntry
        fields = [
            'id', 'user', 'order', 'trade_symbol',
            'title', 'body', 'strategy', 'emotion', 'outcome',
            'rating', 'tags', 'screenshot',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'user', 'created_at', 'updated_at']

    def get_trade_symbol(self, obj):
        if obj.order:
            if obj.order.asset_id:
                return obj.order.asset.symbol if obj.order.asset else ""
            return obj.order.symbol_display
        return ""


class TradeFilterSerializer(serializers.Serializer):
    market_type = serializers.ChoiceField(
        choices=['indian', 'crypto', 'all'],
        default='all',
        required=False,
    )
    mode = serializers.ChoiceField(
        choices=['live', 'paper', 'all'],
        default='all',
        required=False,
    )
    tags = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        allow_empty=True,
    )
    emoji = serializers.CharField(required=False, allow_blank=True)
    start_date = serializers.DateField(required=False)
    end_date = serializers.DateField(required=False)
    has_notes = serializers.BooleanField(required=False)


# ══════════════════════════════════════════════════════════════
# ✅ NEW: POSITION SERIALIZERS
# ══════════════════════════════════════════════════════════════

class PositionSerializer(serializers.ModelSerializer):
    """
    Position serializer for live trading position management.
    Tracks open positions with real-time P&L updates.
    """
    # Asset details
    symbol = serializers.CharField(source='asset.symbol', read_only=True)
    asset_name = serializers.CharField(source='asset.name', read_only=True)
    
    # Computed P&L percentage
    pnl_percentage = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        read_only=True
    )
    
    # Live signal reference (optional)
    signal_id = serializers.IntegerField(
        source='live_signal.id', 
        read_only=True, 
        allow_null=True
    )
    signal_type = serializers.CharField(
        source='live_signal.signal_type', 
        read_only=True, 
        allow_null=True
    )
    
    # Opening order reference
    opening_order_id = serializers.UUIDField(
        source='opening_order.id',
        read_only=True,
        allow_null=True
    )
    
    class Meta:
        model = Position
        fields = [
            # IDs
            'id',
            'user',
            'asset',
            'opening_order',
            'opening_order_id',
            
            # Basic info
            'symbol',
            'asset_name',
            'side',
            'status',
            'mode',
            
            # Quantity
            'quantity',
            'remaining_qty',
            
            # Pricing
            'avg_entry_price',
            'current_price',
            
            # P&L
            'unrealized_pnl',
            'realized_pnl',
            'pnl_percentage',
            
            # Risk management
            'stop_loss',
            'take_profit',
            
            # Live signal reference
            'live_signal',
            'signal_id',
            'signal_type',
            
            # Timestamps
            'opened_at',
            'closed_at',
            'updated_at',
        ]
        read_only_fields = [
            'id', 'user', 'opened_at', 'closed_at', 'updated_at',
            'unrealized_pnl', 'realized_pnl', 'pnl_percentage',
            'symbol', 'asset_name', 'opening_order_id', 'signal_id', 'signal_type'
        ]


class PositionCloseSerializer(serializers.Serializer):
    """
    Serializer for closing a position.
    Supports both full and partial closes.
    """
    close_price = serializers.DecimalField(
        max_digits=20,
        decimal_places=8,
        required=False,
        help_text="Close price (if not provided, current market price will be used)"
    )
    
    partial_qty = serializers.DecimalField(
        max_digits=20,
        decimal_places=8,
        required=False,
        help_text="Quantity to close (if not provided, entire position will be closed)"
    )
    
    def validate_close_price(self, value):
        """Validate close price is positive"""
        if value and value <= 0:
            raise serializers.ValidationError("Close price must be greater than 0")
        return value
    
    def validate_partial_qty(self, value):
        """Validate partial quantity is positive"""
        if value and value <= 0:
            raise serializers.ValidationError("Partial quantity must be greater than 0")
        return value


class CloseAllPositionsSerializer(serializers.Serializer):
    """
    Serializer for closing all positions with optional filters.
    """
    asset_ids = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        allow_empty=True,
        help_text="List of asset IDs to close (if empty, all positions will be closed)"
    )
    
    mode = serializers.ChoiceField(
        choices=['live', 'paper'],
        required=False,
        help_text="Filter by mode (live or paper trading)"
    )
    
    symbols = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        allow_empty=True,
        help_text="List of symbols to close (alternative to asset_ids)"
    )


class PositionUpdateSerializer(serializers.ModelSerializer):
    """
    Serializer for updating position risk management parameters.
    Allows updating stop loss and take profit without closing position.
    """
    class Meta:
        model = Position
        fields = ['stop_loss', 'take_profit']
    
    def validate(self, data):
        """Validate SL/TP values"""
        stop_loss = data.get('stop_loss')
        take_profit = data.get('take_profit')
        position = self.instance
        
        if position and position.side == Order.Side.BUY:
            # Long position
            if stop_loss and stop_loss >= position.avg_entry_price:
                raise serializers.ValidationError({
                    'stop_loss': 'Stop loss must be below entry price for long positions'
                })
            if take_profit and take_profit <= position.avg_entry_price:
                raise serializers.ValidationError({
                    'take_profit': 'Take profit must be above entry price for long positions'
                })
        
        elif position and position.side == Order.Side.SELL:
            # Short position
            if stop_loss and stop_loss <= position.avg_entry_price:
                raise serializers.ValidationError({
                    'stop_loss': 'Stop loss must be above entry price for short positions'
                })
            if take_profit and take_profit >= position.avg_entry_price:
                raise serializers.ValidationError({
                    'take_profit': 'Take profit must be below entry price for short positions'
                })
        
        return data