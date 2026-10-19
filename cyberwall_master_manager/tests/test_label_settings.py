"""Label configuration compatibility, validation and API forwarding."""
from dataclasses import dataclass
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from modules.printer_settings import printer_settings
from modules.thermal_printer import ThermalPrinterService


class LabelSettingsTests(unittest.TestCase):
    def test_old_and_partial_config(self):
        self.assertEqual(printer_settings({})['labels']['feed_mm'], 15)
        settings = printer_settings({'labels': {'feed_mm': 10}})
        self.assertEqual(settings['labels']['feed_mm'], 10)
        self.assertEqual(settings['labels']['retract_mm'], 25)
        self.assertEqual(printer_settings({})['labels']['feed_mm'], 15)

    def test_invalid_values(self):
        for key, value in [('feed_mm', 26), ('retract_mm', 0),
                           ('settle_seconds', 1), ('motion_settle_seconds', True),
                           ('position_timeout', float('nan')), ('position_timeout', '20'),
                           ('unknown', 3)]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                printer_settings({'labels': {key: value}})

    def test_settings_reach_label_printer(self):
        @dataclass
        class Media:
            paper_type: str
        values = dict(feed_mm=10, retract_mm=12, position_timeout=30,
                      settle_seconds=2, motion_settle_seconds=4)
        api = SimpleNamespace(Printer=Mock(), TiMiniBackend=Mock(),
            MediaConfig=lambda **kw: Media(kw['paper_type']),
            LabelPrinter=Mock(), LabelSettings=Mock(return_value='label-settings'),
            RemovalRequired=RuntimeError, DeviceStatusError=RuntimeError, PaperType=SimpleNamespace(CONTINUOUS='continuous'))
        with patch.dict(sys.modules, {'cyberwall_id_printer': api,
                         'cyberwall_id_printer.layout': SimpleNamespace(render_for_head=Mock())}):
            service = ThermalPrinterService({'library_path': '', 'paper': {'type': 'labels'},
                                             'labels': values}, '/tmp/config.toml')
            service._create()
        api.LabelSettings.assert_called_once_with(**values)
        self.assertEqual(api.LabelPrinter.call_args.kwargs['settings'], 'label-settings')
