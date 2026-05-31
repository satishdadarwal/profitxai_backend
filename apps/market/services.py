# apps/market/services.py
#
# FIXES (2026-05-30):
#   ✅ FIX 1 — fetch_live_quote(): Master account pattern
#              Pehle user ka apna account check karo, nahi mila toh master account use karo
#              BrokerAdapterFactory.get_adapter_for_account() use karo (SEBI compliant)
#   ✅ FIX 2 — fetch_bulk_quotes(): Same master account pattern for Indian symbols
#              User ke paas koi account nahi toh bhi NIFTY price milega

import logging
from decimal import Decimal
from typing import Dict, List

logger = logging.getLogger(__name__)


def _get_fyers_account(user=None):
    """
    Market data ke liye best Fyers account dhundo.

    Priority (fyers_feed.py aur CandleDataView ke jaisa):
      1. settings.FYERS_APP_ID se match karo (master account)
      2. label="Master Account"
      3. User ka apna active account (agar user provided)
      4. Koi bhi active verified account

    Returns: BrokerAccount ya None
    """
    from apps.brokers.models import BrokerAccount
    from django.conf import settings

    master_app_id = getattr(settings, "FYERS_APP_ID", "").strip()

    # Step 1: Master account — FYERS_APP_ID se
    if master_app_id:
        account = (
            BrokerAccount.objects
            .filter(broker="fyers", is_active=True, app_id=master_app_id)
            .exclude(access_token__isnull=True)
            .exclude(access_token="")
            .order_by("-updated_at")
            .first()
        )
        if account:
            return account

    # Step 2: label="Master Account"
    account = (
        BrokerAccount.objects
        .filter(broker="fyers", is_active=True, is_verified=True,
                label="Master Account")
        .exclude(access_token__isnull=True)
        .exclude(access_token="")
        .first()
    )
    if account:
        return account

    # Step 3: User ka apna account
    if user and getattr(user, "is_authenticated", False):
        account = (
            BrokerAccount.objects
            .filter(user=user, broker="fyers", is_active=True, is_verified=True)
            .exclude(access_token__isnull=True)
            .exclude(access_token="")
            .order_by("-updated_at")
            .first()
        )
        if account:
            return account

    # Step 4: Koi bhi active account
    return (
        BrokerAccount.objects
        .filter(broker="fyers", is_active=True, is_verified=True)
        .exclude(access_token__isnull=True)
        .exclude(access_token="")
        .order_by("-updated_at")
        .first()
    )


def fetch_live_quote(symbol: str, user, broker_slug: str = "") -> Dict:
    from .delta_service import fetch_delta_ticker, is_crypto_symbol

    if is_crypto_symbol(symbol):
        return fetch_delta_ticker(symbol)

    from broker_adapters.factory import BrokerAdapterFactory
    from .models import Asset, MarketQuote

    try:
        # ✅ FIX 1: Master account pattern — user ke paas account nahi toh bhi kaam kare
        if not broker_slug or broker_slug == "fyers":
            account = _get_fyers_account(user)
        else:
            # Non-fyers broker — user ka apna account chahiye
            from apps.brokers.models import BrokerAccount
            account = BrokerAccount.objects.filter(
                user=user, broker=broker_slug,
                is_active=True, is_verified=True,
            ).exclude(access_token__isnull=True).exclude(access_token="").first()

        if not account:
            raise ValueError(
                f"No active broker account found for '{broker_slug or 'fyers'}'. "
                "Admin se Fyers master account connect karwao."
            )

        adapter = BrokerAdapterFactory.get_adapter_for_account(account)
        data    = adapter.get_quote(symbol)

    except Exception as e:
        logger.warning("Live quote failed | symbol=%s | error=%s", symbol, e)
        # DB fallback
        try:
            asset = Asset.objects.get(symbol__iexact=symbol)
            quote = asset.quote
            return {
                "symbol":     asset.symbol,
                "ltp":        float(quote.ltp),
                "bid":        float(quote.bid),
                "ask":        float(quote.ask),
                "volume":     quote.volume,
                "change":     float(quote.change),
                "change_pct": float(quote.change_pct),
                "cached":     True,
                "source":     "db_fallback",
            }
        except Exception:
            return {"error": str(e)}

    if not data or not data.get("ltp"):
        return {"error": f"No quote data for {symbol}"}

    # DB cache update
    try:
        asset = Asset.get_or_create_from_symbol(symbol)
        asset.last_price = Decimal(str(data["ltp"]))
        asset.save(update_fields=["last_price", "updated_at"])

        MarketQuote.objects.update_or_create(
            asset=asset,
            defaults={
                "ltp":        Decimal(str(data.get("ltp", 0))),
                "bid":        Decimal(str(data.get("bid", 0))),
                "ask":        Decimal(str(data.get("ask", 0))),
                "volume":     int(data.get("volume", 0)),
                "change":     Decimal(str(data.get("change", 0))),
                "change_pct": Decimal(str(data.get("change_pct", 0))),
            },
        )
    except Exception as exc:
        logger.warning("Quote DB cache update failed | %s", exc)

    return {
        "symbol":     symbol.upper(),
        "ltp":        data.get("ltp", 0),
        "bid":        data.get("bid", 0),
        "ask":        data.get("ask", 0),
        "volume":     data.get("volume", 0),
        "change":     data.get("change", 0),
        "change_pct": data.get("change_pct", 0),
        "cached":     False,
        "source":     getattr(account, "broker", broker_slug),
    }


