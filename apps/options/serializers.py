# apps/options/serializers.py
# COMPLETE PRODUCTION-READY VERSION

from rest_framework import serializers
from django.utils import timezone

from apps.options.models import (
    BacktestRun,
    OptionContract,
    OptionSnapshot,
    OptionSymbol,
    OptionTrade,
)

# Paper trading app models
from apps.paper_trading.models import (
    PaperAccount,
    PaperTopUp,
    PaperTrade,
)


class OptionSymbolSerializer(serializers.ModelSerializer):
    class Meta:
        model = OptionSymbol
        fields = ["id", "name", "fyers_symbol", "lot_size", "strike_step", "is_active"]


class OptionContractSerializer(serializers.ModelSerializer):
    symbol = OptionSymbolSerializer(read_only=True)

    class Meta:
        model = OptionContract
        fields = ["id", "symbol", "strike", "option_type", "expiry", "fyers_symbol"]


class OptionSnapshotSerializer(serializers.ModelSerializer):
    contract = OptionContractSerializer(read_only=True)

    class Meta:
        model = OptionSnapshot
        fields = [
            "id",
            "contract",
            "ltp",
            "oi",
            "volume",
            "iv",
            "delta",
            "theta",
            "spot_price",
            "timestamp",
        ]


class PaperAccountSerializer(serializers.ModelSerializer):
    user = serializers.StringRelatedField(read_only=True)

    class Meta:
        model = PaperAccount
        fields = ["id", "user", "balance", "initial_capital", "total_pnl"]


class PaperTopUpSerializer(serializers.ModelSerializer):
    account = PaperAccountSerializer(read_only=True)

    class Meta:
        model = PaperTopUp
        fields = ["id", "account", "amount", "status", "payment_id", "provider", "created_at"]


class PaperTradeSerializer(serializers.ModelSerializer):
    account = PaperAccountSerializer(read_only=True)
    
    # Computed fields
    instrument_type = serializers.SerializerMethodField()
    exchange = serializers.SerializerMethodField()
    status_color = serializers.SerializerMethodField()
    pnl_points = serializers.SerializerMethodField()
    trade_value = serializers.SerializerMethodField()
    days_to_expiry = serializers.SerializerMethodField()
    duration = serializers.SerializerMethodField()

    class Meta:
        model = PaperTrade
        fields = [
            "id", "account", "symbol", "asset_type", "side", "quantity",
            "lot_size", "leverage", "entry_price", "current_price",
            "stop_loss", "target_price", "exit_price", "strike_price",
            "option_type", "pnl", "margin_used", "status", "exit_reason",
            "setup_type", "strategy_id", "nifty_spot_at_entry",
            "opened_at", "closed_at", "unrealized_pnl", "unrealized_pnl_pct",
            # New computed fields
            "instrument_type", "exchange", "status_color", "pnl_points",
            "trade_value", "days_to_expiry", "duration"
        ]
    
    def get_instrument_type(self, obj):
        if hasattr(obj, 'asset_type') and obj.asset_type:
            return obj.asset_type
        
        if hasattr(obj, 'symbol') and obj.symbol:
            symbol_str = str(obj.symbol).upper()
            
            if any(crypto in symbol_str for crypto in ['BTC', 'ETH', 'USDT', 'DELTA', 'BINANCE', 'BYBIT']):
                return "futures"
            
            if 'CE' in symbol_str or 'PE' in symbol_str:
                return "options"
            elif 'NIFTY' in symbol_str or 'BANKNIFTY' in symbol_str:
                return "futures"
        
        return "options"
    
    def get_exchange(self, obj):
        return "NSE"
    
    def get_status_color(self, obj):
        status_colors = {
            'open': 'blue',
            'closed': 'green' if obj.pnl and obj.pnl > 0 else 'red',
            'pending': 'yellow',
            'cancelled': 'gray',
        }
        return status_colors.get(obj.status, 'blue')
    
    def get_pnl_points(self, obj):
        if obj.current_price and obj.entry_price:
            multiplier = 1 if obj.side == 'buy' else -1
            return float((obj.current_price - obj.entry_price) * multiplier)
        return 0.0
    
    def get_trade_value(self, obj):
        if obj.quantity and obj.current_price:
            return float(obj.quantity * obj.current_price)
        elif obj.quantity and obj.entry_price:
            return float(obj.quantity * obj.entry_price)
        return 0.0
    
    def get_days_to_expiry(self, obj):
        return None
    
    def get_duration(self, obj):
        if not obj.opened_at:
            return None
        
        if obj.closed_at:
            delta = obj.closed_at - obj.opened_at
        else:
            delta = timezone.now() - obj.opened_at
        
        days = delta.days
        hours = delta.seconds // 3600
        minutes = (delta.seconds % 3600) // 60
        
        if days > 0:
            duration_str = f"{days}d {hours}h"
        elif hours > 0:
            duration_str = f"{hours}h {minutes}m"
        else:
            duration_str = f"{minutes}m"
        
        if not obj.closed_at:
            duration_str += " (ongoing)"
        
        return duration_str


