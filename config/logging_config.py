import logging
from pathlib import Path

_logging_initialized = False


def setup_logging():
    global _logging_initialized

    if _logging_initialized:
        return

    try:
        from django.conf import settings
        log_dir = Path(settings.BASE_DIR) / 'logs'
    except:
        log_dir = Path(__file__).parent.parent / 'logs'

    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / 'app.log'

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    root_logger.handlers.clear()

    # Console handler (same as before)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter(
        '%(levelname)s %(asctime)s [%(name)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    ))

    try:
        # ✅ Windows-safe rotation
        from concurrent_log_handler import ConcurrentRotatingFileHandler

        file_handler = ConcurrentRotatingFileHandler(
            log_file,
            maxBytes=5 * 1024 * 1024,  # 5 MB
            backupCount=3,              # app.log + 3 backup files
            encoding='utf-8',
            delay=True
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(
            '%(levelname)s %(asctime)s,%(msecs)03d [%(name)s:%(lineno)d] %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        ))

        root_logger.addHandler(file_handler)
        root_logger.addHandler(console_handler)
        _logging_initialized = True
        logging.info("✅ Logging initialized (concurrent rotation)")

    except ImportError:
        # concurrent-log-handler install nahi hai — simple FileHandler fallback
        logging.warning("⚠️ concurrent-log-handler not found. Run: pip install concurrent-log-handler")
        file_handler = logging.FileHandler(log_file, mode='a', encoding='utf-8', delay=True)
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(
            '%(levelname)s %(asctime)s,%(msecs)03d [%(name)s:%(lineno)d] %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        ))
        root_logger.addHandler(file_handler)
        root_logger.addHandler(console_handler)
        _logging_initialized = True

    except Exception as e:
        root_logger.addHandler(console_handler)
        _logging_initialized = True
        logging.error(f"⚠️ File logging failed: {e}")

    return root_logger


def shutdown_logging():
    """Close all handlers"""
    root_logger = logging.getLogger()
    for handler in root_logger.handlers[:]:
        handler.close()
        root_logger.removeHandler(handler)