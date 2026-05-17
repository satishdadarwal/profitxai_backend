# test_token_refresh.py

import os
import sys
import django

# Django setup
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.conf import settings
from apps.brokers.tasks import auto_refresh_master_fyers_token

print("=" * 60)
print("Testing Fyers Master Token Refresh")
print("=" * 60)

# Check all credentials
print(f"\n1. Credentials Check:")
print(f"   FYERS_APP_ID: {getattr(settings, 'FYERS_APP_ID', 'NOT SET')}")
print(f"   FYERS_SECRET_KEY: {getattr(settings, 'FYERS_SECRET_KEY', 'NOT SET')[:10]}...")
print(f"   FYERS_MASTER_TOTP_SECRET: {getattr(settings, 'FYERS_MASTER_TOTP_SECRET', 'NOT SET')[:10]}...")

token = getattr(settings, 'FYERS_MASTER_REFRESH_TOKEN', '')
print(f"   FYERS_MASTER_REFRESH_TOKEN: {len(token)} chars")

if len(token) > 0:
    print(f"      First 20: {token[:20]}")
    print(f"      Last 20: {token[-20:]}")

# Test TOTP
print(f"\n2. TOTP Test:")
totp_secret = getattr(settings, 'FYERS_MASTER_TOTP_SECRET', '')
if totp_secret:
    import pyotp
    try:
        totp = pyotp.TOTP(totp_secret)
        current_code = totp.now()
        print(f"   ✅ TOTP Working: {current_code}")
    except Exception as e:
        print(f"   ❌ TOTP Error: {e}")
else:
    print("   ❌ TOTP Secret: NOT SET")

# Test refresh
print(f"\n3. Token Refresh Test:")
try:
    result = auto_refresh_master_fyers_token()
    print(f"   Result: {result}")
    
    if result.get('status') == 'success':
        print("\n" + "=" * 60)
        print("✅ SUCCESS! Everything working!")
        print("=" * 60)
    else:
        print("\n" + "=" * 60)
        print("❌ FAILED! Check error above")
        print("=" * 60)
        
except Exception as e:
    print(f"   ❌ ERROR: {e}")
    import traceback
    traceback.print_exc()