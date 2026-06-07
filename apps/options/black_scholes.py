# apps/options/black_scholes.py
import math

def _norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def _norm_pdf(x):
    return math.exp(-x * x / 2) / math.sqrt(2 * math.pi)

def compute_greeks(S, K, T, r, sigma, option_type='call'):
    """
    S: spot, K: strike, T: time to expiry (years),
    r: risk-free rate, sigma: IV (0.14 = 14%)
    """
    if T <= 0:
        price = max(S - K, 0) if option_type == 'call' else max(K - S, 0)
        return {'price': price, 'delta': 1.0 if option_type=='call' else -1.0,
                'gamma': 0, 'theta': 0, 'vega': 0, 'iv': sigma}
    if sigma <= 0:
        sigma = 0.15  # fallback IV

    d1 = (math.log(S / K) + (r + sigma**2 / 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    nd1 = _norm_cdf(d1)
    nd2 = _norm_cdf(d2)
    pdf_d1 = _norm_pdf(d1)

    if option_type == 'call':
        price = S * nd1 - K * math.exp(-r * T) * nd2
        delta = nd1
        theta = (-(S * pdf_d1 * sigma / (2 * math.sqrt(T)))
                 - r * K * math.exp(-r * T) * nd2) / 365
    else:
        price = K * math.exp(-r * T) * (1 - nd2) - S * (1 - nd1)
        delta = nd1 - 1
        theta = (-(S * pdf_d1 * sigma / (2 * math.sqrt(T)))
                 + r * K * math.exp(-r * T) * (1 - nd2)) / 365

    gamma = pdf_d1 / (S * sigma * math.sqrt(T))
    vega  = S * pdf_d1 * math.sqrt(T) / 100

    return {
        'price': round(price, 2),
        'delta': round(delta, 4),
        'gamma': round(gamma, 6),
        'theta': round(theta, 2),
        'vega':  round(vega, 2),
        'iv':    round(sigma * 100, 2),
    }


def greeks_from_chain(spot, strike, expiry_str, ce_ltp=0, pe_ltp=0,
                      ce_iv=0, pe_iv=0, r=0.065):
    """
    Compute Greeks from chain data.
    Uses IV from API if available, else estimates from LTP.
    """
    from datetime import datetime, date
    try:
        expiry = datetime.strptime(expiry_str, "%d-%m-%Y").date()
    except Exception:
        expiry = date.today()

    T = max((expiry - date.today()).days / 365, 0.001)

    # Use API IV if available else fallback 15%
    iv_ce = (ce_iv / 100) if ce_iv and ce_iv > 0 else 0.15
    iv_pe = (pe_iv / 100) if pe_iv and pe_iv > 0 else 0.15

    ce_greeks = compute_greeks(spot, strike, T, r, iv_ce, 'call')
    pe_greeks = compute_greeks(spot, strike, T, r, iv_pe, 'put')

    straddle_cost = round((ce_ltp or ce_greeks['price']) +
                          (pe_ltp or pe_greeks['price']), 2)
    dte = max((expiry - date.today()).days, 0)

    return {
        'strike':        strike,
        'expiry':        expiry_str,
        'dte':           dte,
        'spot':          spot,
        'ce_delta':      ce_greeks['delta'],
        'pe_delta':      pe_greeks['delta'],
        'gamma':         ce_greeks['gamma'],
        'theta':         ce_greeks['theta'],
        'vega':          ce_greeks['vega'],
        'iv_ce':         ce_greeks['iv'],
        'iv_pe':         pe_greeks['iv'],
        'straddle_cost': straddle_cost,
        'ce_price':      ce_greeks['price'],
        'pe_price':      pe_greeks['price'],
    }
