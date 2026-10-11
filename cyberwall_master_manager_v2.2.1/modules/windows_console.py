"""Неблокирующее чтение Unicode-событий настоящей консоли Windows.

ReadConsoleInputW вызывается только при непустой очереди и читает одну запись,
чтобы не забрать ввод следующего запроса после Enter. CRT kbhit/getwch не нужны.
"""
from dataclasses import dataclass
import ctypes as ct


class KeyEventRecord(ct.Structure):
    _fields_ = [("down", ct.c_int32), ("repeat", ct.c_uint16),
                ("virtual_key", ct.c_uint16), ("scan", ct.c_uint16),
                ("unicode_char", ct.c_uint16), ("control", ct.c_uint32)]


class EventUnion(ct.Union):
    _fields_ = [("key", KeyEventRecord), ("padding", ct.c_uint32 * 4)]


class InputRecord(ct.Structure):
    _fields_ = [("event_type", ct.c_uint16), ("event", EventUnion)]


@dataclass(frozen=True)
class KeyStroke:
    char: str = ""
    key: int = 0
    repeat: int = 1


class WindowsConsoleInput:
    def __init__(self):
        self.api = ct.WinDLL("kernel32", use_last_error=True)
        self.api.GetStdHandle.argtypes = [ct.c_uint32]
        self.api.GetStdHandle.restype = ct.c_void_p
        self.api.GetConsoleMode.argtypes = [ct.c_void_p, ct.POINTER(ct.c_uint32)]
        self.api.GetConsoleMode.restype = ct.c_int32
        self.api.SetConsoleMode.argtypes = [ct.c_void_p, ct.c_uint32]
        self.api.SetConsoleMode.restype = ct.c_int32
        self.api.GetNumberOfConsoleInputEvents.argtypes = [ct.c_void_p, ct.POINTER(ct.c_uint32)]
        self.api.GetNumberOfConsoleInputEvents.restype = ct.c_int32
        self.api.ReadConsoleInputW.argtypes = [ct.c_void_p, ct.POINTER(InputRecord),
                                             ct.c_uint32, ct.POINTER(ct.c_uint32)]
        self.api.ReadConsoleInputW.restype = ct.c_int32
        self.handle = self.api.GetStdHandle(0xfffffff6)  # STD_INPUT_HANDLE = -10
        self.saved_mode = ct.c_uint32()
        self._check(self.api.GetConsoleMode(self.handle, ct.byref(self.saved_mode)))
        # Сохраняем системную обработку Ctrl+C. Quick Edit не должен останавливать
        # вывод при случайном выделении мышью. Исходный режим восстанавливается.
        mode = (self.saved_mode.value | 0x0080 | 0x0001) & ~(0x0002 | 0x0004 | 0x0040 | 0x0200)
        self._check(self.api.SetConsoleMode(self.handle, mode))
        self.high_surrogate = None

    @staticmethod
    def _check(success):
        if not success:
            raise ct.WinError(ct.get_last_error())

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.api.SetConsoleMode(self.handle, self.saved_mode.value)

    def poll(self):
        count = ct.c_uint32()
        self._check(self.api.GetNumberOfConsoleInputEvents(self.handle, ct.byref(count)))
        if not count.value:
            return None  # Не вызываем блокирующее чтение на пустой очереди.
        record = InputRecord()
        read = ct.c_uint32()
        self._check(self.api.ReadConsoleInputW(self.handle, ct.byref(record), 1, ct.byref(read)))
        if not read.value or record.event_type != 1 or not record.event.key.down:
            return KeyStroke(repeat=0)  # Mouse/focus/resize/key-up: потреблены, но не печатаются.
        key = record.event.key
        code = key.unicode_char
        if 0xD800 <= code <= 0xDBFF:
            self.high_surrogate = code
            return KeyStroke(repeat=0)
        if 0xDC00 <= code <= 0xDFFF and self.high_surrogate is not None:
            code = 0x10000 + ((self.high_surrogate - 0xD800) << 10) + code - 0xDC00
        self.high_surrogate = None
        return KeyStroke(chr(code) if code else "", key.virtual_key, max(1, key.repeat))
