"""Проверка Win32-контракта на имитации очереди событий (без Windows)."""
from collections import deque
import ctypes as ct
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import windows_console as win


def event(char='', key=0, down=True, kind=1, repeat=1):
    record = win.InputRecord()
    record.event_type = kind
    record.event.key = win.KeyEventRecord(down, repeat, key, 0, ord(char) if char else 0, 0)
    return record


class NativeInputTests(unittest.TestCase):
    def setUp(self):
        self.queue = deque()
        self.modes = []
        self.api = Mock()
        self.api.GetStdHandle.return_value = 123
        def get_mode(handle, pointer):
            ct.cast(pointer, ct.POINTER(ct.c_uint32))[0] = 0x47
            return 1
        def count(handle, pointer):
            ct.cast(pointer, ct.POINTER(ct.c_uint32))[0] = len(self.queue)
            return 1
        def read(handle, pointer, length, count_pointer):
            self.assertEqual(length, 1)
            record = self.queue.popleft()
            ct.memmove(pointer, ct.byref(record), ct.sizeof(record))
            ct.cast(count_pointer, ct.POINTER(ct.c_uint32))[0] = 1
            return 1
        self.api.GetConsoleMode.side_effect = get_mode
        self.api.SetConsoleMode.side_effect = lambda handle, mode: self.modes.append(mode) or 1
        self.api.GetNumberOfConsoleInputEvents.side_effect = count
        self.api.ReadConsoleInputW.side_effect = read
        self.loader = patch.object(ct, 'WinDLL', return_value=self.api, create=True)
        self.loader.start()
    def tearDown(self):
        self.loader.stop()
    def test_structure_layout_matches_windows_abi(self):
        self.assertEqual(ct.sizeof(win.KeyEventRecord), 16)
        self.assertEqual(ct.sizeof(win.InputRecord), 20)
        self.assertEqual(win.InputRecord.event.offset, 4)
    def test_empty_queue_never_calls_blocking_read(self):
        with win.WindowsConsoleInput() as keyboard:
            self.assertIsNone(keyboard.poll())
        self.api.ReadConsoleInputW.assert_not_called()
    def test_unicode_and_non_keyboard_events_are_consumed(self):
        self.queue.extend([event(kind=2), event('ф', down=False), event('ф'), event('я', repeat=3)])
        with win.WindowsConsoleInput() as keyboard:
            self.assertEqual(keyboard.poll().repeat, 0)
            self.assertEqual(keyboard.poll().repeat, 0)
            self.assertEqual(keyboard.poll().char, 'ф')
            self.assertEqual(keyboard.poll(), win.KeyStroke('я', 0, 3))
            self.assertIsNone(keyboard.poll())
    def test_modes_restored_even_on_interrupt(self):
        with self.assertRaises(KeyboardInterrupt):
            with win.WindowsConsoleInput():
                raise KeyboardInterrupt
        self.assertTrue(self.modes[0] & 1)
        self.assertFalse(self.modes[0] & (0x40 | 2 | 4))
        self.assertEqual(self.modes[-1], 0x47)


if __name__ == '__main__':
    unittest.main()
