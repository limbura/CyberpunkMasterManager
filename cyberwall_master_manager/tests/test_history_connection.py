"""История, изоляция секретов, автоподключение и смена БД без MySQL/USB."""
import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cyberwall_master_manager as app
from modules.card_console import CardConsole, ScannedCard
from modules.windows_console import KeyStroke
from modules.input_history import HistoryCursor
from modules import connection_store as storage

PROFILE = dict(host='db.example', port=3307, user='master', database='game', password='not-a-real-password')


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.console = CardConsole(False)
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.output)
        self.redirect.__enter__()

    def tearDown(self):
        self.console.close()
        self.redirect.__exit__(None, None, None)

    def edit(self, keys, entries=None, epoch=None):
        keyboard = Mock()
        keyboard.poll.side_effect = keys
        return self.console._windows_edit('Ввод:', epoch, keyboard, entries)

    def test_up_down_boundaries_and_draft(self):
        h = HistoryCursor(['один', 'два'])
        self.assertEqual(h.move(-1, 'черновик'), 'два')
        self.assertEqual(h.move(-1, 'два'), 'один')
        self.assertEqual(h.move(-1, 'один'), 'один')
        self.assertEqual(h.move(1, 'один'), 'два')
        self.assertEqual(h.move(1, 'два'), 'черновик')
        self.assertEqual(h.move(1, 'черновик'), 'черновик')

    def test_recalled_line_editable_and_only_enter_submits(self):
        value = self.edit([KeyStroke(key=38), KeyStroke(key=37), KeyStroke('!'), KeyStroke('\r')], ['abc'])
        self.assertEqual(value, 'ab!c')

    def test_down_restores_typed_draft(self):
        value = self.edit([KeyStroke('я'), KeyStroke(key=38), KeyStroke(key=40), KeyStroke('\r')], ['abc'])
        self.assertEqual(value, 'я')

    def test_no_history_ignores_arrows(self):
        self.console.histories['search'] = ['secret-other-context']
        self.assertEqual(self.edit([KeyStroke(key=38), KeyStroke('\r')]), '')

    def test_card_event_does_not_store_typed_or_scanned_id(self):
        epoch = self.console._start_scan()
        self.console._send(epoch, ScannedCard(123))
        self.assertEqual(self.edit([KeyStroke('я'), None], ['prior'], epoch), ScannedCard(123))
        self.assertEqual(self.console.histories, {})

    def test_history_is_grouped_deduplicated_and_bounded(self):
        with patch('builtins.input', side_effect=['abc', '', 'abc', '123', 'password']):
            with patch('sys.stdin.isatty', return_value=False):
                for _ in range(3): self.console.read('>', history='search')
                self.console.read('>', history='menu')
                self.console.read('>')
        self.assertEqual(self.console.histories, {'search': ['abc'], 'menu': ['123']})
        with patch('builtins.input', side_effect=map(str, range(105))), patch('sys.stdin.isatty', return_value=False):
            for _ in range(105): self.console.read('>', history='search')
        self.assertEqual(self.console.histories['search'], list(map(str, range(5, 105))))

    def test_windows_editor_runs_without_reader_and_for_no_history_fields(self):
        with patch('sys.stdin.isatty', return_value=True), patch('modules.card_console.os.name', 'nt'), \
             patch.object(self.console, '_windows_read', side_effect=['test', 'private']) as edit:
            self.console.read('>', history='search')
            self.console.read('>')
        self.assertIsNone(edit.call_args.args[2])
        self.assertEqual(self.console.histories, {'search': ['test']})
        self.assertFalse(self.console.active)

    def test_reader_selection_never_added_to_history(self):
        with patch('sys.stdin.isatty', return_value=True), patch('modules.card_console.os.name', 'nt'), \
             patch.object(self.console, '_windows_read', return_value=ScannedCard(4)):
            self.console.read('>', history='search')
        self.assertEqual(self.console.histories['search'], [])

    def test_batch_entry_uses_console_without_history(self):
        db, console = Mock(), Mock()
        with patch.object(app, 'CONSOLE', console), patch.object(app, 'database_scope', return_value={}), \
             patch.object(app, 'read_input', side_effect=[':menu', '2', '0', ':b']), patch.object(app.batch_cards, 'run') as run:
            app.choose_candidate(db)
        self.assertEqual(run.call_args.kwargs['ask'], console.read)

    def test_password_edit_and_confirmation_both_disable_history(self):
        db, console = Mock(), Mock()
        entity = app.Entity('account', 123456789, 'Name', 1)
        db.refresh.return_value = entity
        db.field_value.return_value = 'old'
        console.read.side_effect = [' 00123 ', 'да']
        with patch.object(app, 'CONSOLE', console):
            app.edit_account_field(db, entity, 'password')
        self.assertTrue(all(c.kwargs['history'] is None for c in console.read.call_args_list))
        db.set_field.assert_called_once_with(1, 'password', 'old', ' 00123 ')

    def test_delete_confirmation_cannot_recall_ids(self):
        db, console = Mock(), Mock()
        db.refresh.return_value = app.Entity('account', 123456789, 'Name', 1)
        console.read.return_value = ''
        with patch.object(app, 'CONSOLE', console):
            app.delete_entity(db, db.refresh.return_value)
        self.assertIsNone(console.read.call_args.kwargs['history'])

    @unittest.skipIf(os.name == 'nt', 'POSIX terminal')
    def test_posix_terminal_history_unicode_and_mode_restoration(self):
        import pty
        import select
        import subprocess
        import time
        master, slave = pty.openpty()
        code = """import termios
from modules.card_console import CardConsole
before = termios.tcgetattr(0)
c = CardConsole(False)
a = c.read('FIRST', history='search')
b = c.read('SECOND', history='search')
d = c.read('NOHISTORY')
assert (a,b,d) == ('тест','тест!','')
assert termios.tcgetattr(0) == before
print('PASSED', flush=True)
"""
        process = subprocess.Popen([sys.executable, '-c', code], cwd=Path(__file__).resolve().parents[1],
                                   stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        try:
            def until(marker):
                data = b''
                deadline = time.monotonic() + 5
                while marker not in data:
                    if time.monotonic() >= deadline:
                        self.fail('Terminal did not reach ' + repr(marker) + ': ' + repr(data))
                    if select.select([master], [], [], 0.1)[0]:
                        data += os.read(master, 4096)
            until(b'FIRST')
            os.write(master, 'тест\r'.encode())
            until(b'SECOND')
            os.write(master, b'\x1b[A!\r')
            until(b'NOHISTORY')
            os.write(master, b'\x1b[A\r')
            until(b'PASSED')
            self.assertEqual(process.wait(timeout=5), 0)
        finally:
            if process.poll() is None:
                process.kill(); process.wait()
            os.close(master)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'private' / 'connection.dat'
        self.store = storage.ConnectionStore(self.path)

    def tearDown(self): self.temp.cleanup()

    def test_round_trip_and_clear_real_platform_storage(self):
        self.assertIsNone(self.store.load())
        self.store.save(PROFILE)
        self.assertEqual(self.store.load(), PROFILE)
        if os.name == 'nt':
            self.assertNotIn(PROFILE['password'].encode(), self.path.read_bytes())
            self.assertTrue(self.path.read_bytes().startswith(b'DPAPI1\0'))
        else:
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        self.store.clear(); self.store.clear()
        self.assertIsNone(self.store.load())

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
    def test_dpapi_corruption_rejected(self):
        self.store.save(PROFILE)
        data = bytearray(self.path.read_bytes()); data[-10] ^= 1
        self.path.write_bytes(data)
        with self.assertRaises(OSError): self.store.load()

    def test_invalid_profile_never_replaces_existing(self):
        self.store.save(PROFILE)
        for invalid in ({}, {**PROFILE, 'port': True}, {**PROFILE, 'password': None}):
            with self.assertRaises(ValueError): self.store.save(invalid)
        self.assertEqual(self.store.load(), PROFILE)

    def test_failed_atomic_replace_keeps_previous_and_cleans_temporary(self):
        self.store.save(PROFILE)
        with patch.object(storage.os, 'replace', side_effect=OSError('disk')):
            with self.assertRaises(OSError): self.store.save({**PROFILE, 'password': 'new'})
        self.assertEqual(self.store.load(), PROFILE)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_corrupt_file_rejected_without_deleting_it(self):
        self.path.parent.mkdir(); self.path.write_bytes(b'invalid')
        with self.assertRaises(ValueError): self.store.load()
        self.assertEqual(self.path.read_bytes(), b'invalid')


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.store = Mock(load=Mock(return_value=None))
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.output)
        self.redirect.__enter__()
        self.console_patch = patch.object(app, 'CONSOLE', None)
        self.console_patch.start()

    def tearDown(self):
        self.console_patch.stop(); self.redirect.__exit__(None, None, None)

    def test_auto_connect_no_prompts_or_password_output(self):
        self.store.load.return_value = dict(PROFILE)
        with patch.object(app, 'open_database') as connect, patch.object(app, 'prompt_default') as prompt, patch.object(app, 'getpass') as password:
            self.assertEqual(app.connect_interactively(store=self.store), connect.return_value)
        connect.assert_called_once_with(PROFILE)
        prompt.assert_not_called(); password.assert_not_called(); self.store.save.assert_not_called()
        self.assertNotIn(PROFILE['password'], self.output.getvalue())

    def test_failed_auto_connection_manual_change_all_parameters(self):
        self.store.load.return_value = dict(PROFILE)
        with patch.object(app, 'open_database', side_effect=[app.pymysql.MySQLError(1045, PROFILE['password']), Mock()]) as connect, \
             patch.object(app, 'prompt_default', side_effect=['newhost', '3308', 'newuser', 'newdb']), \
             patch.object(app, 'getpass', return_value='newpass'):
            app.connect_interactively(store=self.store)
        self.assertEqual(connect.call_count, 2)
        self.store.save.assert_called_once_with(dict(host='newhost', port=3308, user='newuser', database='newdb', password='newpass'))
        self.assertNotIn(PROFILE['password'], self.output.getvalue())

    def test_unreadable_profile_prompts_again(self):
        self.store.load.side_effect = OSError('decrypt')
        with patch.object(app, 'open_database'), patch.object(app, 'prompt_default', side_effect=lambda label, default: default), patch.object(app, 'getpass', return_value='pass'):
            app.connect_interactively(store=self.store)
        self.store.save.assert_called_once()

    def test_manual_mode_never_reuses_saved_password(self):
        with patch.object(app, 'open_database'), patch.object(app, 'prompt_default', side_effect=lambda label, default: default) as prompts, patch.object(app, 'getpass', return_value='new') as password:
            app.connect_interactively(automatic=False, defaults={k:v for k,v in PROFILE.items() if k != 'password'}, store=self.store)
        self.store.load.assert_not_called(); password.assert_called_once()
        self.assertEqual(prompts.call_args_list[-1].args, ('База данных', 'game'))
        self.assertEqual(self.store.save.call_args.args[0]['password'], 'new')

    def test_failed_manual_connection_not_saved(self):
        with patch.object(app, 'open_database', side_effect=app.pymysql.MySQLError(1045, 'denied')), \
             patch.object(app, 'prompt_default', side_effect=lambda label, default: default), \
             patch.object(app, 'getpass', return_value='pass'), patch.object(app, 'read_input', side_effect=app.QuitRequested):
            with self.assertRaises(app.QuitRequested): app.connect_interactively(store=self.store)
        self.store.save.assert_not_called()

    def test_save_failure_does_not_close_good_connection(self):
        self.store.save.side_effect = OSError('disk')
        with patch.object(app, 'open_database') as connect, patch.object(app, 'prompt_default', side_effect=lambda label, default: default), patch.object(app, 'getpass', return_value='pass'):
            self.assertEqual(app.connect_interactively(store=self.store), connect.return_value)
        connect.return_value.close.assert_not_called()
        self.assertIn('сохранить параметры не удалось', self.output.getvalue())

    def test_schema_access_failure_closes_connection(self):
        with patch.object(app.pymysql, 'connect', create=True) as connect, patch.object(app.Database, 'check_read_access', side_effect=app.pymysql.MySQLError(1142, 'denied')):
            with self.assertRaises(app.pymysql.MySQLError): app.open_database(PROFILE)
        connect.return_value.close.assert_called_once()

    def test_disconnect_closes_old_connection_clears_profile_and_history_then_manual_login(self):
        first = Mock(host='oldhost', port=3306, user='olduser', db=b'old_db')
        second = Mock(host='newhost', port=3307, user='newuser', db=b'new_db')
        console = Mock()
        with patch.object(app, 'CardConsole', return_value=console), patch.object(app, 'ConnectionStore', return_value=self.store), \
             patch.object(app, 'connect_interactively', side_effect=[first, second]) as connect, \
             patch.object(app, 'recover_pending_id'), patch.object(app, 'choose_candidate', side_effect=[app.DisconnectRequested, app.QuitRequested]):
            self.assertEqual(app.main(), 0)
        first.close.assert_called_once(); second.close.assert_called_once()
        console.histories.clear.assert_called_once(); self.store.clear.assert_called_once()
        self.assertFalse(connect.call_args.kwargs['automatic'])
        self.assertEqual(connect.call_args.kwargs['defaults'], dict(host='oldhost', port=3306, user='olduser', database='old_db'))

    def test_pending_other_database_still_allows_disconnect_without_losing_journal(self):
        first = Mock(host='wrong', port=3306, user='master', db=b'wrong')
        with patch.object(app, 'CardConsole'), patch.object(app, 'ConnectionStore', return_value=self.store), \
             patch.object(app, 'connect_interactively', side_effect=[first, app.QuitRequested]), \
             patch.object(app, 'recover_pending_id', side_effect=app.WriteUncertain('other database')), \
             patch.object(app, 'read_input', return_value=':disconnect'), patch.object(app, 'choose_candidate') as choose:
            self.assertEqual(app.main(), 0)
        first.close.assert_called_once()
        choose.assert_not_called()
        self.store.clear.assert_called_once()

    def test_search_disconnect_does_not_query_db(self):
        db = Mock()
        with patch.object(app, 'read_input', side_effect=[':menu', '5']):
            with self.assertRaises(app.DisconnectRequested): app.choose_candidate(db)
        db.search.assert_not_called()


if __name__ == '__main__': unittest.main()
