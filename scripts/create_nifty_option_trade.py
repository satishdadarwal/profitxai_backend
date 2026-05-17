# scripts/create_nifty_option_trade.py
"""
Manual NIFTY Options Trade Generator
Usage: python manage.py shell < scripts/create_nifty_option_trade.py
"""

from apps.orders.services import create_order
from apps.strategies.models import Strategy
from apps.users.models import User
from apps.strategies.fyers_utils import get_atm_option_symbol
from apps.market.models import Asset
from decimal import Decimal

# ── Configuration ──────────────────────────────────────────
CURRENT_NIFTY_PRICE = 23900.0  # Adjust this
CE_PREMIUM = 150.00
CE_SL = 130.00
CE_TARGET = 200.00
PE_PREMIUM = 140.00
PE_SL = 120.00
PE_TARGET = 180.00
LOT_SIZE = 50

# ── Execution ──────────────────────────────────────────────
print("=" * 60)
print("NIFTY OPTIONS TRADE GENERATOR")
print("=" * 60)

# User aur Strategy
user = User.objects.first()
strategy = Strategy.objects.filter(user=user).first()

print(f"\nUser: {user.email}")
print(f"Strategy: {strategy.name if strategy else 'None'}")
print(f"Current NIFTY: {CURRENT_NIFTY_PRICE}")

# Generate option symbols
ce_symbol = get_atm_option_symbol("NIFTY", CURRENT_NIFTY_PRICE, "CE")
pe_symbol = get_atm_option_symbol("NIFTY", CURRENT_NIFTY_PRICE, "PE")

print(f"\nCE Symbol: {ce_symbol}")
print(f"PE Symbol: {pe_symbol}")

# ── Create Assets ──────────────────────────────────────────
print("\n" + "-" * 60)
print("Creating Assets...")
print("-" * 60)

ce_asset, ce_created = Asset.objects.get_or_create(
    symbol=ce_symbol,
    defaults={
        'name': f'NIFTY {int(CURRENT_NIFTY_PRICE)} CE',
        'asset_type': 'option',
        'exchange': 'NSE',
        'is_active': True,
        'lot_size': LOT_SIZE,
    }
)
print(f"CE Asset: {ce_asset.symbol} (Created: {ce_created})")

pe_asset, pe_created = Asset.objects.get_or_create(
    symbol=pe_symbol,
    defaults={
        'name': f'NIFTY {int(CURRENT_NIFTY_PRICE)} PE',
        'asset_type': 'option',
        'exchange': 'NSE',
        'is_active': True,
        'lot_size': LOT_SIZE,
    }
)
print(f"PE Asset: {pe_asset.symbol} (Created: {pe_created})")

# ── Create Orders ──────────────────────────────────────────
print("\n" + "-" * 60)
print("Creating Orders...")
print("-" * 60)

# Call Option
ce_order = create_order(
    strategy=strategy,
    symbol=ce_symbol,
    side="buy",
    quantity=LOT_SIZE,
    price=Decimal(str(CE_PREMIUM)),
    sl_price=Decimal(str(CE_SL)),
    target_price=Decimal(str(CE_TARGET)),
    instrument_type="option",
    broker=None,
    mode="paper",
)

print(f"\nCall Option Order:")
print(f"  ID: {ce_order.id}")
print(f"  Symbol: {ce_order.asset.symbol}")
print(f"  Side: {ce_order.side}")
print(f"  Quantity: {ce_order.quantity}")
print(f"  Premium: {ce_order.limit_price}")
print(f"  SL: {ce_order.sl_price}")
print(f"  Target: {ce_order.target_price}")
print(f"  Status: {ce_order.status}")

# Put Option
pe_order = create_order(
    strategy=strategy,
    symbol=pe_symbol,
    side="buy",
    quantity=LOT_SIZE,
    price=Decimal(str(PE_PREMIUM)),
    sl_price=Decimal(str(PE_SL)),
    target_price=Decimal(str(PE_TARGET)),
    instrument_type="option",
    broker=None,
    mode="paper",
)

print(f"\nPut Option Order:")
print(f"  ID: {pe_order.id}")
print(f"  Symbol: {pe_order.asset.symbol}")
print(f"  Side: {pe_order.side}")
print(f"  Quantity: {pe_order.quantity}")
print(f"  Premium: {pe_order.limit_price}")
print(f"  SL: {pe_order.sl_price}")
print(f"  Target: {pe_order.target_price}")
print(f"  Status: {pe_order.status}")

print("\n" + "=" * 60)
print("TRADES CREATED SUCCESSFULLY!")
print("=" * 60)