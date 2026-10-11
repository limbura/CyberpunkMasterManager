"""
Автоматические тесты основной части библиотеки wcopy_reader.
Replay реального лога + моделирование ошибок и последовательностей карт.
Аппаратное подключение и установленный hidapi не нужны.
"""
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import wcopy_reader as r

TX = [bytes.fromhex(s).ljust(65, b'\0') for s in [
    '00 01 0F 00 00 FF 00 9A 5A A5 54 69 61 6E CB FE',
    '00 01 0F 02 00 FF 00 00 00 04 D4 32 01 00 E3 FE',
    '00 01 0F 04 00 FF 00 00 00 04 D4 32 01 01 E0 FE',
    '00 01 11 06 00 FF 00 00 00 06 D4 32 05 00 01 02 D4 FE',
    '00 01 0F 08 00 FF 00 00 00 04 D4 4A 01 00 C5 FE',
    '00 01 1A 0A 00 FF 00 00 00 0F D4 40 01 60 01 FF FF FF FF FF FF C3 D7 C8 33 C7 FE',
    '00 01 10 0C 00 FF 00 00 00 05 D4 40 01 30 01 98 FE',
]]
RX = [bytes.fromhex(s).ljust(64, b'\0') for s in [
    '02 07 01 00 00 F5 FD',
    '02 0A 03 00 D5 33 90 00 58 FD',
    '02 0A 05 00 D5 33 90 00 56 FD',
    '02 0A 07 00 D5 33 90 00 54 FD',
    '02 14 09 00 D5 4B 01 01 00 04 08 04 C3 D7 C8 33 90 00 89 FD',
    '02 0B 0B 00 D5 41 00 90 00 41 FD',
    '02 1B 0D 00 D5 41 00 ' + '00 ' * 15 + '01 90 00 2E FD',
]]


def response(seq, payload):
    packet = bytes([2, len(payload) + 6]) + seq.to_bytes(2, 'little') + payload
    return (packet + bytes([(255 - sum(packet)) % 256, 253])).ljust(64, b'\0')


class FakeDevice:
    def __init__(self, rx=None):
        self.rx = list(RX if rx is None else rx)
        self.sent = []
        self.closed = False

    def open_path(self, path):
        self.path = path

    def write(self, data):
        assert data == TX[len(self.sent)], 'Команда отличается от реального лога'
        self.sent.append(data)
        return len(data)

    def read(self, size, timeout):
        item = self.rx.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed = True


