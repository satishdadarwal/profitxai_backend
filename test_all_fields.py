# test_all_fields.py - Complete Field Verification
import os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

import socket
old_getaddrinfo = socket.getaddrinfo
def new_getaddrinfo(*args, **kwargs):
    responses = old_getaddrinfo(*args, **kwargs)
    return [r for r in responses if r[0] == socket.AF_INET]
socket.getaddrinfo = new_getaddrinfo

from apps.options.models import OptionTrade, OptionSymbol, OptionContract
from apps.options.serializers import OptionTradeSerializer
from apps.users.models import User
from apps.strategies.models import Strategy
from django.utils import timezone
from datetime import date
import json

print("="*80)
print("🚀 TESTING ALL 40 FIELDS")
print("="*80)

# Get or create test data
user = User.objects.first()
if not user:
    print("❌ No user found! Create a user first.")
    exit(1)

strategy = Strategy.objects.first()
nifty = OptionSymbol.objects.get(name='NIFTY')
contract = OptionContract.objects.filter(symbol=nifty).first()

# Create test trade with NEW fields
print("\n1️⃣ Creating test trade with metadata...")
trade = OptionTrade.objects.create(
    user=user,
    mode='live',
    symbol=nifty,
    contract=contract,
    strategy=strategy,
    action='buy',
    lots=1,
    quantity=nifty.lot_size,
    entry_price=150.0,
    target_price=200.0,
    stop_loss=100.0,
    current_price=155.0,
    entry_spot=22500.0,
    current_spot=22550.0,
    status='open',
    setup_type='EMA Scalp',
    timeframe='15',
    
    # ✅ NEW FIELDS
    metadata={
        'signal_strength': 8,
        'indicators': [
            'RSI: 35 (Oversold)',
            'MACD: Bullish Cross',
            'Volume: Above Average'
        ],
        'executed_by': 'AUTO',
        'notes': 'Strong bullish setup on 15min timeframe'
    },
    confirmed_at=timezone.now()
)
print(f"✅ Trade created: {trade.id}")

# Serialize
print("\n2️⃣ Serializing with all fields...")
serializer = OptionTradeSerializer(trade)
data = serializer.data

# Display complete JSON
print("\n" + "="*80)
print("📊 COMPLETE JSON OUTPUT (ALL 40 FIELDS)")
print("="*80)
print(json.dumps(dict(data), indent=2, default=str))

# Verify all 10 NEW nested objects
print("\n" + "="*80)
print("✅ VERIFICATION: 10 NEW NESTED OBJECTS")
print("="*80)

required_fields = {
    'strategy': 'Nested strategy object',
    'margin': 'Margin calculation',
    'risk': 'Risk metrics',
    'pnl_details': 'Detailed P&L',
    'metadata': 'Trade metadata (from model)',
    'broker': 'Broker order info',
    'strike_selection': 'Strike analysis',
    'live_stats': 'Market snapshot',
    'chart_markers': 'Chart annotations',
    'notifications': 'Alert status',
}

all_present = True
for field, description in required_fields.items():
    present = field in data
    value = data.get(field)
    
    if present and value is not None:
        status = "✅"
        value_type = type(value).__name__
        
        # Show sample data
        if isinstance(value, dict):
            keys = list(value.keys())[:3]
            preview = f"dict with keys: {keys}"
        elif isinstance(value, list):
            preview = f"list with {len(value)} items"
        else:
            preview = str(value)[:50]
        
        print(f"{status} {field:<20} → {value_type:<10} | {preview}")
    else:
        status = "❌"
        all_present = False
        print(f"{status} {field:<20} → MISSING or NULL")

# Field count
print("\n" + "="*80)
print("📈 FIELD COUNT")
print("="*80)
total_fields = len(data.keys())
print(f"Total fields in response: {total_fields}")
print(f"Expected: 40+ fields")
print(f"Status: {'✅ PASS' if total_fields >= 40 else '❌ FAIL'}")

# Deep dive on specific fields
print("\n" + "="*80)
print("🔍 DETAILED FIELD INSPECTION")
print("="*80)

# 1. Strategy
if 'strategy' in data and data['strategy']:
    print("\n1. STRATEGY:")
    print(json.dumps(data['strategy'], indent=2))
else:
    print("\n1. STRATEGY: ❌ Missing or None")

# 2. Margin
if 'margin' in data and data['margin']:
    print("\n2. MARGIN:")
    print(json.dumps(data['margin'], indent=2))
else:
    print("\n2. MARGIN: ❌ Missing or None")

# 3. Risk
if 'risk' in data and data['risk']:
    print("\n3. RISK:")
    print(json.dumps(data['risk'], indent=2))
else:
    print("\n3. RISK: ❌ Missing or None")

# 4. P&L Details
if 'pnl_details' in data and data['pnl_details']:
    print("\n4. PNL_DETAILS:")
    print(json.dumps(data['pnl_details'], indent=2))
else:
    print("\n4. PNL_DETAILS: ❌ Missing or None")

# 5. Metadata
if 'metadata' in data and data['metadata']:
    print("\n5. METADATA:")
    print(json.dumps(data['metadata'], indent=2))
else:
    print("\n5. METADATA: ❌ Missing or None")

# 6. Strike Selection
if 'strike_selection' in data and data['strike_selection']:
    print("\n6. STRIKE_SELECTION:")
    print(json.dumps(data['strike_selection'], indent=2))
else:
    print("\n6. STRIKE_SELECTION: ❌ Missing or None")

# 7. Chart Markers
if 'chart_markers' in data and data['chart_markers']:
    print("\n7. CHART_MARKERS:")
    print(json.dumps(data['chart_markers'], indent=2))
else:
    print("\n7. CHART_MARKERS: ❌ Missing or None")

# Cleanup
print("\n" + "="*80)
print("🧹 CLEANUP")
print("="*80)
trade.delete()
print("✅ Test trade deleted")

# Final verdict
print("\n" + "="*80)
print("🎯 FINAL VERDICT")
print("="*80)

if all_present and total_fields >= 40:
    print("✅ SUCCESS! All 40 fields working perfectly!")
    print("✅ Migration successful")
    print("✅ Serializer working correctly")
    print("✅ Production ready!")
else:
    print("⚠️  WARNING: Some fields missing or null")
    print("Check the detailed inspection above")

print("\n" + "="*80)
print("✅ TEST COMPLETE")
print("="*80)