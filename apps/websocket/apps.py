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

            try:
                from apps.websocket.fyers_feed import feed_manager

                feed_manager.start()
                logger.info("🚀 Fyers feed auto-started")
            except Exception as e:
                logger.error(f"Fyers auto-start error: {e}")

            try:
                from apps.websocket.delta_feed import delta_feed_manager

                delta_feed_manager.start()
                logger.info("🚀 Delta feed auto-started")
            except Exception as e:
                logger.error(f"Delta auto-start error: {e}")

        thread = threading.Thread(target=start_feeds, daemon=True)
        thread.start()
