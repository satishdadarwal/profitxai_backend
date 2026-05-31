import logging

from django.apps import AppConfig

logger = logging.getLogger(__name__)


class WebsocketConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.websocket"

    def ready(self):
        import threading
        import time

        def start_feeds():
            time.sleep(3)  # Wait for DB

            # ── Fyers feed (NSE/BSE indices + equities) ──────────────
            try:
                from apps.websocket.fyers_feed import feed_manager
                feed_manager.start()
                logger.info("🚀 Fyers feed auto-started")
            except Exception as e:
                logger.error(f"Fyers auto-start error: {e}")

            # ── Delta feed (Crypto — BTC, ETH, SOL etc.) ─────────────
            try:
                from apps.websocket.delta_feed import delta_feed_manager
                delta_feed_manager.start()
                logger.info("🚀 Delta feed auto-started")
            except Exception as e:
                logger.error(f"Delta auto-start error: {e}")

            # ── Dhan feed (NSE/BSE indices via Dhan LTP API) ──────────
            # NOTE: Dhan feed sirf tab kaam karta hai jab:
            #   1. BrokerAccount(broker="dhan", is_active=True) DB mein ho
            #   2. DHAN_MASTER_CLIENT_ID .env mein set ho
            #   3. VPS IP web.dhan.co pe whitelist ho
            try:
                from apps.websocket.dhan_feed import dhan_feed_manager

                INDEX_SYMBOLS = [
                    "NSE:NIFTY50-INDEX",
                    "NSE:NIFTYBANK-INDEX",
                    "BSE:SENSEX-INDEX",
                    "NSE:FINNIFTY-INDEX",
                    "NSE:MIDCPNIFTY-INDEX",
                ]
                for sym in INDEX_SYMBOLS:
                    dhan_feed_manager.subscribe(sym)  # subscribe() start() bhi karta hai

                logger.info("🚀 Dhan feed auto-started | symbols=%d", len(INDEX_SYMBOLS))
            except Exception as e:
                logger.error(f"Dhan auto-start error: {e}")

        thread = threading.Thread(target=start_feeds, daemon=True)
        thread.start()