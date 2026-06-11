# test_broker_livestats.py
# NOTE: OptionTrade has been removed. Tests now use Order model.
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
from apps.options.models import OptionSnapshot
from apps.orders.models import Order
import json

print("="*80)
print("TESTING BROKER & LIVE_STATS FIELDS")
print("="*80)

# Get last options order
order = Order.objects.filter(instrument_type="options").last()
if not order:
    print("No order found!")
    exit(1)

print(f"\nTesting order: {order.id}")
print(f"   Symbol: {order.symbol_display}")
print(f"   User: {order.user}")

# 1. Check BrokerOrder
print("\n" + "="*80)
print("1. CHECKING BROKER ORDER")
print("="*80)

broker_account = BrokerAccount.objects.filter(user=order.user, is_active=True).first()

if broker_account:
    print(f"Found broker account: {broker_account.broker} ({broker_account.label})")
    broker_order = BrokerOrder.objects.filter(order=order).first()
    if broker_order:
        print(f"Found broker order: {broker_order.id}")
    else:
        print("No broker order linked to this order")
else:
    print("No broker account found for this user")

print("\nDone.")
