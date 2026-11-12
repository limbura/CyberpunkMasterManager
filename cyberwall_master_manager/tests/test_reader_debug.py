"""Проверки файлового журнала RFID без реального оборудования."""
import logging
from pathlib import Path
import tempfile
import unittest

from test_cyberwall_master_manager import console
from modules.reader_debug import configure_reader_debug_logging, close_reader_debug_logging


class ReaderDebugLoggingTests(unittest.TestCase):
    def tearDown(self):
        close_reader_debug_logging()

    def test_debug_writes_poll_and_reader_transport_to_rotating_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "reader_debug.log"
            try:
                configure_reader_debug_logging(True, path=path)
                logging.getLogger("modules.card_console").debug("poll duration_ms=123.4")
                logging.getLogger("wcopy_reader").debug("poll CYB TX 55 00 69")
                self.assertTrue(path.exists())
                contents = path.read_text(encoding="utf-8")
                self.assertIn("duration_ms=123.4", contents)
                self.assertIn("CYB TX", contents)
            finally:
                close_reader_debug_logging()

    def test_disabled_does_not_create_log(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "reader_debug.log"
            self.assertIsNone(configure_reader_debug_logging(False, path=path))
            self.assertFalse(path.exists())

    def test_logger_levels_are_restored_after_close(self):
        name = "wcopy_reader"
        logger = logging.getLogger(name)
        original = logger.level
        with tempfile.TemporaryDirectory() as temp:
            try:
                configure_reader_debug_logging(True, path=Path(temp) / "reader_debug.log")
                self.assertEqual(logger.level, logging.DEBUG)
            finally:
                close_reader_debug_logging()
        self.assertEqual(logger.level, original)


if __name__ == "__main__":
    unittest.main()
