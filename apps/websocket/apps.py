import logging
import os
from django.apps import AppConfig
logger = logging.getLogger(__name__)

IS_CELERY = bool(os.environ.get('CELERY_WORKER_RUNNING'))

class WebsocketConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.websocket"

    def ready(self):
        # Celery workers mein feeds start mat karo
        if IS_CELERY:
            logger.info("⏭️  Celery worker — skipping feed auto-start")
            return

        import threading
        import time

        def start_feeds():
            time.sleep(3)
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
            try:
                from apps.websocket.fyers_feed import _start_feed_subscribe_listener
                _start_feed_subscribe_listener()
                logger.info("🚀 feed:subscribe Redis listener started")
            except Exception as e:
                logger.error(f"feed:subscribe listener error: {e}")

        threading.Thread(target=start_feeds, daemon=True).start()
