"""Неблокирующая клавиатура Linux/macOS для общего редактора строки."""
import codecs
import os
import select
import sys
import termios
import time
import tty
from .windows_console import KeyStroke


class PosixConsoleInput:
    def __init__(self):
        self.fd = sys.stdin.fileno()
        self.saved = termios.tcgetattr(self.fd)
        self.decoder = codecs.getincrementaldecoder(sys.stdin.encoding or "utf-8")("replace")
        self.escape = ""
        self.escape_at = 0.0

    def __enter__(self):
        tty.setcbreak(self.fd)
        return self

    def __exit__(self, *args):
        termios.tcsetattr(self.fd, termios.TCSANOW, self.saved)

    def poll(self):
        # Не читаем за Enter: следующие символы принадлежат следующему запросу.
        if not select.select([self.fd], [], [], 0)[0]:
            if self.escape and time.monotonic() - self.escape_at > 0.1:
                self.escape = ""
            return None
        data = os.read(self.fd, 1)
        if not data:
            raise EOFError
        char = self.decoder.decode(data)
        if not char:
            return KeyStroke(repeat=0)
        if self.escape or char == "\x1b":
            self.escape += char
            self.escape_at = time.monotonic()
            keys = {"\x1b[A": 38, "\x1b[B": 40, "\x1b[C": 39, "\x1b[D": 37,
                    "\x1b[H": 36, "\x1b[F": 35, "\x1bOH": 36, "\x1bOF": 35,
                    "\x1b[1~": 36, "\x1b[4~": 35, "\x1b[3~": 46}
            key = keys.get(self.escape)
            if key:
                self.escape = ""
                return KeyStroke(key=key)
            if len(self.escape) > 2 and (char.isalpha() or char == "~"):
                self.escape = ""
            return KeyStroke(repeat=0)
        if char == "\x04":
            raise EOFError
        return KeyStroke("\b" if char == "\x7f" else char)
