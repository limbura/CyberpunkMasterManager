"""Автономные сценарные тесты. Настоящие MySQL/USB не используются."""
import contextlib
import io
import queue
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Позволяет проверить логику даже на машине без установленного MySQL-драйвера.
try:
    import pymysql
except ImportError:
    pymysql = types.ModuleType('pymysql')
    class MySQLError(Exception):
        pass
    pymysql.MySQLError = MySQLError
    cursors = types.ModuleType('pymysql.cursors')
    cursors.DictCursor = object
    sys.modules['pymysql'] = pymysql
    sys.modules['pymysql.cursors'] = cursors
import cyberwall_master_manager as app
from modules import card_console as console

ACCOUNT = app.Entity('account', 123456789, 'Тестовый аккаунт', 9223372036854775901, 12500, 800, 3, 1)


class Cursor:
    def __init__(self, row=None, fail_update=False):
        self.row = row
        self.calls = []
        self.fail_update = fail_update
        self.remaining = 2
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def execute(self, sql, args=()):
        self.calls.append((sql, args))
        if self.fail_update and sql.startswith('UPDATE'):
            raise pymysql.MySQLError(2013, 'test disconnect')
    def fetchone(self):
        return self.row
    def fetchall(self):
        return [self.row] if self.row is not None else []
    def nextset(self):
        self.remaining -= 1
        return self.remaining >= 0


