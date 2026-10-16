"""Connection recovery and batch failure policy without hardware."""
import asyncio
import contextlib
import io
from pathlib import Path
import sys
import tempfile
import threading
import time
import tomllib
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from modules.thermal_printer import ThermalPrinterService
from modules.printer_settings import printer_settings, save_printer
from modules import printer_menu


class TransmissionError(Exception):
    may_have_printed = True


class FakePrinter:
    def __init__(self):
        self.alive = False
        self.connects, self.jobs, self.loops = [], [], []
        self.fail_address = ''
        self.fail_print = False
        self.started = threading.Event()
        self.release = threading.Event()
        self.release.set()

    async def connect(self, address):
        self.loops.append(asyncio.get_running_loop())
        self.connects.append(address)
        if address == self.fail_address: raise OSError('Not available')
        self.alive = True
        return SimpleNamespace(address=address, name='Термопринтер', calibration=False)

    async def disconnect(self): self.alive = False

    async def check_connection(self):
        self.loops.append(asyncio.get_running_loop())
        return self.alive

    async def print_id(self, account_id, name, **kwargs):
        self.loops.append(asyncio.get_running_loop())
        self.jobs.append((account_id, name, kwargs))
        self.started.set()
        while not self.release.is_set(): await asyncio.sleep(.01)
        if self.fail_print:
            self.alive = False
            raise TransmissionError('Link lost')
        return SimpleNamespace(copies=kwargs['copies'])


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.fake, self.saver, self.service = FakePrinter(), Mock(), None

    def tearDown(self):
        self.fake.release.set()
        if self.service:
            self.service.close()
            self.assertFalse(self.service._thread and self.service._thread.is_alive())

    def start(self, **settings):
        self.service = ThermalPrinterService(dict(address='printer-A', connection_check_seconds=.1,
            reconnect_delay_seconds=.1, **settings), '/tmp/test-config.toml',
            factory=lambda: (SimpleNamespace(scan=self.scan), self.fake), saver=self.saver)
        self.service.start()
        return self.service

    async def scan(self): return [dict(name='Термопринтер', address='printer-B')]

    def eventually(self, predicate):
        end = time.monotonic() + 3
        while not predicate():
            if time.monotonic() >= end: self.fail(f'Timed out: {self.service.status}')
            time.sleep(.01)

    def test_idle_disconnect_reconnects_same_device(self):
        service = self.start()
        self.eventually(lambda: service.status.state == 'connected')
        self.fake.alive = False
        self.eventually(lambda: len(self.fake.connects) == 2 and service.status.state == 'connected')
        self.assertEqual(self.fake.connects, ['printer-A', 'printer-A'])
        self.assertEqual(self.fake.jobs, [])
        self.saver.assert_not_called()
        self.assertEqual(len(set(self.fake.loops)), 1)

    def test_failed_batch_is_discarded_after_reconnection(self):
        service = self.start()
        self.eventually(lambda: service.status.state == 'connected')
        self.fake.fail_print = True
        with self.assertRaisesRegex(RuntimeError, 'Автоматического повтора'):
            service.print_label(mode='blank-id-name', copies=10).result(2)
        self.eventually(lambda: len(self.fake.connects) == 2 and service.status.state == 'connected')
        self.assertEqual(len(self.fake.jobs), 1)
        self.assertEqual(self.fake.jobs[0][2]['copies'], 10)
        self.fake.fail_print = False
        service.print_label('123456789', mode='id-only').result(2)
        self.assertEqual(len(self.fake.jobs), 2)

    def test_no_print_queue_and_manual_disconnect_pauses_retry(self):
        service = self.start()
        self.eventually(lambda: service.status.state == 'connected')
        self.fake.release.clear()
        future = service.print_label(mode='blank-id-only', copies=4)
        self.assertTrue(self.fake.started.wait(2))
        with self.assertRaisesRegex(RuntimeError, 'уже выполняет'):
            service.print_label(mode='blank-id-only')
        self.fake.release.set()
        future.result(2)
        service.disconnect().result(2)
        with self.assertRaisesRegex(RuntimeError, 'не подключён'):
            service.print_label(mode='blank-id-only')
        time.sleep(.25)
        self.assertEqual(self.fake.connects, ['printer-A'])
        service.reconnect().result(2)
        self.assertEqual(self.fake.connects, ['printer-A', 'printer-A'])
        self.assertEqual(len(self.fake.jobs), 1)

    def test_select_remembers_only_success(self):
        service = self.start(auto_connect=False)
        self.assertEqual(service.scan().result(2)[0]['address'], 'printer-B')
        self.fake.fail_address = 'bad'
        with self.assertRaises(OSError): service.select('bad').result(2)
        self.assertEqual(service.settings['address'], 'printer-A')
        self.saver.assert_not_called()
        service.select('printer-B').result(2)
        self.saver.assert_called_once_with(Path('/tmp/test-config.toml').resolve(), 'printer-B', 'Термопринтер')
        self.fake.alive = False
        self.eventually(lambda: self.fake.connects.count('printer-B') == 2)

    def test_disabled_reconnection(self):
        service = self.start(auto_reconnect=False)
        self.eventually(lambda: service.status.state == 'connected')
        self.fake.alive = False
        self.eventually(lambda: service.status.state == 'disconnected')
        with self.assertRaises(RuntimeError): service.print_label(mode='blank-id-only')
        time.sleep(.25)
        self.assertEqual(self.fake.connects, ['printer-A'])
        self.assertEqual(self.fake.jobs, [])

    def test_missing_optional_library_does_not_raise_on_start(self):
        def fail(): raise ImportError('dependency missing')
        self.service = ThermalPrinterService({}, '/tmp/config.toml', factory=fail)
        self.service.start()
        self.assertEqual(self.service.status.state, 'unavailable')


