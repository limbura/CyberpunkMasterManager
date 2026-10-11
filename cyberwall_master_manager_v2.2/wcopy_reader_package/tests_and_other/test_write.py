"""Автоматические тесты записи на карту. Моделирование записи: команды, сохранение байтов и отказ при сбоях."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import wcopy_reader as r
from test_wcopy_reader import RX, TX, response


class WriteDevice:
    def __init__(self, old, expected, verified=None, uid=None):
        self.sent = []
        self.closed = False
        self.expected = expected
        self.rx = list(RX)
        self.rx[-1] = response(13, b'\xd5\x41\0' + old + b'\x90\0')
        self.rx.append(response(15, b'\xd5\x41\0\x90\0'))
        for i, raw in enumerate(RX):
            payload = raw[4:raw[1]-2]
            if i == 4 and uid is not None:
                payload = b'\xd5\x4b\x01\x01\0\x04\x08\x04' + uid + b'\x90\0'
            if i == 6:
                payload = b'\xd5\x41\0' + (expected if verified is None else verified) + b'\x90\0'
            self.rx.append(response(17 + 2*i, payload))

    def open_path(self, path):
        self.path = path

    def write(self, data):
        i = len(self.sent)
        if i < 7:
            assert data == TX[i]
        elif i == 7:
            # Независимо проверяем длину, адрес, код записи и весь блок.
            assert len(data) == 65 and data[:5] == bytes.fromhex('00 01 20 0E 00')
            assert data[5:31] == bytes.fromhex('FF 00 00 00 15 D4 40 01 A0 01') + self.expected
            assert data[31] == (~sum(data[1:31]) & 255) and data[32] == 0xFE
        else:
            original = TX[i-8]
            assert data[5:original[2]-1] == original[5:original[2]-1]
            assert int.from_bytes(data[3:5], 'little') == 2*i
            assert data[data[2]-1] == (~sum(data[1:data[2]-1]) & 255)
        self.sent.append(data)
        return len(data)

    def read(self, size, timeout):
        item = self.rx.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed = True


class WriteTests(unittest.TestCase):
    def run_write(self, d, number, **kwargs):
        fake = SimpleNamespace(device=lambda: d, enumerate=lambda *a: [{'path': b'test'}])
        with patch.object(r, '_get_hid', return_value=fake), patch.object(r.time, 'sleep'):
            return r.write_card_number(number, **kwargs)

    def test_roundtrip_boundaries_and_preserve_prefix(self):
        old = bytes.fromhex('0102030405060708FFEEDDCCBBAA9988')
        for number in (0, 123456789, 2**64-1, 0x9000):
            with self.subTest(number=number):
                d = WriteDevice(old, old[:8] + number.to_bytes(8, 'big'))
                self.assertEqual(self.run_write(d, number), number)
                self.assertEqual(len(d.sent), 15)
                self.assertTrue(d.closed)

    def test_custom_format_preserves_both_sides(self):
        old = bytes(range(16))
        expected = old[:4] + b'\x34\x12' + old[6:]
        d = WriteDevice(old, expected)
        self.assertEqual(self.run_write(d, 0x1234, offset=4, length=2, byteorder='little'), 0x1234)

    def test_mismatch_in_id_or_preserved_bytes(self):
        expected = b'\0'*15+b'\x01'
        for wrong in (b'\0'*16, b'\xff'+expected[1:]):
            d = WriteDevice(b'\0'*16, expected, verified=wrong)
            with self.assertRaises(r.VerificationError):
                self.run_write(d, 1)
            self.assertTrue(d.closed)

    def test_different_card_is_not_success(self):
        d = WriteDevice(b'\0'*16, b'\0'*15+b'\x01', uid=b'abcd')
        # Авторизация использует UID новой карты; проверка команды — отдельно.
        original_write = d.write
        def write(data):
            if len(d.sent) == 13:
                d.sent.append(data)
                return len(data)
            return original_write(data)
        d.write = write
        with self.assertRaises(r.VerificationError):
            self.run_write(d, 1)
        self.assertTrue(d.closed)

    def test_write_failure_and_timeout_do_not_retry(self):
        for failure, error in ((response(15, b'\xd5\x41\x01\x90\0'), r.CardWriteError),
                               (b'', r.ReaderTimeoutError), (OSError('lost'), r.USBError)):
            d = WriteDevice(b'\0'*16, b'\0'*15+b'\x01')
            d.rx[7] = failure
            with self.assertRaises(error):
                self.run_write(d, 1)
            self.assertEqual(len(d.sent), 8)
            self.assertTrue(d.closed)

    def test_verify_failure_is_not_success(self):
        for index, failure in ((8, b''), (12, response(25, b'\xd5\x4b\x63\0')),
                               (14, response(29, b'\xd5\x41\x01\x90\0'))):
            d = WriteDevice(b'\0'*16, b'\0'*15+b'\x01')
            d.rx[index] = failure
            with self.assertRaises(r.VerificationError) as ctx:
                self.run_write(d, 1)
            self.assertIsInstance(ctx.exception.__cause__, r.RFIDError)
            self.assertTrue(d.closed)

    def test_initial_read_failure_never_writes(self):
        for index, payload in ((4, b'\xd5\x4b\x63\0'), (5, b'\xd5\x41\x14\x90\0'),
                               (6, b'\xd5\x41\x01\x90\0')):
            d = WriteDevice(b'\0'*16, b'\0'*15+b'\x01')
            d.rx[index] = response(index*2+1, payload)
            with self.assertRaises(r.RFIDError):
                self.run_write(d, 1)
            self.assertLess(len(d.sent), 8)
            self.assertTrue(d.closed)

    def test_invalid_input_never_opens_device(self):
        with patch.object(r, '_get_hid') as hid:
            for value in (-1, 2**64, True, 1.0, '123', None):
                with self.assertRaises(ValueError):
                    r.write_card_number(value)
            for kwargs in ({'offset': 12}, {'length': 0}, {'byteorder': 'x'},
                           {'timeout_ms': 0}, {'path': b''}):
                with self.assertRaises(ValueError):
                    r.write_card_number(1, **kwargs)
            hid.assert_not_called()


if __name__ == '__main__':
    unittest.main()
