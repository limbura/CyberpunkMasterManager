"""Сценарии конвейера без настоящих карт или БД."""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from test_cyberwall_master_manager import app, connection, Cursor
from modules import batch_cards as b, card_console as console

r = console.rfid
SCOPE = {'host': 'test', 'port': 3306, 'database': 'cyberwall'}
A = {'card_id': 1, 'id': 123456789, 'name': 'Банк Востока'}
B = {'card_id': 7, 'id': 987654321, 'name': 'Второй аккаунт'}

def card(n=0, uid=b'abcd'):
    return r.CardData(uid, bytes(range(8)) + n.to_bytes(8, 'big'), b'\0\x04', 8)

class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'batch_cards.json'
        self.j = b.Journal.create(self.path, SCOPE, [B, A]); self.j.save()
        self.out = io.StringIO()
        redir = contextlib.redirect_stdout(self.out); redir.__enter__()
        self.addCleanup(redir.__exit__, None, None, None)
        self.db = Mock()
        self.db.batch_accounts.return_value = [A, B]
        self.db.batch_account.side_effect = lambda n: {1: A, 7: B}.get(n)
        self.db.batch_by_id.side_effect = lambda n: {A['id']: A, B['id']: B}.get(n)
        self.db.batch_guard.side_effect = lambda expected: contextlib.nullcontext()
        self.reader = Mock(RFIDError=r.RFIDError, NoCardError=r.NoCardError)
        self.reader.read_card.return_value = card()
        self.reader.write_card_number.return_value = 1
    def load(self): return b.Journal.load(self.path, SCOPE)
    def attempt(self, answer=''):
        return b.attempt(self.db, self.j, self.j.current(), self.reader, Mock(return_value=answer), 10)
    def commands(self, commands):
        with patch.object(b, 'clear_screen'), patch.object(b.time, 'sleep'):
            b.process(self.db, self.j, self.reader, Mock(side_effect=commands))
    def ready(self):
        self.j.reserve(self.j.current(), card()); self.j.finish(self.j.current(), 'written')

    def test_wrong_label_never_reads_or_writes_and_stays(self):
        self.commands(['987654321', ':b'])
        self.reader.read_card.assert_not_called(); self.reader.write_card_number.assert_not_called()
        self.assertEqual(self.load().data['current'], 1)
        self.assertIn('ID НЕ СОВПАЛ', self.out.getvalue())

    def test_success_waits_for_removal_then_next_card_id(self):
        self.reader.read_card.side_effect = [card(), card(1), r.NoCardError('none', stage='poll'), r.NoCardError('none', stage='poll')]
        self.commands(['123 456 789', ':b'])
        self.reader.write_card_number.assert_called_once_with(1, expected_uid=b'abcd', expected_block=card().block)
        saved = self.load()
        self.assertEqual(saved.data['current'], 7)
        self.assertEqual(saved.data['uids'], {'61626364': 1})
        self.assertEqual(saved.data['accounts'][0]['status'], 'written')
        self.assertIsNone(saved.data['remove_uid'])

    def test_unknown_number_requires_confirmation(self):
        self.reader.read_card.return_value = card(999)
        self.assertIn('отменена', self.attempt('нет'))
        self.reader.write_card_number.assert_not_called()

    def test_known_other_account_requires_explicit_overwrite(self):
        self.reader.read_card.return_value = card(7)
        self.attempt(); self.reader.write_card_number.assert_not_called()
        self.attempt('ПЕРЕЗАПИСАТЬ'); self.reader.write_card_number.assert_called_once()

    def test_uid_used_for_another_account_is_blocked(self):
        self.j.data['uids'][card().uid.hex()] = 7
        with self.assertRaisesRegex(b.BatchError, 'card_id 7'): self.attempt()
        self.reader.write_card_number.assert_not_called()

    def test_already_correct_requires_stable_read_without_write(self):
        self.reader.read_card.return_value = card(1)
        self.attempt(); self.reader.write_card_number.assert_not_called()
        self.assertEqual(self.load().current()['status'], 'already')
        self.assertEqual(self.reader.read_card.call_count, 2)

    def test_swap_during_already_correct_check_is_not_success(self):
        self.reader.read_card.side_effect = [card(1), card(1, b'efgh')]
        with self.assertRaises(b.BatchError): self.attempt()
        self.assertEqual(self.load().current()['status'], 'error')
        self.reader.beep.assert_not_called()

    def test_write_failure_or_interrupt_keeps_cursor_and_uid(self):
        for error in (r.VerificationError('failed', stage='verify'), KeyboardInterrupt()):
            self.reader.write_card_number.side_effect = error
            with self.assertRaises(type(error)): self.attempt()
            saved = self.load()
            self.assertEqual(saved.data['current'], 1)
            self.assertEqual(saved.current()['status'], 'error')
            self.assertEqual(saved.data['uids'][card().uid.hex()], 1)
            self.assertIsNone(saved.data['remove_uid'])
        self.reader.beep.assert_not_called()

    def test_intent_save_failure_prevents_hardware_write(self):
        with patch.object(self.j, 'save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError): self.attempt()
        self.reader.write_card_number.assert_not_called()

    def test_result_save_failure_leaves_uncertain_intent(self):
        original = self.j.save
        def save():
            if self.j.current()['status'] == 'written': raise OSError('disk full')
            original()
        with patch.object(self.j, 'save', side_effect=save):
            with self.assertRaises(OSError): self.attempt()
        self.assertEqual(self.load().current()['status'], 'writing')
        self.reader.beep.assert_not_called()

    def test_beep_failure_keeps_success(self):
        self.reader.beep.side_effect = r.FeedbackError('failed', stage='feedback')
        self.assertIn('писк не сработал', self.attempt())
        self.assertEqual(self.load().current()['status'], 'written')

    def test_skip_jump_gap_and_restore_cursor(self):
        self.commands([':skip', ':card 3', ':id 123456789', ':b'])
        saved = self.load()
        self.assertEqual(saved.data['current'], 1)
        self.assertEqual(saved.current()['status'], 'skipped')
        self.assertEqual([row['card_id'] for row in saved.data['accounts']], [1, 7])
        self.assertIn('Пропуски card_id', self.out.getvalue())

    def test_completed_account_requires_explicit_repeat(self):
        self.ready(); self.j.data['remove_uid'] = None; self.j.save()
        self.commands(['123456789', ':r', ':b'])
        self.reader.write_card_number.assert_not_called()
        self.assertEqual(self.load().current()['status'], 'pending')
        self.assertIn('сначала выбери :r', self.out.getvalue())

    def test_changed_database_label_rejects_old_id(self):
        self.db.batch_account.side_effect = lambda n: dict(A, id=111222333, name='Новое имя') if n == 1 else B
        self.commands(['123456789', ':b'])
        self.reader.read_card.assert_not_called()
        self.assertEqual(self.load().current()['name'], 'Новое имя')

    def test_guard_rejects_change_after_label_confirmation(self):
        self.db.batch_guard.side_effect = b.BatchError('аккаунт изменён')
        self.commands(['123456789', ':b'])
        self.reader.write_card_number.assert_not_called()
        self.assertFalse(self.load().data['uids'])

    def test_usb_error_does_not_count_as_removal(self):
        self.ready()
        self.reader.read_card.side_effect = [r.NoCardError('no', stage='poll'), r.USBError('lost', stage='read'), r.NoCardError('no', stage='poll'), KeyboardInterrupt()]
        with patch.object(b.time, 'sleep'), self.assertRaises(KeyboardInterrupt):
            b.wait_removed(self.reader, self.j)
        self.assertIsNotNone(self.load().data['remove_uid'])
        self.assertEqual(self.load().data['current'], 1)

    def test_scope_mismatch_and_corruption_do_not_overwrite(self):
        content = self.path.read_bytes()
        with self.assertRaises(b.BatchError): b.Journal.load(self.path, dict(SCOPE, host='other'))
        self.assertEqual(self.path.read_bytes(), content)
        self.path.write_text('{broken')
        with self.assertRaises(b.BatchError): self.load()
        self.assertEqual(self.path.read_text(), '{broken')

    def test_atomic_replace_failure_preserves_previous_journal(self):
        content = self.path.read_bytes(); self.j.current()['status'] = 'skipped'
        with patch.object(b.os, 'replace', side_effect=OSError('failed')):
            with self.assertRaises(OSError): self.j.save()
        self.assertEqual(self.path.read_bytes(), content)

    def test_second_process_cannot_lock_same_journal(self):
        with b.journal_lock(self.path):
            with self.assertRaises(b.BatchError):
                with b.journal_lock(self.path): pass

    def test_resume_and_archive_on_new_work(self):
        self.j.advance()
        c = Mock(); c.batch_mode.side_effect = lambda: contextlib.nullcontext(self.reader)
        with patch.object(b, 'process') as process:
            b.run(self.db, c, self.path, SCOPE, ask=Mock(return_value='1'))
            self.assertEqual(process.call_args.args[1].data['current'], 7)
            b.run(self.db, c, self.path, SCOPE, ask=Mock(side_effect=['2', 'НОВАЯ']))
            self.assertEqual(process.call_args.args[1].data['current'], 1)
        archives = list(self.path.parent.glob('batch_cards-*.json'))
        self.assertEqual(len(archives), 1)
        self.assertEqual(b.Journal.load(archives[0], SCOPE).data['current'], 7)

    def test_header_fixed_positions_and_clear_between_accounts(self):
        with patch.object(b, 'clear_screen') as clear:
            b.header(self.j, A | {'status': 'pending'}); first = self.out.getvalue().splitlines()
            self.out.seek(0); self.out.truncate()
            b.header(self.j, B | {'status': 'pending'}); second = self.out.getvalue().splitlines()
        self.assertEqual(clear.call_count, 2)
        self.assertEqual(first[2], 'card_id: 1'); self.assertEqual(second[2], 'card_id: 7')
        self.assertTrue(first[3].startswith('ID:')); self.assertTrue(second[4].startswith('Имя:'))
        self.assertNotIn('Банк Востока', '\n'.join(second))
        self.assertNotIn('принадлежность', '\n'.join(second))

    def test_sql_guard_locks_row_and_rejects_stale_identity(self):
        cursor = Cursor(A); conn = connection(cursor); db = app.Database(conn)
        with db.batch_guard(A): pass
        self.assertIn('FOR UPDATE', cursor.calls[0][0]); self.assertEqual(cursor.calls[0][1], (1,))
        conn.commit.assert_called_once()
        with self.assertRaises(b.BatchError):
            with db.batch_guard(dict(A, id=111222333)): pass
        conn.rollback.assert_called_once()

    def test_batch_mode_restores_background_after_interrupt(self):
        c = console.CardConsole(enabled=False); c.enabled = True
        with self.assertRaises(KeyboardInterrupt):
            with c.batch_mode():
                self.assertTrue(c.write_in_progress); raise KeyboardInterrupt()
        self.assertFalse(c.write_in_progress); self.assertTrue(c.wakeup.is_set())

    def test_batch_entry_in_general_search(self):
        self.db.connection = connection(Cursor())
        with patch.object(app, 'CONSOLE', Mock()), patch.object(app, 'read_input', side_effect=[':batch', ':b']), patch.object(app.batch_cards, 'run') as run:
            self.assertIsNone(app.choose_candidate(self.db))
        run.assert_called_once()

    def test_resume_uncertain_write_rechecks_card_instead_of_rewriting(self):
        self.j.reserve(self.j.current(), card())
        self.j = self.load()
        self.reader.read_card.side_effect = [card(1), card(1), r.NoCardError('no', stage='poll'), r.NoCardError('no', stage='poll')]
        self.commands(['123456789', ':b'])
        self.reader.write_card_number.assert_not_called()
        self.assertEqual(self.load().data['accounts'][0]['status'], 'already')
        self.assertEqual(self.load().data['current'], 7)

    def test_resume_success_with_changed_db_requires_new_sticker_check(self):
        self.ready(); self.j = self.load()
        self.db.batch_account.side_effect = lambda n: dict(A, id=111222333) if n == 1 else B
        self.reader.read_card.side_effect = [r.NoCardError('no', stage='poll'), r.NoCardError('no', stage='poll')]
        self.commands(['123456789', ':b'])
        self.assertEqual(self.load().data['current'], 1)
        self.assertNotIn(self.load().current()['status'], b.READY)
        self.reader.write_card_number.assert_not_called()

    def test_deleted_account_cannot_be_written(self):
        self.db.batch_account.side_effect = lambda n: None
        self.commands(['123456789', ':skip', ':b'])
        self.reader.read_card.assert_not_called()
        self.assertEqual(self.load().data['current'], 7)