class SettingsAndMenuTests(unittest.TestCase):
    def test_preserves_comments_and_other_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'config.toml'
            path.write_text('# keep\n[database]\nhost = "example" # keep too\n\n[printer]\nblackening = 4\n', encoding='utf-8')
            save_printer(path, 'AA:BB', 'Термопринтер "А"')
            text = path.read_text(encoding='utf-8')
            parsed = tomllib.loads(text)
            self.assertIn('host = "example" # keep too', text)
            self.assertEqual(parsed['printer']['blackening'], 4)
            self.assertEqual(parsed['printer']['name'], 'Термопринтер "А"')

    def test_config_validation(self):
        self.assertEqual(printer_settings({})['scan_timeout_seconds'], 15)
        for raw in ({'max_copies': True}, {'scan_timeout_seconds': float('nan')},
                    {'paper': {'type': 'fake'}}, {'paper': {'width_mm': 58}},
                    {'layout': {'decorations': 'true'}}):
            with self.assertRaises(ValueError): printer_settings(raw)

    def test_template_count_is_one_call(self):
        service = Mock(settings={'max_copies':100})
        ask = Mock(side_effect=['2', '10'])
        with patch.object(printer_menu, 'print_label') as printing, contextlib.redirect_stdout(io.StringIO()):
            printer_menu.templates(service, ask)
        printing.assert_called_once_with(service, mode='blank-id-only', copies=10)
        self.assertEqual(ask.call_count, 2)

    def test_quit_during_device_selection_propagates(self):
        import cyberwall_master_manager as app
        from concurrent.futures import Future
        future = Future()
        future.set_result([dict(name='Термопринтер', address='A')])
        service = Mock(status=SimpleNamespace(state='connected', address='A', busy=False, detail=''))
        service.scan.return_value = future
        ask = Mock(side_effect=['1', app.QuitRequested()])
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(app.QuitRequested):
            printer_menu.printer_menu(service, ask)
        service.select.assert_not_called()

    def test_account_actions_use_fresh_external_id(self):
        import cyberwall_master_manager as app
        old = app.Entity('account', 123456789, 'Старое имя', card_id=999)
        fresh = app.Entity('account', 987654321, 'Новое имя', card_id=999)
        for command, mode, name in [('13', 'id-name', 'Новое имя'), ('14', 'id-only', None)]:
            db = Mock()
            db.refresh.side_effect = [old, fresh, fresh]
            db.access_table.return_value = []
            with patch.object(app, 'recover_pending_id'), \
                 patch.object(app, 'read_input', side_effect=[command, '0']), \
                 patch.object(app.printer_menu, 'print_label') as printing, \
                 contextlib.redirect_stdout(io.StringIO()):
                app.inspect_entity(db, old)
            printing.assert_called_once_with(app.THERMAL_PRINTER, '987654321', name, mode=mode)