class ReaderTests(unittest.TestCase):
    def run_device(self, device, readers=None):
        if readers is None:
            readers = [{'path': b'test', 'interface_number': 0}]
        fake = SimpleNamespace(device=lambda: device, enumerate=lambda v, p: readers)
        with patch.object(r, '_get_hid', return_value=fake), patch.object(r.time, 'sleep'):
            return r.read_card_number()

    def test_real_log_returns_one_and_exact_commands(self):
        d = FakeDevice()
        self.assertEqual(self.run_device(d), 1)
        self.assertEqual(d.sent, TX)
        self.assertTrue(d.closed)

    def test_only_low_eight_bytes(self):
        d = FakeDevice()
        block = b'\xff' * 8 + (513).to_bytes(8, 'big')
        d.rx[-1] = response(13, b'\xd5\x41\0' + block + b'\x90\0')
        self.assertEqual(self.run_device(d), 513)

    def test_zero_and_max_uint64_and_data_ending_9000(self):
        for n in (0, 2**64-1, 0x9000):
            with self.subTest(n=n):
                d = FakeDevice()
                d.rx[-1] = response(13, b'\xd5\x41\0' + b'\0'*8 + n.to_bytes(8, 'big') + b'\x90\0')
                self.assertEqual(self.run_device(d), n)

    def test_no_card(self):
        d = FakeDevice()
        d.rx[4] = response(9, bytes.fromhex('D5 4B 00 90 00'))
        with self.assertRaises(r.NoCardError):
            self.run_device(d)
        self.assertEqual(len(d.sent), 5)
        self.assertTrue(d.closed)

    def test_observed_empty_reader_6300(self):
        d = FakeDevice()
        # Тело взято из аппаратного теста пользователя; HID-обёртка синтетическая.
        d.rx[4] = response(9, bytes.fromhex('D5 4B 63 00'))
        with self.assertRaises(r.NoCardError) as ctx:
            self.run_device(d)
        self.assertEqual(ctx.exception.status, 0x6300)
        self.assertEqual(ctx.exception.response, bytes.fromhex('63 00'))
        self.assertEqual(len(d.sent), 5)
        self.assertTrue(d.closed)

    def test_6300_does_not_mean_absence_during_authentication(self):
        d = FakeDevice()
        d.rx[5] = response(11, bytes.fromhex('D5 41 63 00'))
        with self.assertRaises(r.AuthenticationError):
            self.run_device(d)

    def test_auth_failure(self):
        d = FakeDevice()
        d.rx[5] = response(11, bytes.fromhex('D5 41 14 90 00'))
        with self.assertRaises(r.AuthenticationError) as ctx:
            self.run_device(d)
        self.assertEqual(ctx.exception.status, 0x14)
        self.assertEqual(ctx.exception.stage, 'authenticate')
        self.assertEqual(len(d.sent), 6)
        self.assertTrue(d.closed)

    def test_read_failure(self):
        d = FakeDevice()
        d.rx[6] = response(13, bytes.fromhex('D5 41 01 90 00'))
        with self.assertRaises(r.CardReadError):
            self.run_device(d)
        self.assertTrue(d.closed)

    def test_transport_failures(self):
        for data, error in [(b'', r.ReaderTimeoutError), (OSError('USB disconnected'), r.USBError),
                            (b'\x02', r.ProtocolError),
                            (RX[0][:5] + b'\0' + RX[0][6:], r.ProtocolError)]:
            with self.subTest(error=error, data=data):
                d = FakeDevice([data])
                with self.assertRaises(error):
                    self.run_device(d)
                self.assertTrue(d.closed)

    def test_wrong_sequence(self):
        d = FakeDevice([response(99, b'\0'), b''])
        with self.assertRaises(r.ProtocolError):
            self.run_device(d)

    def test_reader_selection(self):
        with self.assertRaises(r.ReaderNotFoundError):
            self.run_device(FakeDevice(), [])
        with self.assertRaises(r.MultipleReadersError):
            self.run_device(FakeDevice(), [{'path': b'a'}, {'path': b'b'}])

    def test_open_failure(self):
        d = FakeDevice()
        d.open_path = lambda _: (_ for _ in ()).throw(OSError('busy'))
        with self.assertRaises(r.ReaderOpenError):
            self.run_device(d)
        self.assertTrue(d.closed)

    def test_invalid_arguments_without_hardware(self):
        with self.assertRaises(ValueError):
            r.read_card_number(offset=12, length=8)


class WatchTests(unittest.TestCase):
    A = r.CardData(b'aaaa', b'\0'*15+b'\x01', b'\0\x04', 8)
    B = r.CardData(b'bbbb', b'\0'*15+b'\x02', b'\0\x04', 8)

    def events(self, sequence):
        stop = threading.Event()
        sequence = iter(sequence)
        def read(**kwargs):
            try:
                item = next(sequence)
            except StopIteration:
                stop.set()
                raise r.NoCardError('end', stage='poll')
            if isinstance(item, Exception):
                raise item
            return item
        with patch.object(r, 'read_card', side_effect=read):
            return list(r.watch_cards(stop=stop, interval=0.00001))

    def absent(self):
        return r.NoCardError('absent', stage='poll')

    def test_hold_remove_reapply(self):
        absent = r.NoCardError("poll failed", stage="poll", status=0x6300, response=b"\x63\x00")
        ev = self.events([self.A, self.A, absent, absent, self.A])
        self.assertEqual([e.number for e in ev], [1, 1])

    def test_single_missed_poll_does_not_duplicate(self):
        ev = self.events([self.A, self.absent(), self.A])
        self.assertEqual(len(ev), 1)

    def test_other_uid_is_new_even_without_absence(self):
        ev = self.events([self.A, self.B, self.B, self.A])
        self.assertEqual([e.number for e in ev], [1, 2, 1])

    def test_errors_not_removal_and_repeats_suppressed(self):
        err = r.AuthenticationError('auth', stage='authenticate', status=0x14)
        ev = self.events([self.A, err, err, self.A])
        self.assertEqual(len(ev), 2)
        self.assertIsInstance(ev[1], r.CardError)
        self.assertIs(ev[1].error, err)

    def test_timeout_not_no_card(self):
        err = r.ReaderTimeoutError('timeout', stage='poll')
        ev = self.events([self.A, self.absent(), err, self.absent(), self.A])
        self.assertEqual(len(ev), 2)
        self.assertIsInstance(ev[1], r.CardError)

    def test_stopped_watch_does_not_touch_reader(self):
        stop = threading.Event()
        stop.set()
        with patch.object(r, 'read_card') as read:
            self.assertEqual(list(r.watch_cards(stop=stop)), [])
            read.assert_not_called()


if __name__ == '__main__':
    unittest.main()
