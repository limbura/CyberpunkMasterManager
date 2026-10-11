""" Автоматические тесты кода управления светодиодом и звуком. Используют имитацию считывателя """
import threading
import unittest
from unittest.mock import patch

import wcopy_reader as r


class FeedbackTests(unittest.TestCase):
    A = r.CardData(b'aaaa', b'\0'*15+b'\x01', b'\0\x04', 8)
    B = r.CardData(b'bbbb', b'\0'*15+b'\x02', b'\0\x04', 8)

    def test_exact_commands_and_led_status_not_an_error(self):
        session = r._Session(None, 2500)
        with patch.object(session, 'exchange', side_effect=[b'\x90\x02', b'\x90\x02']) as exchange:
            session.feedback('green', True)
        self.assertEqual([c.args[0] for c in exchange.call_args_list], [
            bytes.fromhex('FF 00 40 50 04 05 01 01 01'),
            bytes.fromhex('FF 00 40 0E 04 00 00 00 00')])
        for color, mask in [('red', 0x0D), ('off', 0x0C)]:
            with patch.object(session, 'exchange', return_value=b'\0') as exchange:
                session.feedback(color, False)
            self.assertEqual(exchange.call_args.args[0], bytes([255, 0, 64, mask, 4, 0, 0, 0, 0]))

    def test_rejected_or_unknown_reply_not_success(self):
        session = r._Session(None, 2500)
        for reply in (b'', b'\x63\0', b'\xfd', b'\x90\xff', b'\xd5\x41\0', b'\0\0'):
            with patch.object(session, 'exchange', return_value=reply):
                with self.assertRaises(r.FeedbackError):
                    session.feedback('green', False)

    def run_watch(self, sequence, fail=False, sound=True):
        stop = threading.Event()
        items = iter(sequence)
        def read(**kwargs):
            item = next(items)
            if isinstance(item, Exception):
                raise item
            return item
        count = 0
        original_wait = stop.wait
        def wait(seconds):
            nonlocal count
            count += 1
            if count >= len(sequence):
                stop.set()
            return original_wait(0)
        with patch.object(r, 'read_card', side_effect=read), patch.object(stop, 'wait', side_effect=wait), \
                patch.object(r, 'set_reader_feedback') as feedback:
            if fail:
                feedback.side_effect = r.FeedbackError('unsupported', stage='feedback')
            events = list(r.watch_cards(stop=stop, feedback=True, sound=sound))
        return events, feedback.call_args_list

    def test_beep_once_until_removal_and_different_uid(self):
        absent = r.NoCardError('absent', stage='poll')
        events, calls = self.run_watch([absent, self.A, self.A, absent, absent, self.A, self.B])
        self.assertEqual([e.number for e in events], [1, 1, 2])
        self.assertEqual([c.args[0] for c in calls], ['red', 'green', 'green', 'green', 'red', 'green', 'green'])
        self.assertEqual([c.kwargs['beep'] for c in calls], [False, True, False, False, False, True, True])

    def test_feedback_failure_does_not_lose_cards_or_repeat_commands(self):
        events, calls = self.run_watch([self.A, self.A, self.B], fail=True)
        self.assertEqual([e.number for e in events if isinstance(e, r.CardPresented)], [1, 2])
        self.assertEqual(len([e for e in events if isinstance(e, r.CardError)]), 1)
        self.assertEqual(len(calls), 1)

    def test_bad_arguments_no_hardware(self):
        with patch.object(r, '_get_hid') as hid:
            for kwargs in ({'color': 'blue'}, {'color': 'green', 'beep': 1}):
                with self.assertRaises(ValueError):
                    r.set_reader_feedback(**kwargs)
            hid.assert_not_called()

    def test_no_beep_preserves_led_and_events(self):
        absent = r.NoCardError('absent', stage='poll')
        events, calls = self.run_watch([absent, self.A, self.A, self.B], sound=False)
        self.assertEqual([e.number for e in events], [1, 2])
        self.assertEqual([c.args[0] for c in calls], ['red', 'green', 'green', 'green'])
        self.assertTrue(all(c.kwargs['beep'] is False for c in calls))


if __name__ == '__main__':
    unittest.main()
