# apps/orders/serializers.py
# UPDATED VERSION - WITH NOTES, TAGS, EMOJI_REACTION + POSITION SERIALIZERS

from rest_framework import serializers
from .models import Order, Trade, TradeJournalEntry, Position


class OrderSerializer(serializers.ModelSerializer):
    symbol = serializers.CharField(source='asset.symbol', read_only=True)
    asset_name = serializers.CharField(source='asset.name', read_only=True)
    
    class Meta:
        model = Order
        fields = [
            'id', 'user', 'asset', 'symbol', 'asset_name', 'strategy',
            'broker_account', 'side', 'order_type', 'status', 'mode',
            'quantity', 'filled_qty', 'remaining_qty', 'limit_price',
            'stop_price', 'avg_fill_price', 'sl_price', 'target_price',
            'exchange_order_id', 'execution_status', 'broker_response',
            'rejection_reason', 'notes', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'user', 'created_at', 'updated_at', 'remaining_qty']


class TradeSerializer(serializers.ModelSerializer):
    """
    Unified Trade serializer supporting both Indian and Crypto markets.
    Includes journal fields: notes, tags, emoji_reaction
    """
    symbol = serializers.CharField(source='asset.symbol', read_only=True)
    asset_name = serializers.CharField(source='asset.name', read_only=True)
    order_id = serializers.UUIDField(source='order.id', read_only=True)
    
    # Computed fields
    net_pnl = serializers.SerializerMethodField()
    market_display = serializers.CharField(source='get_market_type_display', read_only=True)
    
    class Meta:
        model = Trade
        fields = [
            # Core
            'id', 'order', 'order_id', 'user', 'asset', 'symbol', 'asset_name',
            'market_type', 'market_display', 'side', 'mode',
            
            # Common fields
            'quantity', 'price', 'amount', 'fee', 'realized_pnl', 'net_pnl',
            
            # Journal fields (NEW)
            'notes', 'tags', 'emoji_reaction',
            
            # Indian market specific
            'strike', 'lots', 'option_type',
            
            # Crypto market specific
            'leverage', 'funding_fee',
            
            # Timestamps
            'created_at',
        ]
        read_only_fields = ['id', 'user', 'created_at', 'net_pnl', 'market_display']
    
    def get_net_pnl(self, obj):
        """Calculate net PnL after fees"""
        if obj.realized_pnl is None:
            return None
        return float(obj.realized_pnl) - float(obj.fee or 0)


class TradeUpdateSerializer(serializers.ModelSerializer):
    """Serializer for updating notes/tags/emoji only"""
    
    class Meta:
        model = Trade
        fields = ['notes', 'tags', 'emoji_reaction']
    
    def validate_tags(self, value):
        """Ensure tags is a list of strings"""
        if not isinstance(value, list):
            raise serializers.ValidationError("Tags must be a list")
        if not all(isinstance(tag, str) for tag in value):
            raise serializers.ValidationError("All tags must be strings")
        return value
    
    def validate_emoji_reaction(self, value):
        """Validate emoji is a single character or empty"""
        if value and len(value) > 10:
            raise serializers.ValidationError("Emoji must be max 10 characters")
        return value


class TradeJournalEntrySerializer(serializers.ModelSerializer):
    trade_symbol = serializers.CharField(source='trade.asset.symbol', read_only=True)
    
    class Meta:
        model = TradeJournalEntry
        fields = [
            'id', 'user', 'trade', 'trade_symbol', 'order',
            'title', 'body', 'strategy', 'emotion', 'outcome',
            'rating', 'tags', 'screenshot',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'user', 'created_at', 'updated_at']


class TradeFilterSerializer(serializers.Serializer):
    """Serializer for trade filtering query params"""
    
    market_type = serializers.ChoiceField(
        choices=['indian', 'crypto', 'all'],
        default='all',
        required=False
    )
    mode = serializers.ChoiceField(
        choices=['live', 'paper', 'all'],
        default='all',
        required=False
    )
    tags = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        allow_empty=True
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
        source='pnl_percentage',
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