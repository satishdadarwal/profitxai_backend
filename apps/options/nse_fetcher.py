# apps/options/nse_fetcher.py
#
# FIX 1: user= parameter add kiya — ab user-specific Fyers account use hoga
# FIX 2: Cache key mein user_id include kiya — ek user ka cache doosre ko nahi milega
# FIX 3: Fallback: agar user ka account nahi mila toh kisi bhi active account try karo

import logging
from django.core.cache import cache
from apps.brokers.models import BrokerAccount
from fyers_apiv3 import fyersModel

logger = logging.getLogger(__name__)

FYERS_SYMBOL_MAP = {
    'NIFTY':      'NSE:NIFTY50-INDEX',
    'BANKNIFTY':  'NSE:NIFTYBANK-INDEX',
    'FINNIFTY':   'NSE:FINNIFTY-INDEX',
    'MIDCPNIFTY': 'NSE:MIDCPNIFTY-INDEX',
    'SENSEX':     'BSE:SENSEX-INDEX',
    'BANKEX':     'BSE:BANKEX-INDEX',
}


def fetch_nse_option_chain(symbol: str = "NIFTY", expiry_ts: str = "", user=None) -> dict:
    """
    Fyers API se live option chain fetch karo.

    Args:
        symbol:    Base symbol — 'NIFTY', 'BANKNIFTY', etc.
        expiry_ts: Unix timestamp string — "" = nearest expiry
        user:      Django user object — uske Fyers account ka token use hoga.
                   None hone par koi bhi active account use hoga (admin/fallback).
    """
    # User-specific cache key — ek user ka data doosre ko nahi milega
    user_id = getattr(user, 'id', 'shared')
    cache_key = f"nse_oc_{user_id}_{symbol}_{expiry_ts}"

    cached = cache.get(cache_key)
    if cached:
        return cached

    # ── User-specific account fetch karo ──────────────────────────────────
    acc = _get_fyers_account(user)

    fyers = fyersModel.FyersModel(
        client_id=acc.app_id,
        token=acc.access_token,
        log_path='',
        is_async=False,
    )

    fyers_symbol = FYERS_SYMBOL_MAP.get(symbol.upper(), f'NSE:{symbol.upper()}-INDEX')

    data = fyers.optionchain(data={
        'symbol':      fyers_symbol,
        'strikecount': 20,
        'timestamp':   expiry_ts,  # "" = nearest, "1778580000" = specific expiry
    })

    if data.get('s') != 'ok':
        raise Exception(f"Fyers option chain error: {data.get('message', 'Unknown error')}")

    d = data['data']
    options_chain   = d.get('optionsChain', [])
    raw_expiry_data = d.get('expiryData', [])

    # Spot price (strike_price == -1 row mein hota hai)
    spot_entry = next((x for x in options_chain if x.get('strike_price') == -1), None)
    spot = float(spot_entry['ltp']) if spot_entry else 0.0

    # Expiry list
    expiries = [e['date'] for e in raw_expiry_data]

    # Selected expiry
    if expiry_ts:
        match = next((e for e in raw_expiry_data if str(e.get('expiry')) == str(expiry_ts)), None)
        selected_expiry = match['date'] if match else (expiries[0] if expiries else '')
    else:
        selected_expiry = expiries[0] if expiries else ''

    # Chain build
    chain_map = {}
    for row in options_chain:
        if row.get('strike_price') == -1:
            continue

        strike   = row['strike_price']
        opt_type = row['option_type']  # 'CE' ya 'PE'

        if strike not in chain_map:
            chain_map[strike] = {
                'strike': strike,
                'expiry': selected_expiry,
                'CE': {},
                'PE': {},
            }

        chain_map[strike][opt_type] = {
            'ltp':    row.get('ltp', 0),
            'oi':     row.get('oi', 0),
            'oich':   row.get('oich', 0),
            'volume': row.get('volume', 0),
            'iv':     row.get('iv', 0),
            'bid':    row.get('bid', 0),
            'ask':    row.get('ask', 0),
            'ltpch':  row.get('ltpch', 0),
            'ltpchp': row.get('ltpchp', 0),
            # ✅ FIX: Fyers ka actual tradeable symbol store karo
            # Manually construct karna galat tha (code -50 aata tha)
            # Fyers response mein 'symbol' ya 'symList' field mein real symbol hota hai
            'symbol': row.get('symbol') or row.get('symList') or row.get('sym', ''),
        }

    result = {
        'spot':            spot,
        'expiries':        expiries,
        'raw_expiry_data': raw_expiry_data,
        'chain':           sorted(chain_map.values(), key=lambda x: x['strike']),
    }

    cache.set(cache_key, result, 30)  # 30 second cache
    return result


def _get_fyers_account(user=None):
    """
    User-specific Fyers BrokerAccount fetch karo.
    Agar user ka account nahi mila toh koi bhi active account try karo (fallback).
    """
    if user is not None:
        acc = (
            BrokerAccount.objects.filter(
                user=user,
                broker='fyers',
                is_active=True,
                is_verified=True,
            )
            .exclude(access_token__isnull=True)
            .exclude(access_token='')
            .first()
        )
        if acc:
            return acc

        logger.warning(
            "fetch_nse_option_chain: user=%s ka Fyers account nahi mila — "
            "kisi aur active account se try kar raha hoon",
            getattr(user, 'id', '?'),
        )

    # Fallback: koi bhi active account (sirf option chain read ke liye safe hai)
    acc = (
        BrokerAccount.objects.filter(
            broker='fyers',
            is_active=True,
            is_verified=True,
        )
        .exclude(access_token__isnull=True)
        .exclude(access_token='')
        .first()
    )

    if not acc:
        raise Exception(
            "No active Fyers account found. "
            "Please connect your Fyers account in Settings > Broker."
        )

    return acc