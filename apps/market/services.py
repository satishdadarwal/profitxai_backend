# apps/market/services.py

import logging
from decimal import Decimal
from typing import Dict, List

logger = logging.getLogger(__name__)


def fetch_live_quote(symbol: str, user, broker_slug: str = "") -> Dict:
    from .delta_service import fetch_delta_ticker, is_crypto_symbol

    if is_crypto_symbol(symbol):
        return fetch_delta_ticker(symbol)

    from apps.brokers.models import BrokerAccount
    from broker_adapters.registry import BrokerRegistry

    from .models import Asset, MarketQuote

    try:
        if not broker_slug:
            broker_slug = "fyers"

        cred = BrokerAccount.objects.filter(
            user=user, broker=broker_slug, is_active=True
        ).first()

        if not cred:
            raise ValueError(f"No active broker connection found for '{broker_slug}'")

        # ✅ make(slug, credentials_dict) — registry ka sahi method
        adapter = BrokerRegistry.make(
            cred.broker,
            {
                "access_token": cred.access_token,
                "app_id": cred.app_id,
                "secret_key": cred.secret_key,
            },
        )

        data = adapter.get_quote(symbol)

    except Exception as e:
        logger.warning("Live quote failed | symbol=%s | error=%s", symbol, e)
        try:
            asset = Asset.objects.get(symbol__iexact=symbol)
            quote = asset.quote
            return {
                "symbol": asset.symbol,
                "ltp": float(quote.ltp),
                "bid": float(quote.bid),
                "ask": float(quote.ask),
                "volume": quote.volume,
                "change": float(quote.change),
                "change_pct": float(quote.change_pct),
                "cached": True,
            }
        except Exception:
            return {"error": str(e)}

    if not data or not data.get("ltp"):
        return {"error": f"No quote data for {symbol}"}

    try:
        asset = Asset.get_or_create_from_symbol(symbol)
        asset.last_price = Decimal(str(data["ltp"]))
        asset.save(update_fields=["last_price", "updated_at"])

        MarketQuote.objects.update_or_create(
            asset=asset,
            defaults={
                "ltp": Decimal(str(data.get("ltp", 0))),
                "bid": Decimal(str(data.get("bid", 0))),
                "ask": Decimal(str(data.get("ask", 0))),
                "volume": int(data.get("volume", 0)),
                "change": Decimal(str(data.get("change", 0))),
                "change_pct": Decimal(str(data.get("change_pct", 0))),
            },
        )
    except Exception as exc:
        logger.warning("Quote DB cache update failed | %s", exc)

    return {
        "symbol": symbol.upper(),
        "ltp": data.get("ltp", 0),
        "bid": data.get("bid", 0),
        "ask": data.get("ask", 0),
        "volume": data.get("volume", 0),
        "change": data.get("change", 0),
        "change_pct": data.get("change_pct", 0),
        "cached": False,
        "source": broker_slug,
    }


def fetch_bulk_quotes(symbols: List[str], user, broker_slug: str = "fyers") -> Dict:
    from django.core.cache import cache

    from apps.brokers.models import BrokerAccount
    from broker_adapters.registry import BrokerRegistry

    from .delta_service import fetch_delta_tickers_bulk, is_crypto_symbol
    from .models import Asset, MarketQuote

    if not symbols:
        return {}

    if not user or not user.is_authenticated:
        logger.warning("Anonymous user — returning DB fallback only")
        return _get_fallback_quotes(symbols, {})

    cache_key = f"bulk_quotes:{user.id}:{','.join(sorted(symbols))}"

    try:
        cached = cache.get(cache_key)
        if cached:
            logger.info("✅ Serving %d quotes from cache", len(symbols))
            return cached
    except Exception as e:
        logger.warning("Cache error: %s", e)

    crypto_syms = [s for s in symbols if is_crypto_symbol(s)]
    indian_syms = [s for s in symbols if not is_crypto_symbol(s)]

    result = {}

    # ── Crypto bulk fetch ─────────────────────────────────────
    if crypto_syms:
        try:
            delta_quotes = fetch_delta_tickers_bulk(crypto_syms)
            result.update(delta_quotes)
            logger.info("✅ Fetched %d crypto quotes", len(delta_quotes))
        except Exception as exc:
            logger.error("Delta bulk fetch failed: %s", exc)

    # ── Indian bulk fetch ─────────────────────────────────────
    if indian_syms:
        try:
            cred = BrokerAccount.objects.filter(
                user=user,
                broker=broker_slug,
                is_active=True,
                is_verified=True,
            ).first()

            if not cred:
                logger.warning("No broker account: %s — using DB fallback", broker_slug)
                return _get_fallback_quotes(indian_syms, result)

            if not cred.access_token:
                logger.error("No access token for bulk quotes")
                return _get_fallback_quotes(indian_syms, result)

            # ✅ BrokerRegistry.make(slug, credentials) — sahi call
            adapter = BrokerRegistry.make(
                broker_slug,
                {
                    "access_token": cred.access_token,
                    "app_id": cred.app_id,
                    "secret_key": cred.secret_key,
                },
            )

            bulk_quotes = adapter.get_bulk_quotes(indian_syms)
            result.update(bulk_quotes)
            logger.info("✅ Fetched %d Indian quotes in bulk", len(bulk_quotes))

            # DB cache update
            for symbol, data in bulk_quotes.items():
                try:
                    asset = Asset.get_or_create_from_symbol(symbol)
                    asset.last_price = Decimal(str(data.get("ltp", 0)))
                    asset.save(update_fields=["last_price", "updated_at"])

                    MarketQuote.objects.update_or_create(
                        asset=asset,
                        defaults={
                            "ltp": Decimal(str(data.get("ltp", 0))),
                            "bid": Decimal(str(data.get("bid", 0))),
                            "ask": Decimal(str(data.get("ask", 0))),
                            "volume": int(data.get("volume", 0)),
                            "change": Decimal(str(data.get("change", 0))),
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
                "symbol": asset.symbol,
                "ltp": float(quote.ltp),
                "bid": float(quote.bid),
                "ask": float(quote.ask),
                "volume": quote.volume,
                "change": float(quote.change),
                "change_pct": float(quote.change_pct),
                "cached": True,
                "source": "db_fallback",
            }
            logger.info("📦 Using cached quote for %s", symbol)
        except Exception:
            pass

    return result
