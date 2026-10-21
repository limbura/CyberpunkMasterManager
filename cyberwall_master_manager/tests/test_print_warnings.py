import contextlib
import io
import unittest
import warnings
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from modules.thermal_printer import ThermalPrinterService, RemovalRequired
from modules import printer_menu

class UnsupportedNameCharactersWarning(UserWarning):
    pass


MESSAGE = 'В имени обнаружены недопустимые символы, они заменены на нижнее подчеркивание'


class WarningTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_print_paths_capture_warning_and_reset(self):
        for labels in (False, True):
            service = ThermalPrinterService({}, '/tmp/config.toml')
            service._unsupported_name_warning = UnsupportedNameCharactersWarning
            service._update(state='connected')
            service._submit = lambda action, **kw: action()
            printer = SimpleNamespace(check_connection=AsyncMock(return_value=True),
                print_image=AsyncMock(return_value=SimpleNamespace(copies=1)), media=object())
            service._printer = printer
            def render(*args, **kwargs):
                warnings.warn('original', UnsupportedNameCharactersWarning)
                warnings.warn('original again', UnsupportedNameCharactersWarning)
                return 'image'
            async def print_id(*args, **kwargs):
                render()
                return SimpleNamespace(copies=1)
            printer.print_id = print_id
            if labels:
                service._single_printer = printer
                service._render = render
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter('always')
                result = await service.print_label('123456789', 'А@')
            self.assertEqual(result.copies, 1)
            self.assertEqual(caught, [])
            self.assertEqual(service.status.print_warnings, (MESSAGE,))
            if labels:
                printer.print_image.assert_awaited_once_with('image')
                service._render = Mock(return_value='image')
            else:
                printer.print_id = AsyncMock(return_value=SimpleNamespace(copies=1))
            await service.print_label('123456789', 'А')
            self.assertEqual(service.status.print_warnings, ())

    async def test_unrelated_warning_and_error_are_preserved(self):
        service = ThermalPrinterService({}, '/tmp/config.toml')
        service._unsupported_name_warning = UnsupportedNameCharactersWarning
        service._update(state='connected')
        service._submit = lambda action, **kw: action()
        async def printing(*args, **kw):
            warnings.warn('other', RuntimeWarning)
            warnings.warn('unsupported', UnsupportedNameCharactersWarning)
            raise ValueError('failed')
        service._printer = SimpleNamespace(check_connection=AsyncMock(return_value=True),
                                            print_id=printing)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            with self.assertRaisesRegex(ValueError, 'failed'):
                await service.print_label('123456789', 'А@')
        self.assertEqual([w.category for w in caught], [RuntimeWarning])
        self.assertEqual(service.status.print_warnings, (MESSAGE,))


class MenuWarningTests(unittest.TestCase):
    def test_message_once_per_user_print_including_removal_retry(self):
        for retry in (False, True):
            service = SimpleNamespace(status=SimpleNamespace(print_warnings=(MESSAGE,)))
            success = Future()
            success.set_result(SimpleNamespace(copies=1))
            pending = Future()
            pending.set_exception(RemovalRequired())
            service.print_label = Mock(side_effect=[pending, success] if retry else [success])
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertTrue(printer_menu.print_label(service, '123456789', 'А@',
                                                        ask=lambda _: ''))
            self.assertEqual(output.getvalue().count(MESSAGE), 1)
            self.assertIn('Серия отправлена', output.getvalue())
