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
        printing.assert_called_once_with(service, mode='blank-id-only', copies=10, ask=ask)
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
        for command, mode, name in [('1', 'id-name', 'Новое имя'), ('2', 'id-only', None)]:
            db = Mock()
            db.refresh.side_effect = [old, fresh, fresh]
            db.access_table.return_value = []
            with patch.object(app, 'recover_pending_id'), \
                 patch.object(app, 'read_input', side_effect=['13', command, '0']) as ask, \
                 patch.object(app.printer_menu, 'print_label') as printing, \
                 contextlib.redirect_stdout(io.StringIO()):
                app.inspect_entity(db, old)
            printing.assert_called_once_with(app.THERMAL_PRINTER, '987654321', name, mode=mode, ask=ask)

    def test_print_submenu_cancel_never_prints(self):
        import cyberwall_master_manager as app
        account = app.Entity('account', 123456789, 'Имя', card_id=999)
        db = Mock()
        db.refresh.return_value = account
        db.access_table.return_value = []
        with patch.object(app, 'recover_pending_id'), \
             patch.object(app, 'read_input', side_effect=['13', '0', '0']), \
             patch.object(app.printer_menu, 'print_label') as printing, \
             contextlib.redirect_stdout(io.StringIO()):
            app.inspect_entity(db, account)
        printing.assert_not_called()

    def test_common_menu_routes_numeric_actions(self):
        import cyberwall_master_manager as app
        db = Mock()
        with patch.object(app, 'read_input', side_effect=[':menu', '3', '4', '0', ':b']), \
             patch.object(app.printer_menu, 'templates') as templates, \
             patch.object(app.printer_menu, 'printer_menu') as devices, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(app.choose_candidate(db))
        templates.assert_called_once()
        devices.assert_called_once()
        db.search.assert_not_called()

class LabelRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from unittest.mock import AsyncMock
        self.standard = FakePrinter()
        self.single = SimpleNamespace(
            media=object(), connect=AsyncMock(return_value=SimpleNamespace(address='A', name='P', calibration=False)),
            disconnect=AsyncMock(), check_connection=AsyncMock(return_value=True),
            print_image=AsyncMock(return_value=SimpleNamespace(copies=1)),
            confirm_removed=AsyncMock(), recover=AsyncMock())
        self.service = ThermalPrinterService({}, '/tmp/config.toml')
        self.service._single_printer = self.single
        self.service._standard_printer = self.standard
        self.service._printer = self.single
        self.service._render = Mock(return_value='image')
        self.service._update(state='connected', address='A')
        self.service._submit = lambda action, **kwargs: action()

    async def test_single_batch_single_switches_sessions(self):
        service = self.service
        await service.print_label('123456789', mode='id-only')
        self.single.print_image.assert_awaited_once_with('image')
        self.single.confirm_removed.assert_not_awaited()
        await service.print_label(mode='blank-id-only', copies=3)
        self.assertEqual(self.standard.jobs[0][2]['copies'], 3)
        self.single.disconnect.assert_awaited()
        await service.print_label(mode='blank-id-name')
        self.single.connect.assert_awaited_once_with('A')
        self.assertFalse(self.standard.alive)
        self.assertEqual(self.single.print_image.await_count, 2)

    async def test_confirmation_only_after_explicit_user_action(self):
        from modules.thermal_printer import RemovalRequired
        self.single.print_image.side_effect = [RemovalRequired(), SimpleNamespace(copies=1)]
        with self.assertRaises(RemovalRequired):
            await self.service.print_label('123456789')
        self.single.confirm_removed.assert_not_awaited()
        await self.service.print_label('123456789', removed=True)
        self.single.confirm_removed.assert_awaited_once()

    async def test_manual_calibration_uses_forward_recovery(self):
        await self.service.calibrate()
        self.single.recover.assert_awaited_once()
        self.single.confirm_removed.assert_not_awaited()


class RemovalMenuTests(unittest.TestCase):
    def test_confirm_or_cancel(self):
        from concurrent.futures import Future
        from modules.thermal_printer import RemovalRequired
        for answer, count in [('', 2), (':b', 1)]:
            first, second = Future(), Future()
            first.set_exception(RemovalRequired())
            second.set_result(SimpleNamespace(copies=1))
            service = Mock()
            service.print_label.side_effect = [first, second]
            with contextlib.redirect_stdout(io.StringIO()):
                printer_menu.print_label(service, '123456789', ask=Mock(return_value=answer))
            self.assertEqual(service.print_label.call_count, count)
            if count == 2:
                self.assertTrue(service.print_label.call_args.kwargs['removed'])

class MaintenanceMenuTests(unittest.TestCase):
    def test_reverse_confirmation_and_cancel(self):
        from concurrent.futures import Future
        for answer, expected in [('', 1), (':b', 0)]:
            service = Mock()
            future = Future()
            future.set_result(None)
            service.move_paper.return_value = future
            with contextlib.redirect_stdout(io.StringIO()):
                printer_menu.maintenance(service, '5 10', Mock(return_value=answer))
            self.assertEqual(service.move_paper.call_count, expected)
            if expected:
                service.move_paper.assert_called_once_with(10.0, reverse=True, removed=True)

    def test_invalid_motion_never_sent(self):
        service = Mock()
        with contextlib.redirect_stdout(io.StringIO()):
            for command in ('4', '5 nan', '4 26', '5 -1', '4 10 extra'):
                printer_menu.maintenance(service, command, Mock())
        service.move_paper.assert_not_called()

    def test_account_menu_keeps_open_after_calibration(self):
        from concurrent.futures import Future
        future = Future()
        future.set_result(None)
        service = Mock(status=SimpleNamespace(state='connected', label_state='ready'))
        service.calibrate.return_value = future
        with contextlib.redirect_stdout(io.StringIO()) as out:
            mode = printer_menu.choose_label_mode(Mock(side_effect=['3', '1']), service)
        self.assertEqual(mode, 'id-name')
        service.calibrate.assert_called_once()
        self.assertIn('Печать: Готово к печати', out.getvalue())