def fetch_bulk_quotes(symbols: List[str], user, broker_slug: str = "fyers") -> Dict:
    from django.core.cache import cache

    from broker_adapters.factory import BrokerAdapterFactory
    from .delta_service import fetch_delta_tickers_bulk, is_crypto_symbol
    from .models import Asset, MarketQuote

    if not symbols:
        return {}

    cache_key = f"bulk_quotes:{getattr(user, 'id', 'anon')}:{','.join(sorted(symbols))}"
    try:
        cached = cache.get(cache_key)
        if cached:
            logger.info("✅ Serving %d quotes from cache", len(symbols))
            return cached
    except Exception as e:
        logger.warning("Cache read error: %s", e)

    crypto_syms = [s for s in symbols if is_crypto_symbol(s)]
    indian_syms = [s for s in symbols if not is_crypto_symbol(s)]
    result      = {}

    # ── Crypto bulk fetch ─────────────────────────────────────────
    if crypto_syms:
        try:
            delta_quotes = fetch_delta_tickers_bulk(crypto_syms)
            result.update(delta_quotes)
            logger.info("✅ Fetched %d crypto quotes", len(delta_quotes))
        except Exception as exc:
            logger.error("Delta bulk fetch failed: %s", exc)

    # ── Indian bulk fetch ─────────────────────────────────────────
    if indian_syms:
        try:
            # ✅ FIX 2: Master account pattern — user ke paas account nahi toh bhi kaam kare
            if broker_slug == "fyers" or not broker_slug:
                account = _get_fyers_account(user)
            else:
                from apps.brokers.models import BrokerAccount
                account = BrokerAccount.objects.filter(
                    user=user, broker=broker_slug,
                    is_active=True, is_verified=True,
                ).exclude(access_token__isnull=True).exclude(access_token="").first()

            if not account:
                logger.warning(
                    "fetch_bulk_quotes: no broker account for '%s' — using DB fallback",
                    broker_slug,
                )
                return _get_fallback_quotes(indian_syms, result)

            adapter     = BrokerAdapterFactory.get_adapter_for_account(account)
            bulk_quotes = adapter.get_bulk_quotes(indian_syms)
            result.update(bulk_quotes)
            logger.info("✅ Fetched %d Indian quotes in bulk | broker=%s | account=%s",
                        len(bulk_quotes), account.broker, account.id)

            # DB cache update
            for sym, data in bulk_quotes.items():
                try:
                    asset = Asset.get_or_create_from_symbol(sym)
                    asset.last_price = Decimal(str(data.get("ltp", 0)))
                    asset.save(update_fields=["last_price", "updated_at"])
                    MarketQuote.objects.update_or_create(
                        asset=asset,
                        defaults={
                            "ltp":        Decimal(str(data.get("ltp", 0))),
                            "bid":        Decimal(str(data.get("bid", 0))),
                            "ask":        Decimal(str(data.get("ask", 0))),
                            "volume":     int(data.get("volume", 0)),
                            "change":     Decimal(str(data.get("change", 0))),
                            "change_pct": Decimal(str(data.get("change_pct", 0))),
                        },
                    )
                except Exception:
                    pass

        except Exception as exc:
            logger.exception("Bulk quotes error: %s", exc)
            return _get_fallback_quotes(indian_syms, result)

    # Cache for 5 seconds
    try:
        if result:
            cache.set(cache_key, result, timeout=5)
    except Exception as e:
        logger.warning("Cache set failed: %s", e)

    return result


def search_assets(q: str, broker_slug: str, user) -> List[Dict]:
    from .models import Asset

    return list(
        Asset.objects.filter(is_active=True, symbol__icontains=q).values(
            "symbol", "name", "exchange", "asset_type"
        )[:20]
    )


def update_asset_price(symbol: str, price: Decimal):
    from .models import Asset, MarketQuote

    try:
        asset = Asset.objects.get(symbol__iexact=symbol)
        asset.last_price = price
        asset.save(update_fields=["last_price", "updated_at"])
        MarketQuote.objects.filter(asset=asset).update(ltp=price)
    except Asset.DoesNotExist:
        pass


def _get_fallback_quotes(symbols: List[str], existing_result: Dict) -> Dict:
    from .models import Asset

    result = dict(existing_result)
    for symbol in symbols:
        try:
            asset = Asset.objects.get(symbol__iexact=symbol)
            quote = asset.quote
            result[symbol.upper()] = {
                "symbol":     asset.symbol,
                "ltp":        float(quote.ltp),
                "bid":        float(quote.bid),
                "ask":        float(quote.ask),
                "volume":     quote.volume,
                "change":     float(quote.change),
                "change_pct": float(quote.change_pct),
                "cached":     True,
                "source":     "db_fallback",
            }
            logger.info("📦 Using cached quote for %s", symbol)
        except Exception:
            pass

    return result
