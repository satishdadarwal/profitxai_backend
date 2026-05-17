# scripts/fyers_master_setup.py

"""
Fyers Master Account Setup — One-time OAuth Flow
================================================

Usage:
    python scripts/fyers_master_setup.py
"""

import os
import sys
import hashlib
import requests
from pathlib import Path
from urllib.parse import urlencode

# Add project root to path
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

# Load .env file using python-dotenv
try:
    from dotenv import load_dotenv
except ImportError:
    print("❌ ERROR: python-dotenv not installed")
    print("Run: pip install python-dotenv")
    sys.exit(1)

# Load .env file
env_path = project_root / '.env'
if not env_path.exists():
    print(f"❌ ERROR: .env file not found at {env_path}")
    print("\nCreate .env file in project root with:")
    print("  FYERS_APP_ID=your_app_id")
    print("  FYERS_SECRET_KEY=your_secret_key")
    print("  FYERS_REDIRECT_URI=http://27.59.119.101:8000/api/brokers/fyers/callback/")
    sys.exit(1)

load_dotenv(env_path)


def main():
    print("=" * 70)
    print("🚀 Fyers Master Account Setup")
    print("=" * 70)
    
    # Step 1: Read credentials from .env
    app_id = os.getenv("FYERS_APP_ID", "")
    secret_key = os.getenv("FYERS_SECRET_KEY", "")
    redirect_uri = os.getenv("FYERS_REDIRECT_URI", "")
    
    if not all([app_id, secret_key, redirect_uri]):
        print("\n❌ ERROR: Missing credentials in .env file")
        print(f"\n.env file location: {env_path}")
        print("\nCurrent values:")
        print(f"  FYERS_APP_ID = '{app_id}' {'❌' if not app_id else '✅'}")
        print(f"  FYERS_SECRET_KEY = '{secret_key[:10] if secret_key else ''}...' {'❌' if not secret_key else '✅'}")
        print(f"  FYERS_REDIRECT_URI = '{redirect_uri}' {'❌' if not redirect_uri else '✅'}")
        
        print("\n📋 How to get these:")
        print("  1. Go to: https://fyers.in/web/api-dashboard/user-apps")
        print("  2. Click on your app")
        print("  3. Copy 'App ID' and 'Secret Key'")
        print("  4. Add to .env file")
        return
    
    print(f"\n✅ App ID: {app_id}")
    print(f"✅ Secret Key: {secret_key[:10]}... (hidden)")
    print(f"✅ Redirect URI: {redirect_uri}")
    
    # Step 2: Generate auth URL
    auth_params = {
        "client_id": app_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": "master_setup",
    }
    auth_url = f"https://api-t1.fyers.in/api/v3/generate-authcode?{urlencode(auth_params)}"
    
    print("\n" + "=" * 70)
    print("📋 STEP 1: Open this URL in browser")
    print("=" * 70)
    print(f"\n{auth_url}\n")
    
    print("Instructions:")
    print("  1. Browser mein ye URL open karo")
    print("  2. Fyers login karo (email + password + TOTP)")
    print("  3. 'Authorize' button click karo")
    print("  4. Redirect hone ke baad URL bar se 'auth_code' copy karo")
    print("\n     Example redirect URL:")
    print("     http://...callback/?auth_code=eyJhbGci...&state=master_setup")
    print("                                    ^^^^^^^^^^^^")
    print("                                    Ye part copy karo")
    
    # Step 3: Get auth_code from user
    print("\n" + "=" * 70)
    print("📋 STEP 2: Enter auth_code")
    print("=" * 70)
    auth_code = input("\nPaste auth_code here: ").strip()
    
    if not auth_code:
        print("\n❌ ERROR: auth_code empty hai")
        return
    
    print(f"\n✅ Received auth_code: {auth_code[:20]}...")
    
    # Step 4: Exchange auth_code for tokens
    print("\n⏳ Exchanging auth_code for tokens...")
    
    app_id_hash = hashlib.sha256(f"{app_id}:{secret_key}".encode()).hexdigest()
    
    print(f"   App ID Hash: {app_id_hash[:16]}...")
    
    try:
        response = requests.post(
            "https://api-t1.fyers.in/api/v3/validate-authcode",
            json={
                "grant_type": "authorization_code",
                "appIdHash": app_id_hash,
                "code": auth_code,
            },
            timeout=15,
        )
        
        print(f"   HTTP Status: {response.status_code}")
        
        data = response.json()
        
        if data.get("s") == "ok":
            print("\n" + "=" * 70)
            print("✅ SUCCESS! Tokens received")
            print("=" * 70)
            
            access_token = data["access_token"]
            refresh_token = data["refresh_token"]
            
            print(f"\n📄 Access Token (expires in 24h):")
            print(f"   {access_token[:50]}...")
            
            print(f"\n🔑 Refresh Token (valid ~1 year):")
            print(f"   {refresh_token}")
            
            # Auto-update .env file
            print("\n" + "=" * 70)
            print("📋 STEP 3: Update .env file")
            print("=" * 70)
            
            try:
                with open(env_path, 'a', encoding='utf-8') as f:
                    f.write(f"\n# Fyers Master Account Tokens (auto-added by setup script)\n")
                    f.write(f"FYERS_MASTER_REFRESH_TOKEN={refresh_token}\n")
                
                print(f"\n✅ Auto-added to .env file!")
                print(f"   Location: {env_path}")
            except Exception as e:
                print(f"\n⚠️  Could not auto-update .env: {e}")
                print("\nManually add this line to .env:")
                print(f"\nFYERS_MASTER_REFRESH_TOKEN={refresh_token}")
            
            print("\n" + "=" * 70)
            print("✅ Setup Complete!")
            print("=" * 70)
            print("\n📋 Next steps:")
            print("  1. Setup TOTP in Fyers app:")
            print("     - Go to Fyers app -> Profile -> Security -> Enable TOTP")
            print("     - Copy the secret key (base32 string)")
            print("     - Add to .env: FYERS_MASTER_TOTP_SECRET=JBSWY3DPEHPK3PXP")
            print("\n  2. Test the setup:")
            print("     python manage.py shell")
            print("     >>> from apps.brokers.tasks import auto_refresh_master_fyers_token")
            print("     >>> result = auto_refresh_master_fyers_token()")
            print("     >>> print(result)")
            print("\n  3. Start Celery Beat for daily auto-refresh:")
            print("     celery -A config beat --loglevel=info")
            
        else:
            print("\n" + "=" * 70)
            print("❌ ERROR: Token exchange failed")
            print("=" * 70)
            print(f"\nFyers Response: {data}")
            
            error_code = data.get("code")
            error_msg = data.get("message", "Unknown error")
            
            print(f"\nError Code: {error_code}")
            print(f"Error Message: {error_msg}")
            
            print("\n💡 Common Issues:")
            print("  - auth_code expired (valid only 60 seconds) -> Try again quickly")
            print("  - redirect_uri mismatch -> Check Fyers dashboard settings")
            print("  - wrong app_id/secret_key -> Double-check .env file")
            print("  - app not approved -> Check Fyers dashboard status")
            
    except requests.exceptions.Timeout:
        print("\n❌ ERROR: Fyers API timeout")
        print("Try again in a few seconds")
        
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()