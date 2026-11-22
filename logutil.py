"""轻量滚动日志：写入应用目录下的 logs/app.log。"""
import logging
import os
import threading
from logging.handlers import RotatingFileHandler

import config

_lock = threading.Lock()
_logger = None


def get_logger():
    global _logger
    with _lock:
        if _logger is None:
            log_dir = os.path.join(config._APP_DIR, "logs")
            os.makedirs(log_dir, exist_ok=True)
            logger = logging.getLogger("video_download")
            logger.setLevel(logging.INFO)
            logger.propagate = False
            handler = RotatingFileHandler(
                os.path.join(log_dir, "app.log"), maxBytes=2 * 1024 * 1024,
                backupCount=3, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            logger.addHandler(handler)
            _logger = logger
        return _logger


def info(msg):
    try:
        get_logger().info(msg)
    except Exception:
        pass
