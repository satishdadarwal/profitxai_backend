# apps/orders/validators.py (NEW FILE)

from decimal import Decimal
from apps.market.services import fetch_live_quote

class OrderValidator:
    """
    Validate orders before execution
    """
    
    @staticmethod
    def validate_order(user, symbol, side, qty, order_type, price=None):
        """
        Pre-execution validation
        Returns: (is_valid, error_message)
        """
        # Check 1: Valid symbol
        if not symbol or len(symbol) < 2:
            return False, "Invalid symbol"
        
        # Check 2: Valid side
        if side not in ['buy', 'sell']:
            return False, "Invalid side - must be 'buy' or 'sell'"
        
        # Check 3: Valid quantity
        if qty <= 0:
            return False, "Quantity must be positive"
        
        # Check 4: Price validation for limit orders
        if order_type == 'limit':
            if not price or price <= 0:
                return False, "Limit orders require valid price"
            
            # Check price is within reasonable range of LTP
            quote = fetch_live_quote(symbol, user)
            ltp = Decimal(str(quote.get('ltp', 0)))
            
            if ltp > 0:
                price_diff_pct = abs(Decimal(str(price)) - ltp) / ltp * 100
                
                # Price cannot be more than 10% away from LTP
                if price_diff_pct > 10:
                    return False, f"Price too far from market ({price_diff_pct:.1f}% away)"
        
        # Check 5: User has sufficient funds
        from apps.wallet.models import Wallet
        wallet = Wallet.objects.get(user=user)
        
        required_margin = calculate_margin(symbol, qty, price or 0)
        if wallet.available_balance < required_margin:
            return False, "Insufficient funds"
        
        # Check 6: Not a duplicate order in last 5 seconds
        from django.utils import timezone
        from datetime import timedelta
        from apps.orders.models import Order
        
        recent_cutoff = timezone.now() - timedelta(seconds=5)
        duplicate = Order.objects.filter(
            user=user,
            symbol=symbol,
            side=side,
            quantity=qty,
            created_at__gte=recent_cutoff
        ).exists()
        
        if duplicate:
            return False, "Duplicate order detected"
        
        return True, ""

def calculate_margin(symbol: str, qty: Decimal, price: Decimal) -> Decimal:
    """Calculate required margin for order"""
    # Simplified - actual calculation depends on broker & instrument type
    notional_value = qty * price
    margin_pct = Decimal('0.20')  # 20% margin
    return notional_value * margin_pct