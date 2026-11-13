"""Transient timeouts and partial Cyb reads must not hide persistent failures or retry writes."""
import queue
import types
import unittest
from unittest.mock import patch
from test_cyberwall_master_manager import console

r = console.rfid


class TransientPollTests(unittest.TestCase):
    def timeout(self, stage='poll'):
        return r.ReaderTimeoutError('late', stage=stage)

    def partial_cyb_read(self, status=1):
        return r.CardReadError(
            'Cyb не прочитал оба блока 0 и 1',
            stage='read', status=status,
            response=status.to_bytes(4, 'big') + bytes(32))

    def run_poll(self, values):
        device = console.CardConsole(enabled=False)
        device._start_scan()
        sequence = iter(values)
        delays = []
        def read(**kwargs):
            value = next(sequence)
            if isinstance(value, Exception): raise value
            return value
        def wait(delay):
            delays.append(delay)
            if len(delays) == len(values): device.stopping = True
            return False
        with patch.object(console.rfid, 'read_card', side_effect=read), \
             patch.object(device.wakeup, 'wait', side_effect=wait), \
             patch.object(console.rfid, 'write_card_number') as write:
            device._worker()
            write.assert_not_called()
        events = []
        while not device.events.empty(): events.append(device.events.get_nowait()[1])
        return events, delays

    def test_isolated_timeout_followed_by_card_is_quiet_and_fast(self):
        card = types.SimpleNamespace(number=123, presentation_id=1)
        with self.assertLogs(console._LOG, level='DEBUG') as logs:
            events, delays = self.run_poll([self.timeout('initialize'), card])
        self.assertEqual(events, [console.ScannedCard(123)])
        self.assertEqual(delays, [console.READER_SETTINGS['poll_interval']]*2)
        self.assertTrue(any('stage=initialize' in message for message in logs.output),
                        logs.output)

    def test_card_removal_reply_resets_timeout_streak(self):
        absent = r.NoCardError('none', stage='poll')
        events, delays = self.run_poll([self.timeout(), absent, self.timeout(), absent])
        self.assertEqual(events, [])
        self.assertEqual(delays, [console.READER_SETTINGS['poll_interval']]*4)

    def test_persistent_timeouts_are_visible_without_repeating_identical_message(self):
        events, delays = self.run_poll([self.timeout(), self.timeout(), self.timeout()])
        self.assertEqual(len(events), 1)
        self.assertIn('late', events[0])
        self.assertEqual(delays, [console.READER_SETTINGS['poll_interval'],
                                 console.READER_SETTINGS['error_retry_interval'],
                                 console.READER_SETTINGS['error_retry_interval']])

    def test_different_stages_still_count_as_consecutive_timeouts(self):
        events, _ = self.run_poll([self.timeout('initialize'), self.timeout('read')])
        self.assertEqual(len(events), 1)

    def test_non_timeout_errors_are_immediate(self):
        for error in (r.ReaderNotFoundError('gone', stage='enumerate'),
                      r.USBError('lost', stage='read'),
                      r.AuthenticationError('key', stage='auth')):
            with self.subTest(error=error):
                events, delays = self.run_poll([error])
                self.assertEqual(len(events), 1)
                self.assertEqual(delays, [console.READER_SETTINGS['error_retry_interval']])

    def test_timeout_does_not_reselect_held_card(self):
        card = types.SimpleNamespace(number=123, presentation_id=1)
        events, _ = self.run_poll([card, self.timeout(), card])
        self.assertEqual(events, [console.ScannedCard(123)])

    def test_first_partial_read_is_quiet_and_uses_normal_poll_interval(self):
        card = types.SimpleNamespace(number=123, presentation_id=1)
        events, delays = self.run_poll([self.partial_cyb_read(), card])
        self.assertEqual(events, [console.ScannedCard(123)])
        self.assertEqual(delays, [console.READER_SETTINGS['poll_interval']]*2)

    def test_repeated_partial_reads_report_once_and_use_error_interval(self):
        error = self.partial_cyb_read(2)
        events, delays = self.run_poll([error, error, error])
        self.assertEqual(len(events), 1)
        self.assertIn('Cyb не прочитал оба блока', events[0])
        self.assertEqual(delays, [console.READER_SETTINGS['poll_interval'],
                                 console.READER_SETTINGS['error_retry_interval'],
                                 console.READER_SETTINGS['error_retry_interval']])

    def test_mixed_timeout_and_partial_read_count_as_two_transient_failures(self):
        events, delays = self.run_poll([self.timeout(), self.partial_cyb_read()])
        self.assertEqual(len(events), 1)
        self.assertEqual(delays, [console.READER_SETTINGS['poll_interval'],
                                 console.READER_SETTINGS['error_retry_interval']])

    def test_no_card_resets_partial_read_streak(self):
        absent = r.NoCardError('none', stage='read')
        events, delays = self.run_poll(
            [self.partial_cyb_read(), absent, self.partial_cyb_read(), absent])
        self.assertEqual(events, [])
        self.assertEqual(delays, [console.READER_SETTINGS['poll_interval']]*4)

    def test_other_read_errors_still_warn_immediately(self):
        for error in (
            r.CardReadError('wCopy status', stage='read', status=1, response=b'\x01'),
            r.CardReadError('unexpected Cyb mask', stage='read', status=4,
                            response=bytes(36)),
            r.CardReadError('unrelated stage', stage='verify', status=1,
                            response=bytes(36)),
        ):
            with self.subTest(error=str(error)):
                events, delays = self.run_poll([error])
                self.assertEqual(len(events), 1)
                self.assertEqual(delays, [console.READER_SETTINGS['error_retry_interval']])

    def test_write_timeout_propagates_once(self):
        device = console.CardConsole(enabled=False)
        device.enabled = True
        with patch.object(r, 'write_card_number', side_effect=self.timeout('write')) as write:
            with self.assertRaises(r.ReaderTimeoutError): device.write_number(123)
        write.assert_called_once_with(123)
        self.assertFalse(device.write_in_progress)
