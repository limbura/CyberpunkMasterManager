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

from .settings import SETTINGS, app_path

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
        self.events = queue.Queue()
        self.condition = threading.Condition()
        self.io_lock = threading.Lock()
        self.active = False  # Разрешение выбирать аккаунты, НЕ разрешение опроса USB.
        self.write_in_progress = False
        self.feedback_retry_at = 0.0
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
        last_selected_uid = None
        absent = 0
        last_error = None
        feedback_error = None
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
                present = None
                card = None
                try:
                    card = rfid.read_card(timeout_ms=READER_SETTINGS["timeout_ms"])
                    present = True
                    absent = 0
                    last_error = None
                except rfid.NoCardError:
                    absent += 1
                    # Это происходит и в меню редактирования: снятие карты не теряется.
                    if absent >= 2:
                        last_selected_uid = None
                    present = False
                    last_error = None
                except rfid.RFIDError as error:
                    absent = 0
                    signature = (error.code, error.stage, error.status)
                    if signature != last_error:
                        self._send(epoch, f"Считыватель: {error}. Можно продолжать с клавиатуры.")
                    last_error = signature
                    delay = READER_SETTINGS["error_retry_interval"]
                if present is not None and time.monotonic() >= self.feedback_retry_at:
                    try:
                        # Команды нужны для штатного цвета и писка прошивки.
                        rfid.set_led("green" if present else "red", timeout_ms=READER_SETTINGS["timeout_ms"])
                        feedback_error = None
                        self.feedback_retry_at = 0.0
                    except rfid.RFIDError as error:
                        signature = (error.code, error.stage, error.status)
                        if signature != feedback_error:
                            self._send(epoch, f"Сбой индикации: {error}. Восстановление будет повторено автоматически.")
                        feedback_error = signature
                        self.feedback_retry_at = time.monotonic() + READER_SETTINGS["feedback_retry_interval"]
                if card is not None and card.uid != last_selected_uid:
                    with self.condition:
                        if self.active and epoch == self.epoch:
                            last_selected_uid = card.uid
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
        from .windows_console import WindowsConsoleInput
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
                warning = None
                try:
                    rfid.beep(t=beep_t)
                except rfid.RFIDError as error:
                    warning = str(error)
                return result, warning
        finally:
            # В том числе при NoCardError, сбое записи и Ctrl+C: сразу возвращаем
            # опрос + LED, не ждём нового поиска, тайм-аута или перезапуска.
            with self.condition:
                self.feedback_retry_at = 0.0
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
