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

def fetch_nse_option_chain(symbol: str = "NIFTY", expiry_ts: str = "") -> dict:
    cache_key = f"nse_oc_{symbol}_{expiry_ts}"
    cached = cache.get(cache_key)
    if cached:
        return cached

    acc = BrokerAccount.objects.filter(
        broker='fyers',
        is_active=True,
        is_verified=True,
    ).first()

    if not acc or not acc.access_token:
        raise Exception("No active Fyers account found")

    fyers = fyersModel.FyersModel(
        client_id=acc.app_id,
        token=acc.access_token,
        log_path='',
        is_async=False,
    )

    fyers_symbol = FYERS_SYMBOL_MAP.get(symbol, f'NSE:{symbol}-INDEX')

    data = fyers.optionchain(data={
        'symbol':      fyers_symbol,
        'strikecount': 20,
        'timestamp':   expiry_ts,  # "" = nearest, "1778580000" = specific
    })

    if data.get('s') != 'ok':
        raise Exception(f"Fyers error: {data.get('message')}")

    d = data['data']
    options_chain  = d.get('optionsChain', [])
    raw_expiry_data = d.get('expiryData', [])

    # Spot
    spot_entry = next((x for x in options_chain if x.get('strike_price') == -1), None)
    spot = spot_entry['ltp'] if spot_entry else 0.0

    # Expiries
    expiries = [e['date'] for e in raw_expiry_data]

    # Selected expiry date string
    if expiry_ts:
        match = next((e for e in raw_expiry_data if e['expiry'] == expiry_ts), None)
        selected_expiry = match['date'] if match else (expiries[0] if expiries else '')
    else:
        selected_expiry = expiries[0] if expiries else ''

    # Chain build
    chain_map = {}
    for row in options_chain:
        if row.get('strike_price') == -1:
            continue

        strike   = row['strike_price']
        opt_type = row['option_type']

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
            'iv':     0,
            'bid':    row.get('bid', 0),
            'ask':    row.get('ask', 0),
            'ltpch':  row.get('ltpch', 0),
            'ltpchp': row.get('ltpchp', 0),
        }

    result = {
        'spot':            spot,
        'expiries':        expiries,
        'raw_expiry_data': raw_expiry_data,  # ✅ view ke liye
        'chain':           sorted(chain_map.values(), key=lambda x: x['strike']),
    }

    cache.set(cache_key, result, 30)
    return result