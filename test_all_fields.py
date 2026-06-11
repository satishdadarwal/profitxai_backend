# test_all_fields.py - Order Model Field Verification
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

from apps.orders.models import Order
from apps.options.models import OptionSymbol, OptionContract
from apps.users.models import User
from apps.strategies.models import Strategy
from django.utils import timezone
import json

print("="*80)
print("TESTING ORDER MODEL FIELDS")
print("="*80)

user = User.objects.first()
if not user:
    print("No user found! Create a user first.")
    exit(1)

strategy = Strategy.objects.first()

# Show last options order
order = Order.objects.filter(instrument_type="options").last()
if order:
    print(f"\nLast options order:")
    print(f"  id:              {order.id}")
    print(f"  mode:            {order.mode}")
    print(f"  side:            {order.side}")
    print(f"  status:          {order.status}")
    print(f"  instrument_type: {order.instrument_type}")
    print(f"  symbol_display:  {order.symbol_display}")
    print(f"  entry_price:     {order.entry_price}")
    print(f"  sl_price:        {order.sl_price}")
    print(f"  target_price:    {order.target_price}")
    print(f"  exit_price:      {order.exit_price}")
    print(f"  realized_pnl:    {order.realized_pnl}")
    print(f"  lots:            {order.lots}")
    print(f"  option_type:     {order.option_type}")
    print(f"  entry_time:      {order.entry_time}")
    print(f"  exit_time:       {order.exit_time}")
    print(f"  exit_reason:     {order.exit_reason}")
    print(f"  metadata:        {json.dumps(order.metadata, default=str)[:200]}")
else:
    print("No options orders found.")

print("\nDone.")
