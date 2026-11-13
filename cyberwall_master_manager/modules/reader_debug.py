"""Отладочный журнал RFID: отдельный файл рядом с EXE и ограничение размера."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .settings import APP_DIR

_LOGGER_NAMES = ("wcopy_reader", "modules.card_console")
_MAX_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 3
_handler: RotatingFileHandler | None = None
_old_levels: dict[str, int] = {}


def configure_reader_debug_logging(enabled: bool, *, path: Path | None = None) -> Path | None:
    """Подключить DEBUG обмена RFID и фонового опроса; не менять root logger."""
    global _handler, _old_levels
    close_reader_debug_logging()
    if not enabled:
        return None
    log_path = Path(path) if path is not None else APP_DIR / "reader_debug.log"
    handler = RotatingFileHandler(log_path, maxBytes=_MAX_BYTES,
                                  backupCount=_BACKUP_COUNT, encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s.%(msecs)03d [%(levelname)s] [%(threadName)s] "
        "%(name)s: %(message)s", datefmt="%d.%m.%Y %H:%M:%S"))
    old_levels = {}
    for name in _LOGGER_NAMES:
        logger = logging.getLogger(name)
        old_levels[name] = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
    _old_levels = old_levels
    _handler = handler
    logging.getLogger("modules.card_console").debug(
        "Журнал считывателя включён: %s (ротация: %d MiB, %d копии)",
        log_path, _MAX_BYTES // 1024 // 1024, _BACKUP_COUNT)
    return log_path


def close_reader_debug_logging() -> None:
    """Закрыть файл и восстановить уровни сторонних логгеров."""
    global _handler, _old_levels
    if _handler is None:
        return
    for name, level in _old_levels.items():
        logger = logging.getLogger(name)
        logger.removeHandler(_handler)
        logger.setLevel(level)
    _handler.close()
    _handler = None
    _old_levels = {}
