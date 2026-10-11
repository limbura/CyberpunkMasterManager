"""Опциональный RFID и ввод с клавиатуры без фоновых потоков input()."""
from __future__ import annotations

from dataclasses import dataclass
import os
import queue
import select
import shutil
import sys
import threading
import time

try:
    import wcopy_reader as rfid
except (ImportError, OSError):
    rfid = None


@dataclass(frozen=True)
class ScannedCard:
    number: int


class CardConsole:
    def __init__(self, enabled=True):
        self.enabled = enabled and rfid is not None
        self.events = queue.Queue()
        self.condition = threading.Condition()
        self.io_lock = threading.Lock()
        self.active = False
        self.epoch = 0
        self.stopping = False
        self.thread = None
        if self.enabled:
            self.thread = threading.Thread(target=self._worker, daemon=True)
            self.thread.start()

    def _send(self, epoch, value):
        with self.condition:
            if self.active and epoch == self.epoch:
                self.events.put((epoch, value))

    def _worker(self):
        last_uid = None
        absent = 0
        last_error = None
        feedback_ok = True
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.active or self.stopping)
                if self.stopping:
                    return
                epoch = self.epoch
            delay = 0.25
            with self.io_lock:
                with self.condition:
                    if not self.active or epoch != self.epoch:
                        continue
                present = None
                card = None
                try:
                    card = rfid.read_card(timeout_ms=750)
                    present = True
                    absent = 0
                    last_error = None
                except rfid.NoCardError:
                    absent += 1
                    if absent >= 2:
                        last_uid = None
                    present = last_uid is not None
                    last_error = None
                except rfid.RFIDError as error:
                    absent = 0  # USB-сбой не означает, что карту убрали.
                    signature = (error.code, error.stage, error.status)
                    if signature != last_error:
                        self._send(epoch, f"Считыватель: {error}. Можно продолжать с клавиатуры.")
                    last_error = signature
                    delay = 1.5
                if present is not None and feedback_ok:
                    try:
                        # Нужны для штатного цвета и писка прошивки. Дополнительного beep нет.
                        rfid.set_led("green" if present else "red", timeout_ms=750)
                    except rfid.RFIDError as error:
                        feedback_ok = False
                        self._send(epoch, f"Индикация недоступна: {error}. Чтение продолжается.")
                if card is not None and card.uid != last_uid:
                    # Не теряем карту, если ручной ввод завершил предыдущий запрос.
                    with self.condition:
                        if self.active and epoch == self.epoch:
                            last_uid = card.uid
                            self.events.put((epoch, ScannedCard(card.number)))
            with self.condition:
                if not self.stopping:
                    self.condition.wait(timeout=delay)
                if self.stopping:
                    return

    def _start_scan(self):
        with self.condition:
            self.epoch += 1
            self.active = True
            self.condition.notify_all()
            return self.epoch

    def _stop_scan(self):
        with self.condition:
            self.active = False
            self.condition.notify_all()

    def _event(self, epoch):
        while True:
            try:
                current, value = self.events.get_nowait()
            except queue.Empty:
                return None
            if current == epoch:
                return value

    def read(self, prompt, allow_card=False):
        # input() работает даже без терминала, HID и самого модуля считывателя.
        if not allow_card or not self.enabled or not sys.stdin.isatty():
            return input(prompt)
        epoch = self._start_scan()
        try:
            if os.name == "nt":
                return self._windows_read(prompt, epoch)
            return self._posix_read(prompt, epoch)
        finally:
            self._stop_scan()

    def _windows_read(self, prompt, epoch):
        from windows_console import WindowsConsoleInput
        try:
            keyboard = WindowsConsoleInput()
        except OSError as error:
            self._stop_scan()
            print(f"Автоввод карты в этой консоли недоступен: {error}. Введи поиск вручную.")
            return input(prompt)
        with keyboard:
            return self._windows_edit(prompt, epoch, keyboard)

    def _windows_edit(self, prompt, epoch, keyboard):
        chars = []
        position = 0
        print(prompt)
        previous_width = 0

        def redraw():
            nonlocal previous_width
            # Одна короткая видимая строка: длинный ввод не переносит курсор
            # между строками и не печатает всю подсказку заново после каждой буквы.
            width = max(8, shutil.get_terminal_size((100, 25)).columns - 4)
            start = max(0, position - width + 1)
            visible = "".join(chars[start:start + width])
            suffix = " " * max(0, previous_width - len(visible))
            cursor_text = "".join(chars[start:position])
            sys.stdout.write("\r> " + visible + suffix + "\r> " + cursor_text)
            sys.stdout.flush()
            previous_width = len(visible)

        redraw()
        while True:
            changed = False
            # Даже большой paste/поток служебных событий не вытесняет обработку карты.
            for _ in range(64):
                key = keyboard.poll()
                if key is None:
                    break
                if key.repeat == 0:
                    continue
                char = key.char
                if char in ("\r", "\n") or key.key == 13:
                    redraw()
                    print()
                    return "".join(chars)
                if char == "\x03":
                    raise KeyboardInterrupt
                if char == "\x1a":
                    raise EOFError
                for _ in range(min(key.repeat, 4096)):
                    before = position
                    if key.key == 37:
                        position = max(0, position - 1)
                    elif key.key == 39:
                        position = min(len(chars), position + 1)
                    elif key.key == 36:
                        position = 0
                    elif key.key == 35:
                        position = len(chars)
                    elif key.key == 46 and position < len(chars):
                        chars.pop(position)
                        changed = True
                    elif char == "\b" or key.key == 8:
                        if position:
                            chars.pop(position - 1)
                            position -= 1
                    elif char and char.isprintable():
                        chars.insert(position, char)
                        position += 1
                    changed = changed or before != position
            event = self._event(epoch)
            if isinstance(event, ScannedCard):
                print(f"\nПрочитана карта: card_id {event.number}")
                return event
            if event is not None:
                print("\n" + event)
                previous_width = 0
                changed = True
            if changed:
                redraw()  # Максимум одна перерисовка за цикл, а не на каждый символ.
            time.sleep(0.02)

    def _posix_read(self, prompt, epoch):
        import termios
        print(prompt, end="", flush=True)
        while True:
            ready, _, _ = select.select([sys.stdin], [], [], 0.05)
            if ready:
                line = sys.stdin.readline()
                if not line:
                    raise EOFError
                return line.rstrip("\n")
            event = self._event(epoch)
            if isinstance(event, ScannedCard):
                termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
                print(f"\nПрочитана карта: card_id {event.number}")
                return event
            if event is not None:
                print("\n" + event)
                print(prompt, end="", flush=True)

    def write_number(self, number, beep_t=10):
        if not self.enabled:
            raise RuntimeError("Работа со считывателем отключена или отсутствует wcopy_reader.py.")
        self._stop_scan()
        # Дожидаемся завершения начавшегося чтения; записи и опроса одновременно нет.
        with self.io_lock:
            result = rfid.write_card_number(number)
            warning = None
            try:
                rfid.beep(t=beep_t)
            except rfid.RFIDError as error:
                warning = str(error)
            return result, warning

    def close(self):
        with self.condition:
            self.active = False
            self.stopping = True
            self.condition.notify_all()
        if self.thread is not None:
            self.thread.join(timeout=1)
