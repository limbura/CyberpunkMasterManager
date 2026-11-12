"""Опциональный RFID и ввод с клавиатуры без фоновых потоков input()."""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import os
import queue
import shutil
import sys
import threading
import time

from .settings import SETTINGS, app_path
from .input_history import HistoryCursor

READER_SETTINGS = SETTINGS["reader"]
READER_IMPORT_ERROR = None
try:
    if getattr(sys, "frozen", False):
        # В готовой сборке API и hidapi упакованы PyInstaller; внешний .py не нужен.
        import wcopy_reader as rfid
    else:
        # Независимая папка библиотеки. Не ищем случайную другую версию в PATH.
        import importlib.util
        reader_file = app_path(READER_SETTINGS["library_path"]) / "wcopy_reader.py"
        if not reader_file.is_file():
            raise ImportError(f"Не найден {reader_file}")
        spec = importlib.util.spec_from_file_location("wcopy_reader", reader_file)
        rfid = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = rfid
        spec.loader.exec_module(rfid)
    if not hasattr(rfid, "is_card_removed"):
        raise ImportError("Для автоматической индикации нужна wcopy_reader 1.6.0 или новее.")
except (ImportError, OSError) as error:
    READER_IMPORT_ERROR = str(error)
    rfid = None


class CardNotPresentError(RuntimeError):
    """Свежий опрос перед записью не обнаружил карту; команда записи не отправлялась."""


@dataclass(frozen=True)
class ScannedCard:
    number: int


class CardConsole:
    def __init__(self, enabled=True):
        self.enabled = enabled and rfid is not None
        self.histories = {}
        self.events = queue.Queue()
        self.condition = threading.Condition()
        self.io_lock = threading.Lock()
        self.active = False  # Разрешение выбирать аккаунты, НЕ разрешение опроса USB.
        self.write_in_progress = False
        self.wakeup = threading.Event()
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
        last_selected_presentation = None
        last_error = None
        while True:
            with self.condition:
                self.condition.wait_for(lambda: not self.write_in_progress or self.stopping)
                if self.stopping:
                    return
            delay = READER_SETTINGS["poll_interval"]
            with self.io_lock:
                with self.condition:
                    if self.write_in_progress or self.stopping:
                        continue
                    epoch = self.epoch
                card = None
                try:
                    card = rfid.read_card(timeout_ms=READER_SETTINGS["timeout_ms"])
                    last_error = None
                except rfid.NoCardError:
                    last_error = None
                except rfid.RFIDError as error:
                    signature = (error.code, error.stage, error.status)
                    if signature != last_error:
                        self._send(epoch, f"Считыватель: {error}. Можно продолжать с клавиатуры.")
                    last_error = signature
                    delay = READER_SETTINGS["error_retry_interval"]
                if card is not None and card.presentation_id != last_selected_presentation:
                    with self.condition:
                        if self.active and epoch == self.epoch:
                            last_selected_presentation = card.presentation_id
                            self.events.put((epoch, ScannedCard(card.number)))
            self.wakeup.wait(delay)
            self.wakeup.clear()
            with self.condition:
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

    def read(self, prompt, allow_card=False, history=None, *, poll_card=None):
        # История только в памяти; None полностью отключает её для этого поля.
        entries = self.histories.setdefault(history, []) if history is not None else None
        epoch = self._start_scan() if allow_card and self.enabled and sys.stdin.isatty() else None
        try:
            if not sys.stdin.isatty():
                value = input(prompt)
            elif os.name == "nt":
                value = self._windows_read(prompt, epoch, entries, poll_card=poll_card)
            else:
                value = self._posix_read(prompt, epoch, entries, poll_card=poll_card)
            if isinstance(value, str) and entries is not None and value.strip():
                if not entries or entries[-1] != value:
                    entries.append(value)
                    del entries[:-100]
            return value
        finally:
            if epoch is not None:
                self._stop_scan()

    def _windows_read(self, prompt, epoch, entries=None, *, poll_card=None):
        from .windows_console import WindowsConsoleInput
        try:
            keyboard = WindowsConsoleInput()
        except OSError:
            self._stop_scan()
            print("Расширенный ввод в этой консоли недоступен. Введи строку вручную.")
            return input(prompt)
        with keyboard:
            return self._windows_edit(prompt, epoch, keyboard, entries, poll_card=poll_card)

    def _windows_edit(self, prompt, epoch, keyboard, entries=None, *, poll_card=None):
        chars = []
        history = HistoryCursor(entries or [])
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

        next_poll = 0.0
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
                    if key.key in (38, 40):
                        chars = list(history.move(-1 if key.key == 38 else 1, "".join(chars)))
                        position = len(chars)
                        changed = True
                    elif key.key == 37:
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
            if poll_card and not chars and time.monotonic() >= next_poll:
                card = poll_card()
                if card is not None:
                    print()
                    return card
                next_poll = time.monotonic() + 0.25
            event = self._event(epoch) if epoch is not None else None
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

    def _posix_read(self, prompt, epoch, entries=None, *, poll_card=None):
        from .posix_console import PosixConsoleInput
        with PosixConsoleInput() as keyboard:
            return self._windows_edit(prompt, epoch, keyboard, entries, poll_card=poll_card)

    def write_number(self, number, beep_t=10):
        """Запись с автоматической индикацией библиотеки; старый beep_t игнорируется."""
        if not self.enabled:
            raise RuntimeError("Работа со считывателем отключена или отсутствует модуль wcopy_reader.")
        self._stop_scan()
        with self.condition:
            self.write_in_progress = True
        try:
            # Ждём текущего опроса. Затем запись сама заново читает карту и
            # проверяет её наличие: сохранённое состояние/UID не используются.
            with self.io_lock:
                try:
                    result = rfid.write_card_number(number)
                except rfid.NoCardError as error:
                    raise CardNotPresentError("Карта не обнаружена. Запись не выполнялась.") from error
                return result, None
        finally:
            # В том числе при NoCardError, сбое записи и Ctrl+C: сразу возвращаем
            # опрос и автоматическую индикацию библиотеки, не ждём нового поиска, тайм-аута или перезапуска.
            with self.condition:
                self.write_in_progress = False
                self.condition.notify_all()
            self.wakeup.set()

    @contextmanager
    def batch_mode(self):
        """Один владелец USB на время конвейера; фон не вмешивается в проверки."""
        if not self.enabled:
            raise RuntimeError("Пакетная запись требует включённой библиотеки считывателя.")
        import inspect
        if not {"expected_uid", "expected_block"} <= set(inspect.signature(rfid.write_card_number).parameters):
            raise RuntimeError("Для пакетной записи нужна wcopy_reader 1.6.0 или новее. Обнови библиотеку.")
        self._stop_scan()
        with self.condition:
            self.write_in_progress = True
        try:
            with self.io_lock:
                yield rfid
        finally:
            with self.condition:
                self.write_in_progress = False
                self.condition.notify_all()
            self.wakeup.set()

    def close(self):
        with self.condition:
            self.active = False
            self.stopping = True
            self.condition.notify_all()
        self.wakeup.set()
        if self.thread is not None:
            self.thread.join(timeout=1)
