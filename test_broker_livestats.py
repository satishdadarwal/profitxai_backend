# test_broker_livestats.py
import os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

import socket
old_getaddrinfo = socket.getaddrinfo
def new_getaddrinfo(*args, **kwargs):
    responses = old_getaddrinfo(*args, **kwargs)
    return [r for r in responses if r[0] == socket.AF_INET]
socket.getaddrinfo = new_getaddrinfo

from apps.brokers.models import BrokerOrder, BrokerAccount
from apps.options.models import OptionSnapshot, OptionTrade
from apps.options.serializers import OptionTradeSerializer
import json

print("="*80)
print("🧪 TESTING BROKER & LIVE_STATS FIELDS")
print("="*80)

# Get last trade
trade = OptionTrade.objects.last()
if not trade:
    print("❌ No trade found!")
    exit(1)

print(f"\n✅ Testing trade: {trade.id}")
print(f"   Contract: {trade.contract.fyers_symbol}")
print(f"   User: {trade.user}")

# 1. Create BrokerOrder
print("\n" + "="*80)
print("1️⃣ CREATING BROKER ORDER")
print("="*80)

broker_account = BrokerAccount.objects.filter(user=trade.user, is_active=True).first()

if broker_account:
    print(f"✅ Found broker account: {broker_account.broker} ({broker_account.label})")
    
    broker_order, created = BrokerOrder.objects.get_or_create(
        option_trade=trade,
        order_type='entry',
        defaults={
            'broker_account': broker_account,
            'exchange_order_id': 'DEMO123456789',
            'status': 'complete'
        }
    )
    if created:
        print(f"✅ BrokerOrder created: {broker_order.id}")
    else:
        print(f"✅ BrokerOrder already exists: {broker_order.id}")
    
    print(f"   Status: {broker_order.status}")
    print(f"   Exchange Order ID: {broker_order.exchange_order_id}")
    print(f"   Broker: {broker_order.broker_name}")
else:
    print("⚠️  No active broker account found")
    print("   Creating a dummy broker account for testing...")
    
    broker_account = BrokerAccount.objects.create(
        user=trade.user,
        broker='fyers',
        label='Test Account',
        is_active=True,
        is_verified=True
    )
    print(f"✅ Created test broker account: {broker_account.id}")
    
    broker_order = BrokerOrder.objects.create(
        broker_account=broker_account,
        option_trade=trade,
        order_type='entry',
        exchange_order_id='DEMO123456789',
        status='complete'
    )
    print(f"✅ BrokerOrder created: {broker_order.id}")

# 2. Create OptionSnapshot
print("\n" + "="*80)
print("2️⃣ CREATING OPTION SNAPSHOT")
print("="*80)

snapshot, created = OptionSnapshot.objects.get_or_create(
    contract=trade.contract,
    defaults={
        'ltp': 155.0,
        'oi': 125000,
        'volume': 15000,
        'iv': 18.5,
        'delta': 0.45,
        'theta': -0.05,
        'spot_price': 22550.0
    }
)

if created:
    print(f"✅ OptionSnapshot created")
else:
    print(f"✅ OptionSnapshot already exists")
    # Update to latest
    snapshot.ltp = 155.0
    snapshot.oi = 125000
    snapshot.volume = 15000
    snapshot.spot_price = 22550.0
    snapshot.save()
    print(f"✅ OptionSnapshot updated")

print(f"   LTP: {snapshot.ltp}")
print(f"   OI: {snapshot.oi}")
print(f"   Volume: {snapshot.volume}")
print(f"   Spot: {snapshot.spot_price}")

# 3. Re-serialize with fresh query
print("\n" + "="*80)
print("3️⃣ RE-SERIALIZING TRADE")
print("="*80)

# Refresh from DB to get latest relations
trade = OptionTrade.objects.select_related(
    'symbol', 'contract', 'strategy', 'user'
).prefetch_related(
    'broker_orders', 'contract__snapshots'
).get(id=trade.id)

serializer = OptionTradeSerializer(trade)
data = serializer.data

# Check broker field
print("\n" + "="*80)
print("📊 BROKER FIELD RESULT:")
print("="*80)
if data['broker']:
    print("✅ BROKER DATA PRESENT!")
    print(json.dumps(data['broker'], indent=2))
else:
    print("❌ BROKER STILL NULL")
    print("   Debugging:")
    print(f"   - BrokerOrder exists: {trade.broker_orders.exists()}")
    print(f"   - BrokerOrder count: {trade.broker_orders.count()}")
    if trade.broker_orders.exists():
        bo = trade.broker_orders.first()
        print(f"   - First order: {bo}")
        print(f"   - Broker name: {bo.broker_name}")
        print(f"   - Exchange ID: {bo.exchange_order_id}")
        print(f"   - Status: {bo.status}")

# Check live_stats field
print("\n" + "="*80)
print("📊 LIVE_STATS FIELD RESULT:")
print("="*80)
if data['live_stats']:
    print("✅ LIVE_STATS DATA PRESENT!")
    print(json.dumps(data['live_stats'], indent=2))
else:
    print("❌ LIVE_STATS STILL NULL")
    print("   Debugging:")
    print(f"   - Contract: {trade.contract}")
    print(f"   - Snapshots exist: {trade.contract.snapshots.exists()}")
    print(f"   - Snapshots count: {trade.contract.snapshots.count()}")
    if trade.contract.snapshots.exists():
        snap = trade.contract.snapshots.latest()
        print(f"   - Latest snapshot: {snap}")
        print(f"   - LTP: {snap.ltp}")
        print(f"   - Timestamp: {snap.timestamp}")

# Full field check
print("\n" + "="*80)
print("📊 ALL 42 FIELDS STATUS:")
print("="*80)

required_fields = [
    'strategy', 'margin', 'risk', 'pnl_details', 'metadata',
    'broker', 'strike_selection', 'live_stats', 'chart_markers', 'notifications'
]

for field in required_fields:
    value = data.get(field)
    if value is not None:
        status = "✅"
        if isinstance(value, dict):
            preview = f"dict ({len(value)} keys)"
        else:
            preview = str(type(value).__name__)
    else:
        status = "❌"
        preview = "NULL"
    
    print(f"{status} {field:<20} → {preview}")

print("\n" + "="*80)
print("✅ TEST COMPLETE")
print("="*80)

# Summary
null_fields = [f for f in required_fields if data.get(f) is None]
if null_fields:
    print(f"\n⚠️  NULL fields: {', '.join(null_fields)}")
else:
    print("\n🎉 ALL FIELDS POPULATED!")