def connection(cursor):
    return types.SimpleNamespace(cursor=lambda: cursor, begin=Mock(), commit=Mock(), rollback=Mock(),
                                 open=True, host='test', port=3306, db=b'cyberwall')


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.output)
        self.redirect.__enter__()
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'pending_id_change.json'
        self.pending_patch = patch.object(app, 'PENDING_ID_PATH', self.path)
        self.pending_patch.start()
        self.console_patch = patch.object(app, 'CONSOLE', None)
        self.console_patch.start()
    def tearDown(self):
        self.console_patch.stop()
        self.pending_patch.stop()
        self.tmp.cleanup()
        self.redirect.__exit__(None, None, None)
    def fake_db(self):
        db = Mock()
        db.refresh.return_value = ACCOUNT
        db.account_by_card.return_value = ACCOUNT
        db.connection = connection(Cursor())
        return db
    def test_table_fields_and_large_card_id(self):
        table = app.make_entity_table([ACCOUNT])
        text = table.header + "\n" + table.lines[0]
        for token in ('АККАУНТ', 'деньги', 'рейт', 'ур. деки', 'полиц. права', '12 500', '9 223 372 036 854 775 901'):
            self.assertIn(token, text)
        self.assertNotIn('ЧЕЛОВЕК', text)
    def test_numeric_validation(self):
        for field, valid, bad in [('money', -2147483648, 2147483648), ('losa', 2147483647, -2147483649), ('netrunner_lvl', 255, 256), ('police', 1, 2)]:
            app.validate_field(field, valid)
            with self.assertRaises(ValueError):
                app.validate_field(field, bad)
        with self.assertRaises(ValueError):
            app.validate_field('id', 123456789)
        with self.assertRaises(ValueError):
            app.validate_field('name', '')
    def test_scan_from_search_uses_card_id(self):
        db = self.fake_db()
        with patch.object(app, 'read_input', return_value=console.ScannedCard(ACCOUNT.card_id)):
            self.assertEqual(app.choose_candidate(db), ACCOUNT)
        db.account_by_card.assert_called_once_with(ACCOUNT.card_id)
        db.search.assert_not_called()
    def test_scan_from_candidates(self):
        db = self.fake_db()
        db.search.return_value = [ACCOUNT]
        with patch.object(app, 'read_input', side_effect=['тест', console.ScannedCard(ACCOUNT.card_id)]):
            self.assertEqual(app.choose_candidate(db, 'account'), ACCOUNT)
    def test_unknown_card_can_return_to_keyboard(self):
        db = self.fake_db()
        db.account_by_card.return_value = None
        with patch.object(app, 'read_input', side_effect=[console.ScannedCard(7), ':b']):
            self.assertIsNone(app.choose_candidate(db))
        self.assertIn('не найден', self.output.getvalue())
    def test_locks_search_does_not_enable_scan(self):
        db = self.fake_db()
        with patch.object(app, 'read_input', return_value=':b') as read:
            app.choose_candidate(db, 'lock')
        self.assertFalse(read.call_args.args[1])
    def test_parameterized_update_and_commit(self):
        cursor = Cursor({'value': 'старое'})
        conn = connection(cursor)
        malicious = "имя';--"
        app.Database(conn).set_field(ACCOUNT.card_id, 'name', 'старое', malicious)
        sql, args = cursor.calls[-1]
        self.assertNotIn(malicious, sql)
        self.assertEqual(args, (malicious, ACCOUNT.card_id))
        conn.commit.assert_called_once()
    def test_concurrent_change_cancels_update(self):
        cursor = Cursor({'value': 900})
        conn = connection(cursor)
        with self.assertRaises(app.SelectionChanged):
            app.Database(conn).set_field(ACCOUNT.card_id, 'money', 100, 200)
        self.assertFalse(any(sql.startswith('UPDATE') for sql, _ in cursor.calls))
        conn.rollback.assert_called_once()
    def test_disconnect_is_uncertain_and_not_retried(self):
        cursor = Cursor({'value': 100}, fail_update=True)
        conn = connection(cursor)
        with self.assertRaises(app.WriteUncertain):
            app.Database(conn).set_field(ACCOUNT.card_id, 'money', 100, 200)
        self.assertEqual(sum(sql.startswith('UPDATE') for sql, _ in cursor.calls), 1)
    def test_password_shown_and_leading_zeros_preserved(self):
        db = self.fake_db()
        db.field_value.return_value = '01012001'
        with patch('builtins.input', side_effect=['002233', 'да']):
            app.edit_account_field(db, ACCOUNT, 'password')
        self.assertIn('01012001', self.output.getvalue())
        db.set_field.assert_called_once_with(ACCOUNT.card_id, 'password', '01012001', '002233')
    def test_password_back_does_not_write(self):
        db = self.fake_db()
        db.field_value.return_value = '0123'
        with patch('builtins.input', return_value=':b'):
            app.edit_account_field(db, ACCOUNT, 'password')
        db.set_field.assert_not_called()
    def test_id_uses_procedure_and_drains_results(self):
        cursor = Cursor({'new_id': 876543210, 'errCode': 0})
        db = app.Database(connection(cursor))
        self.assertEqual(db.change_id(ACCOUNT.card_id), 876543210)
        calls = [x for x in cursor.calls if x[0].startswith('CALL')]
        self.assertEqual(calls, [('CALL changeId(%s, @master_new_id, @master_id_err)', (ACCOUNT.card_id,))])
        self.assertFalse(any('UPDATE' in sql for sql, _ in cursor.calls))
        self.assertEqual(cursor.remaining, -1)
    def test_id_cancel_does_not_call_procedure(self):
        db = self.fake_db()
        with patch('builtins.input', return_value=':b'):
            app.change_account_id(db, ACCOUNT)
        db.change_id.assert_not_called()
        self.assertFalse(self.path.exists())
    def test_id_requires_matching_sticker_confirmation(self):
        db = self.fake_db()
        new = app.Entity('account', 987654321, ACCOUNT.label, ACCOUNT.card_id)
        db.change_id.return_value = new.number
        db.account_by_card.return_value = new
        db.refresh.side_effect = [ACCOUNT, new]
        with patch('builtins.input', side_effect=['да', '', ':b', '111111111', '987 654 321']):
            app.change_account_id(db, ACCOUNT)
        db.change_id.assert_called_once_with(ACCOUNT.card_id)
        self.assertFalse(self.path.exists())
        self.assertIn('Подтверждено', self.output.getvalue())
    def test_id_exit_keeps_pending_and_restart_does_not_regenerate(self):
        db = self.fake_db()
        db.change_id.return_value = ACCOUNT.number
        with patch('builtins.input', side_effect=['да', ':q']):
            with self.assertRaises(app.QuitRequested):
                app.change_account_id(db, ACCOUNT)
        self.assertTrue(self.path.exists())
        db.change_id.reset_mock()
        with patch('builtins.input', return_value=str(ACCOUNT.number)):
            app.recover_pending_id(db)
        db.change_id.assert_not_called()
        self.assertFalse(self.path.exists())
    def test_id_uncertain_keeps_journal(self):
        db = self.fake_db()
        db.change_id.side_effect = pymysql.MySQLError(2013, 'lost')
        with patch('builtins.input', return_value='да'):
            with self.assertRaises(app.WriteUncertain):
                app.change_account_id(db, ACCOUNT)
        self.assertTrue(self.path.exists())
        db.change_id.assert_called_once()
    def test_journal_failure_prevents_id_generation(self):
        db = self.fake_db()
        with patch('builtins.input', return_value='да'), patch.object(app, 'save_pending_id', side_effect=OSError('read-only')):
            with self.assertRaises(OSError):
                app.change_account_id(db, ACCOUNT)
        db.change_id.assert_not_called()
    def test_write_card_uses_card_id_not_display_id(self):
        db = self.fake_db()
        device = Mock()
        device.write_number.return_value = (ACCOUNT.card_id, None)
        with patch.object(app, 'CONSOLE', device), patch.object(app, 'read_input', return_value='да'):
            app.write_account_card(db, ACCOUNT)
        device.write_number.assert_called_once_with(ACCOUNT.card_id, 10)
    def test_write_cancel_never_touches_reader(self):
        db = self.fake_db()
        device = Mock()
        with patch.object(app, 'CONSOLE', device), patch.object(app, 'read_input', return_value=':b'):
            app.write_account_card(db, ACCOUNT)
        device.write_number.assert_not_called()
    def test_access_from_lock_accepts_scanned_account(self):
        db = self.fake_db()
        lock = app.Entity('lock', 2, 'Лаборатория')
        db.refresh.side_effect = lambda e: e
        db.set_access.return_value = 0
        with patch.object(app, 'read_input', side_effect=[console.ScannedCard(ACCOUNT.card_id), 'да']):
            app.change_access(db, lock, True)
        db.set_access.assert_called_once_with(2, ACCOUNT.card_id, True)
    def test_optional_reader_falls_back_to_keyboard(self):
        with patch.object(console, 'rfid', None):
            device = console.CardConsole()
            with patch('builtins.input', return_value='Тест'):
                self.assertEqual(device.read('Поиск: ', allow_card=True), 'Тест')
            device.close()
    def test_reader_write_keeps_success_when_beep_fails(self):
        device = console.CardConsole(enabled=False)
        device.enabled = True  # Не запускаем поток в этом тесте.
        with patch.object(console.rfid, 'write_card_number', return_value=ACCOUNT.card_id) as write, patch.object(console.rfid, 'beep', side_effect=console.rfid.USBError('beep failed', stage='feedback')):
            result, warning = device.write_number(ACCOUNT.card_id)
        self.assertEqual(result, ACCOUNT.card_id)
        self.assertIn('beep failed', warning)
        write.assert_called_once_with(ACCOUNT.card_id)
    def test_stale_card_event_cannot_leak_into_next_prompt(self):
        device = console.CardConsole(enabled=False)
        first = device._start_scan()
        device._send(first, console.ScannedCard(1))
        device._stop_scan()
        second = device._start_scan()
        device._send(first, console.ScannedCard(2))
        self.assertIsNone(device._event(second))
        device._send(second, console.ScannedCard(3))
        self.assertEqual(device._event(second).number, 3)
        device.close()

    def test_hotplug_held_card_and_remove_reapply(self):
        card = types.SimpleNamespace(uid=b'1234', number=ACCOUNT.card_id)
        sequence = queue.Queue()
        for value in [console.rfid.ReaderNotFoundError('not connected', stage='enumerate'),
                      card, card, console.rfid.NoCardError('empty', stage='poll'),
                      console.rfid.NoCardError('empty', stage='poll'), card]:
            sequence.put(value)
        def read(**kwargs):
            try:
                value = sequence.get_nowait()
            except queue.Empty:
                value = card
            if isinstance(value, Exception):
                raise value
            return value
        with patch.object(console.rfid, 'read_card', side_effect=read) as reads, patch.object(console.rfid, 'set_led'), patch.object(console.rfid, 'beep') as beep:
            device = console.CardConsole()
            try:
                device._start_scan()
                first = device.events.get(timeout=4)[1]
                if isinstance(first, str):
                    first = device.events.get(timeout=4)[1]
                self.assertEqual(first.number, ACCOUNT.card_id)
                self.assertEqual(device.events.get(timeout=4)[1].number, ACCOUNT.card_id)
                self.assertGreaterEqual(reads.call_count, 6)
                beep.assert_not_called()
            finally:
                device.close()

    def test_windows_keyboard_editing_and_card_input(self):
        from modules.windows_console import KeyStroke
        keyboard = Mock()
        keyboard.poll.side_effect = [KeyStroke('а'), KeyStroke('б'), KeyStroke('\b'), KeyStroke('в'), KeyStroke('\r')]
        device = console.CardConsole(enabled=False)
        self.assertEqual(device._windows_edit('Поиск: ', 0, keyboard), 'ав')
        keyboard.poll.side_effect = None
        keyboard.poll.return_value = None
        epoch = device._start_scan()
        device._send(epoch, console.ScannedCard(ACCOUNT.card_id))
        self.assertEqual(device._windows_edit('Поиск: ', epoch, keyboard).number, ACCOUNT.card_id)
        device.close()

    def test_paste_is_redrawn_in_batches_and_stops_at_enter(self):
        from modules.windows_console import KeyStroke
        keyboard = Mock()
        keyboard.poll.side_effect = [KeyStroke('Ж')] * 200 + [KeyStroke('\r'), KeyStroke('x')]
        device = console.CardConsole(enabled=False)
        self.assertEqual(device._windows_edit('Поиск: ', 0, keyboard), 'Ж' * 200)
        self.assertEqual(keyboard.poll.call_count, 201)
        # Подсказка не перерисовывается на каждый из 200 символов.
        self.assertEqual(self.output.getvalue().count('Поиск:'), 1)
        self.assertLess(self.output.getvalue().count('\r> '), 15)
        device.close()

    def test_service_event_flood_does_not_starve_card(self):
        from modules.windows_console import KeyStroke
        keyboard = Mock()
        keyboard.poll.return_value = KeyStroke(repeat=0)
        device = console.CardConsole(enabled=False)
        epoch = device._start_scan()
        device._send(epoch, console.ScannedCard(ACCOUNT.card_id))
        result = device._windows_edit('Поиск: ', epoch, keyboard)
        self.assertEqual(result.number, ACCOUNT.card_id)
        self.assertEqual(keyboard.poll.call_count, 64)
        device.close()

    def test_search_keyboard_is_responsive_while_usb_read_is_blocked(self):
        import threading
        from modules.windows_console import KeyStroke
        entered = threading.Event()
        release = threading.Event()
        def blocked(**kwargs):
            entered.set()
            release.wait(5)
            raise console.rfid.NoCardError('empty', stage='poll')
        with patch.object(console.rfid, 'read_card', side_effect=blocked), patch.object(console.rfid, 'set_led'):
            device = console.CardConsole()
            try:
                epoch = device._start_scan()
                self.assertTrue(entered.wait(1))
                keyboard = Mock()
                keyboard.poll.side_effect = [KeyStroke('т'), KeyStroke('е'), KeyStroke('с'), KeyStroke('т'), KeyStroke('\r')]
                self.assertEqual(device._windows_edit('Поиск:', epoch, keyboard), 'тест')
                self.assertFalse(release.is_set())
            finally:
                release.set()
                device.close()

    def test_table_headers_on_every_page_with_values_only(self):
        table = app.make_entity_table([ACCOUNT] * 16)
        app.show_page(table, 0)
        app.show_page(table, 1)
        text = self.output.getvalue()
        self.assertEqual(text.count('полиц. права'), 2)
        self.assertNotIn('card_id', table.lines[0])
        self.assertNotIn('деньги', table.lines[0])
        self.assertIn('16 |', text)

    def test_police_removal_writes_null(self):
        cursor = Cursor({'value': 1})
        conn = connection(cursor)
        app.Database(conn).set_field(ACCOUNT.card_id, 'police', 1, None)
        self.assertEqual(cursor.calls[-1][1], (None, ACCOUNT.card_id))
        conn.commit.assert_called_once()
        self.assertEqual(app.format_number(None), '-')

    def test_police_zero_input_is_also_stored_as_null(self):
        db = self.fake_db()
        db.field_value.return_value = 1
        with patch('builtins.input', side_effect=['0', 'да']):
            app.edit_account_field(db, ACCOUNT, 'police')
        db.set_field.assert_called_once_with(ACCOUNT.card_id, 'police', 1, None)

    def test_wrong_sticker_id_is_explicit_error(self):
        db = self.fake_db()
        pending = {'scope': app.database_scope(db), 'card_id': ACCOUNT.card_id,
                   'old_id': 111111111, 'new_id': ACCOUNT.number}
        app.save_pending_id(pending)
        with patch('builtins.input', side_effect=['999', str(ACCOUNT.number)]):
            app.confirm_sticker(db, pending)
        text = self.output.getvalue()
        self.assertTrue(text.lstrip().startswith('ID ИЗМЕНЕН'))
        self.assertIn('ОШИБКА: введён неправильный ID', text)
        self.assertNotIn('В памяти карты', text)

    def test_empty_reader_is_not_announced_as_card_present(self):
        db = self.fake_db()
        device = Mock()
        device.write_number.side_effect = console.CardNotPresentError()
        with patch.object(app, 'CONSOLE', device), patch.object(app, 'read_input', return_value='да') as read:
            app.write_account_card(db, ACCOUNT)
        self.assertNotIn('Карта приложена', read.call_args.args[0])
        text = self.output.getvalue()
        self.assertIn('Карта не обнаружена. Запись не выполнялась.', text)
        self.assertNotIn('Карта могла измениться', text)
        self.assertNotIn('а не ID', text)

    def test_monitor_keeps_feedback_in_account_menu(self):
        import threading
        feedback = threading.Event()
        card = types.SimpleNamespace(uid=b'1234', number=ACCOUNT.card_id)
        with patch.object(console.rfid, 'read_card', return_value=card), patch.object(console.rfid, 'set_led', side_effect=lambda *a, **k: feedback.set()):
            device = console.CardConsole()
            try:
                self.assertTrue(feedback.wait(1))
                self.assertFalse(device.active)
                self.assertTrue(device.events.empty())
            finally:
                device.close()

    def test_failed_write_immediately_rearms_feedback_without_search(self):
        import threading
        recovered = threading.Event()
        device = console.CardConsole(enabled=False)
        device.enabled = True
        device.feedback_retry_at = float('inf')
        card = types.SimpleNamespace(uid=b'1234', number=ACCOUNT.card_id)
        with patch.object(console.rfid, 'write_card_number', side_effect=console.rfid.NoCardError('empty', stage='poll')) as write, patch.object(console.rfid, 'read_card', return_value=card), patch.object(console.rfid, 'set_led', side_effect=lambda *a, **k: recovered.set()):
            try:
                with self.assertRaises(console.CardNotPresentError):
                    device.write_number(ACCOUNT.card_id)
                self.assertFalse(device.write_in_progress)
                self.assertEqual(device.feedback_retry_at, 0)
                self.assertTrue(device.wakeup.is_set())
                self.assertFalse(device.active)
                device.thread = threading.Thread(target=device._worker, daemon=True)
                device.thread.start()
                self.assertTrue(recovered.wait(1))
                write.assert_called_once()
            finally:
                device.close()

    def test_feedback_recovers_after_transient_failure(self):
        import threading
        recovered = threading.Event()
        calls = []
        card = types.SimpleNamespace(uid=b'1234', number=ACCOUNT.card_id)
        def led(*args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                raise console.rfid.USBError('temporary', stage='feedback')
            recovered.set()
        with patch.object(console.rfid, 'read_card', return_value=card), patch.object(console.rfid, 'set_led', side_effect=led):
            device = console.CardConsole()
            try:
                self.assertTrue(recovered.wait(2.5))
            finally:
                device.close()


if __name__ == '__main__':
    unittest.main()