# ==================================
# STRATEGY SERIALIZER (for nesting)
# ==================================
class StrategyNestedSerializer(serializers.Serializer):
    """Lightweight strategy info for trade serialization"""
    id = serializers.UUIDField(read_only=True)
    name = serializers.CharField(read_only=True)
    algo_name = serializers.CharField(read_only=True)
    instrument_type = serializers.CharField(read_only=True)


# ==================================
# MAIN OPTIONTRADE SERIALIZER
# ==================================
class OptionTradeSerializer(serializers.ModelSerializer):
    user = serializers.StringRelatedField(read_only=True)
    symbol = OptionSymbolSerializer(read_only=True)
    contract = OptionContractSerializer(read_only=True)
    snapshots = OptionSnapshotSerializer(many=True, read_only=True, source="contract.snapshots")
    
    # Nested strategy
    strategy = StrategyNestedSerializer(read_only=True)
    
    # Computed fields - Basic
    instrument_type = serializers.SerializerMethodField()
    exchange = serializers.SerializerMethodField()
    status_color = serializers.SerializerMethodField()
    pnl_points = serializers.SerializerMethodField()
    trade_value = serializers.SerializerMethodField()
    days_to_expiry = serializers.SerializerMethodField()
    duration = serializers.SerializerMethodField()
    
    # Computed fields - Advanced nested objects
    margin = serializers.SerializerMethodField()
    risk = serializers.SerializerMethodField()
    pnl_details = serializers.SerializerMethodField()
    broker = serializers.SerializerMethodField()
    strike_selection = serializers.SerializerMethodField()
    live_stats = serializers.SerializerMethodField()
    chart_markers = serializers.SerializerMethodField()
    notifications = serializers.SerializerMethodField()

    class Meta:
        model = OptionTrade
        fields = [
            "id",
            "user",
            "mode",
            "instrument_type",
            "exchange",
            "symbol",
            "contract",
            "snapshots",
            "strategy",
            "action",
            "lots",
            "quantity",
            "entry_price",
            "target_price",
            "stop_loss",
            "current_price",
            "entry_spot",
            "current_spot",
            "status",
            "status_color",
            "exit_price",
            "exit_reason",
            "pnl",
            "pnl_points",
            "trade_value",
            "days_to_expiry",
            "duration",
            "setup_type",
            "timeframe",
            "entry_time",
            "exit_time",
            "backtest_run",
            # Advanced nested objects
            "margin",
            "risk",
            "pnl_details",
            "metadata",  # Direct from model
            "broker",
            "strike_selection",
            "live_stats",
            "chart_markers",
            "notifications",
            "confirmed_at",  # Direct from model
        ]
    
    # ================================
    # BASIC COMPUTED FIELDS
    # ================================
    
    def get_instrument_type(self, obj):
        exchange = None
        if obj.symbol and obj.symbol.fyers_symbol:
            exchange = obj.symbol.fyers_symbol.split(':')[0].upper()
        
        if exchange in ['DELTA', 'BINANCE', 'BYBIT', 'COINBASE', 'KRAKEN', 'KUCOIN']:
            return "futures"
        
        if obj.contract and obj.contract.fyers_symbol:
            symbol_upper = obj.contract.fyers_symbol.upper()
            if 'CE' in symbol_upper or 'PE' in symbol_upper:
                return "options"
            else:
                return "futures"
        
        return "options"
    
    def get_exchange(self, obj):
        if obj.symbol and obj.symbol.fyers_symbol:
            return obj.symbol.fyers_symbol.split(':')[0]
        return "NSE"
    
    def get_status_color(self, obj):
        status_colors = {
            'open': 'blue',
            'closed': 'green' if obj.pnl and obj.pnl > 0 else 'red',
            'pending': 'yellow',
            'cancelled': 'gray',
        }
        return status_colors.get(obj.status, 'blue')
    
    def get_pnl_points(self, obj):
        if obj.current_price and obj.entry_price:
            multiplier = 1 if obj.action == 'buy' else -1
            return float((obj.current_price - obj.entry_price) * multiplier)
        return 0.0
    
    def get_trade_value(self, obj):
        if obj.quantity and obj.current_price:
            return float(obj.quantity * obj.current_price)
        elif obj.quantity and obj.entry_price:
            return float(obj.quantity * obj.entry_price)
        return 0.0
    
    def get_days_to_expiry(self, obj):
        if obj.contract and obj.contract.expiry:
            delta = obj.contract.expiry - timezone.now().date()
            return delta.days
        return None
    
    def get_duration(self, obj):
        if not obj.entry_time:
            return None
        
        if obj.exit_time:
            delta = obj.exit_time - obj.entry_time
        else:
            delta = timezone.now() - obj.entry_time
        
        days = delta.days
        hours = delta.seconds // 3600
        minutes = (delta.seconds % 3600) // 60
        
        if days > 0:
            duration_str = f"{days}d {hours}h"
        elif hours > 0:
            duration_str = f"{hours}h {minutes}m"
        else:
            duration_str = f"{minutes}m"
        
        if not obj.exit_time:
            duration_str += " (ongoing)"
        
        return duration_str
    
    # ================================
    # ADVANCED NESTED OBJECTS
    # ================================
    
    def get_margin(self, obj):
        """Calculate margin requirements"""
        if not obj.entry_price or not obj.quantity:
            return None
        
        required = float(obj.entry_price * obj.quantity)
        
        return {
            "required": required,
            "premium_cost": required,
            "broker_margin": 0.0,  # Can be calculated from broker rules
            "leverage_used": 1.0   # Options typically no leverage on premium
        }
    
    def get_risk(self, obj):
        """Calculate risk metrics"""
        if not obj.entry_price or not obj.stop_loss or not obj.target_price:
            return None
        
        # Risk amount = (entry - SL) * quantity
        risk_amount = float((obj.entry_price - obj.stop_loss) * obj.quantity) if obj.action == 'buy' else float((obj.stop_loss - obj.entry_price) * obj.quantity)
        
        # SL percentage
        sl_percent = abs(float((obj.entry_price - obj.stop_loss) / obj.entry_price * 100))
        
        # TP percentage
        tp_percent = abs(float((obj.target_price - obj.entry_price) / obj.entry_price * 100))
        
        # Risk:Reward ratio
        if sl_percent > 0:
            rr_ratio = f"1:{tp_percent/sl_percent:.2f}"
        else:
            rr_ratio = "N/A"
        
        return {
            "risk_amount": abs(risk_amount),
            "risk_percent": None,  # Need account capital to calculate
            "sl_percent": round(sl_percent, 2),
            "tp_percent": round(tp_percent, 2),
            "risk_reward_ratio": rr_ratio
        }
    
    def get_pnl_details(self, obj):
        """Detailed P&L breakdown"""
        if not obj.current_price or not obj.entry_price or not obj.quantity:
            return None
        
        multiplier = 1 if obj.action == 'buy' else -1
        unrealized_pnl = float((obj.current_price - obj.entry_price) * obj.quantity * multiplier)
        trade_value = float(obj.entry_price * obj.quantity)
        unrealized_percent = (unrealized_pnl / trade_value * 100) if trade_value > 0 else 0
        
        # Brokerage (can be configured)
        brokerage = 50.0  # Example fixed brokerage
        net_pnl = unrealized_pnl - brokerage
        
        return {
            "unrealized_pnl": round(unrealized_pnl, 2),
            "unrealized_percent": round(unrealized_percent, 2),
            "realized_pnl": float(obj.pnl) if obj.pnl and obj.status == 'closed' else None,
            "realized_percent": None,  # Calculate when closed
            "brokerage": brokerage,
            "net_pnl": round(net_pnl, 2)
        }
    
    def get_broker(self, obj):
        """Broker order details from BrokerOrder model"""
        try:
            # Get latest broker order for this trade
            broker_order = obj.broker_orders.first()  # Reverse relation from BrokerOrder
            
            if broker_order:
                return {
                    "name": broker_order.broker_name,
                    "order_id": broker_order.exchange_order_id,
                    "order_status": broker_order.status,
                    "rejection_reason": broker_order.rejection_reason or None
                }
        except:
            pass
        
        return None
    
    def get_strike_selection(self, obj):
        """Strike selection analysis"""
        if not obj.contract or not obj.entry_spot:
            return None
        
        strike = obj.contract.strike
        spot = obj.entry_spot
        
        # Determine ATM/ITM/OTM
        diff = abs(strike - spot)
        strike_step = obj.symbol.strike_step if obj.symbol else 50
        
        if diff < strike_step / 2:
            moneyness = "ATM"
        elif (strike > spot and obj.contract.option_type == 'CE') or (strike < spot and obj.contract.option_type == 'PE'):
            moneyness = "OTM"
        else:
            moneyness = "ITM"
        
        # Find ATM strike (nearest to spot)
        atm_strike = round(spot / strike_step) * strike_step
        
        return {
            "type": moneyness,
            "atm_strike": float(atm_strike),
            "selected_strike": float(strike),
            "moneyness": moneyness
        }
    
    def get_live_stats(self, obj):
        """Latest market snapshot"""
        try:
            latest_snapshot = obj.contract.snapshots.latest()
            
            return {
                "last_updated": latest_snapshot.timestamp.isoformat(),
                "price_change_1m": None,  # Need historical data
                "volume_traded": latest_snapshot.volume,
                "open_interest": latest_snapshot.oi
            }
        except:
            return None
    
    def get_chart_markers(self, obj):
        """Chart annotation data"""
        entry_candle = {
            "timestamp": obj.entry_time.isoformat() if obj.entry_time else None,
            "price": float(obj.entry_price) if obj.entry_price else None,
            "type": f"{obj.action.upper()}_ENTRY"
        }
        
        exit_candle = None
        if obj.exit_time and obj.exit_price:
            exit_candle = {
                "timestamp": obj.exit_time.isoformat(),
                "price": float(obj.exit_price),
                "type": f"{obj.exit_reason.upper()}_EXIT" if obj.exit_reason else "EXIT"
            }
        
        return {
            "entry_candle": entry_candle,
            "sl_line": float(obj.stop_loss) if obj.stop_loss else None,
            "tp_line": float(obj.target_price) if obj.target_price else None,
            "exit_candle": exit_candle
        }
    
    def get_notifications(self, obj):
        """Notification status from Notification model"""
        try:
            from apps.notifications.models import Notification
            
            # Query notifications for this trade
            notifications = Notification.objects.filter(
                user=obj.user,
                metadata__trade_id=str(obj.id)
            )
            
            return {
                "entry_sent": notifications.filter(metadata__type='ENTRY').exists(),
                "sl_alert_sent": notifications.filter(metadata__type='SL').exists(),
                "tp_alert_sent": notifications.filter(metadata__type='TP').exists(),
                "exit_sent": notifications.filter(metadata__type='EXIT').exists(),
            }
        except:
            # Fallback if notification tracking not set up
            return {
                "entry_sent": False,
                "sl_alert_sent": False,
                "tp_alert_sent": False,
                "exit_sent": False,
            }


class BacktestRunSerializer(serializers.ModelSerializer):
    user = serializers.StringRelatedField(read_only=True)
    symbol = OptionSymbolSerializer(read_only=True)
    trades = OptionTradeSerializer(many=True, read_only=True)

    class Meta:
        model = BacktestRun
        fields = [
            "id",
            "user",
            "symbol",
            "from_date",
            "to_date",
            "strategy",
            "initial_capital",
            "status",
            "final_capital",
            "total_pnl",
            "win_rate",
            "max_drawdown",
            "total_trades",
            "error_message",
            "created_at",
            "completed_at",
            "trades",
        ]