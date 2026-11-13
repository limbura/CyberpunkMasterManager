"""Конвейер записи карт: фиксированный список, проверка наклейки, устойчивый журнал."""
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sys
import time
import uuid

from . import printer_menu

READY = {"written", "already"}
STATES = READY | {"pending", "skipped", "error", "writing", "missing"}
TITLES = {"pending": "Ожидает записи", "skipped": "Пропущен", "error": "Нужна повторная проверка",
          "writing": "Прошлая операция не подтверждена", "missing": "Аккаунт удалён",
          "written": "ЗАПИСАНО И ПРОВЕРЕНО", "already": "КАРТА УЖЕ СООТВЕТСТВУЕТ АККАУНТУ"}


class BatchError(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def safe(value):
    return "".join(c if c.isprintable() else " " for c in str(value))


def number(text):
    value = "".join(text.split())
    if not value or not value.isascii() or not value.isdecimal():
        raise BatchError("Введи номер цифрами; пробелы между разрядами допустимы.")
    return int(value)


def identity(row):
    return {key: row[key] for key in ("card_id", "id", "name")}


def clear_screen():
    if sys.stdout.isatty():
        if os.name == "nt":
            os.system("cls")  # Только фиксированная команда, без пользовательского ввода.
        else:
            print("\033[2J\033[3J\033[H", end="", flush=True)


@contextmanager
def journal_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + ".lock").open("a+b") as handle:
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b"0"); handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise BatchError("Этот журнал уже используется другой копией программы.") from error
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


class Journal:
    def __init__(self, path, data):
        self.path, self.data = Path(path), data

    @classmethod
    def create(cls, path, scope, rows):
        rows = sorted(rows, key=lambda row: row["card_id"])
        if not rows:
            raise BatchError("В БД нет аккаунтов для записи.")
        data = {"format": 2, "scope": scope, "created": now(),
                "accounts": [dict(identity(row), status="pending") for row in rows],
                "current": rows[0]["card_id"], "remove_uid": None, "uids": {}, "history": []}
        journal = cls(path, data)
        journal.validate(scope)
        return journal

    @classmethod
    def load(cls, path, scope):
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            journal = cls(path, data)
            journal.validate(scope)
            if data["format"] == 1:
                data["format"] = 2
                if data["remove_uid"] and journal.current()["status"] in READY:
                    journal.current()["label_phase"] = "needed"
            return journal
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            raise BatchError(f"Журнал не загружен: {error} Он не будет перезаписан.") from error

    def validate(self, scope):
        d = self.data
        if d["format"] not in (1, 2) or d["scope"] != scope:
            raise BatchError("Другая версия журнала или другое подключение к БД.")
        rows = d["accounts"]
        if not isinstance(rows, list) or not rows:
            raise BatchError("Пустой или некорректный список аккаунтов.")
        ids = []
        for row in rows:
            if (type(row["card_id"]) is not int or not 0 < row["card_id"] < 2**64
                    or type(row["id"]) is not int or not 100000000 <= row["id"] <= 999999999
                    or not isinstance(row["name"], str) or row["status"] not in STATES):
                raise BatchError("Некорректная запись аккаунта в журнале.")
            if "label_phase" in row and (row["label_phase"] not in ("needed", "sending", "sent")
                    or row["status"] not in READY or row["card_id"] != d["current"]
                    or not d["remove_uid"] or d["remove_uid"] != row.get("uid")):
                raise BatchError("Некорректный этап печати наклейки в журнале.")
            ids.append(row["card_id"])
        if ids != sorted(set(ids)) or (d["current"] is not None and d["current"] not in ids):
            raise BatchError("Некорректный порядок или текущий card_id.")
        if not isinstance(d["uids"], dict) or not isinstance(d["history"], list):
            raise BatchError("Некорректный журнал операций.")
        for uid, owner in d["uids"].items():
            if not isinstance(uid, str) or len(bytes.fromhex(uid)) not in (4, 7) or type(owner) is not int or owner not in ids:
                raise BatchError("Некорректная привязка UID.")
        for row in rows:
            if row["status"] in READY | {"writing"} and d["uids"].get(row.get("uid")) != row["card_id"]:
                raise BatchError("Нет сохранённого UID для результата записи.")
        if d["remove_uid"] is not None and (d["remove_uid"] not in d["uids"]
                or d["uids"][d["remove_uid"]] != d["current"]):
            raise BatchError("Некорректное ожидание снятия карты.")

    def save(self):
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(self.data, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def event(self, row, result, **details):
        self.data["history"].append(dict(time=now(), **identity(row), result=result, **details))

    def current(self):
        return next((row for row in self.data["accounts"] if row["card_id"] == self.data["current"]), None)

    def advance(self):
        current = self.data["current"]
        self.data["current"] = next((row["card_id"] for row in self.data["accounts"]
            if row["card_id"] > current and row["status"] not in READY | {"skipped"}), None)
        self.save()

    def reserve(self, row, card):
        uid = card.uid.hex()
        owner = self.data["uids"].get(uid)
        if owner is not None and owner != row["card_id"]:
            raise BatchError(f"Этот UID уже использовался для card_id {owner}. Возьми другую карту.")
        self.data["uids"][uid] = row["card_id"]
        row["status"] = "writing"
        row["uid"] = uid
        self.event(row, "writing", uid=uid, old_number=card.number)
        self.save()  # До аппаратной записи: сбой диска запрещает продолжать.

    def finish(self, row, status):
        row["status"] = status
        row.pop("error", None)
        self.data["remove_uid"] = row["uid"]
        row["label_phase"] = "needed"
        self.event(row, status, uid=row["uid"])
        self.save()


def header(journal, row, message=""):
    clear_screen()
    width = max(20, shutil.get_terminal_size((100, 25)).columns - 1)
    counts = Counter(item["status"] for item in journal.data["accounts"])
    lines = ["ПАКЕТНАЯ ЗАПИСЬ КАРТ", "",
             f"card_id: {row['card_id']}" if row else "card_id: —",
             f"ID:      {row['id']:,}".replace(",", " ") if row else "ID:      —",
             "Имя:     " + safe(row["name"]) if row else "Имя:     —", "",
             "Статус: " + (TITLES[row["status"]] if row else "Конец списка"),
             f"Записано: {counts['written']} | Уже соответствовало: {counts['already']} | "
             f"Пропущено: {counts['skipped']} | Проблемы: {counts['error'] + counts['writing'] + counts['missing']}",
             safe(message)]
    for line in lines:
        print(line[:width])  # Каждое поле занимает ровно одну строку.


def scan(reader):
    return reader.read_card()


def wait_removed(reader, journal):
    print("\nУБЕРИ КАРТУ. Следующий аккаунт появится после снятия. Ctrl+C — выйти.")
    last_error = None
    while True:
        try:
            scan(reader)
        except reader.NoCardError:
            if reader.is_card_removed():
                break
            last_error = None
        except reader.RFIDError as error:
            if str(error) != last_error:
                print("Ожидаем считыватель: " + safe(error))
                last_error = str(error)
        time.sleep(0.25)
    journal.data["remove_uid"] = None
    if journal.current()["status"] in READY:
        journal.advance()
    else:
        journal.save()


def sync_account(db, journal, row):
    current = db.batch_account(row["card_id"])
    if current is None:
        row.pop("label_phase", None)
        row["status"] = "missing"
        journal.save()
        return "Аккаунт удалён из БД. Можно пропустить его."
    if identity(current) != identity(row):
        if row.get("label_phase") and journal.data["remove_uid"]:
            row.update(identity(current), label_phase="needed")
        else:
            row.update(identity(current), status="pending")
        journal.event(row, "account_changed")
        journal.save()
        return "Данные в БД изменились. Проверь новые ID и имя."
    if row["status"] == "missing":
        row["status"] = "pending"
        journal.save()
    return ""


def attempt(db, journal, row, reader, ask, beep_t, card=None):
    if card is None:
        card = scan(reader)
    uid_owner = journal.data["uids"].get(card.uid.hex())
    if uid_owner is not None and uid_owner != row["card_id"]:
        raise BatchError(f"Этот UID уже использовался для card_id {uid_owner}. Возьми другую карту.")
    if card.number != row["card_id"]:
        owner = db.batch_account(card.number) if card.number else None
        if owner:
            print(f"НА КАРТЕ ДРУГОЙ АККАУНТ: card_id {owner['card_id']}, ID {owner['id']}, {safe(owner['name'])}")
            if ask("Для перезаписи введи ПЕРЕЗАПИСАТЬ (иначе отмена): ").strip().upper() != "ПЕРЕЗАПИСАТЬ":
                return "Перезапись отменена. Текущий аккаунт сохранён."
        elif card.number:
            print(f"В памяти неизвестный номер {card.number}; он будет заменён.")
            if ask("Enter — заменить; любой текст — отмена: ").strip():
                return "Перезапись отменена."
    # Блокируем строку аккаунта лишь на время проверки/записи, без ожидания ввода.
    with db.batch_guard(identity(row)):
        journal.reserve(row, card)
        try:
            if card.number == row["card_id"]:
                check = scan(reader)
                if check.uid != card.uid or check.block != card.block:
                    raise BatchError("Карта или её содержимое изменились. Успех не подтверждён.")
                status = "already"
            else:
                value = reader.write_card_number(row["card_id"], expected_uid=card.uid, expected_block=card.block)
                if value != row["card_id"]:
                    raise BatchError("Обратное чтение вернуло другой номер.")
                status = "written"
        except BaseException as error:
            row["status"] = "error"
            row["error"] = safe(error) or type(error).__name__
            journal.event(row, "error", uid=card.uid.hex(), error=row["error"])
            journal.save()
            raise
        journal.finish(row, status)
    return "Карта проверена."


def report(journal, only_skipped=False):
    clear_screen()
    print("ПРОПУЩЕННЫЕ АККАУНТЫ" if only_skipped else "ИТОГИ РАБОТЫ")
    for row in journal.data["accounts"]:
        if only_skipped and row["status"] != "skipped":
            continue
        print(f"card_id {row['card_id']} | ID {row['id']} | {safe(row['name'])} | {TITLES[row['status']]}")
    print("\nПолный журнал: " + str(journal.path))


PRINTER_ACTIONS = (
    "3 — калибровка этикеток",
    "4 <мм> — подать бумагу вперёд",
    "5 <мм> — подать бумагу назад (только со снятой этикеткой!)",
)


def card_actions(row, auto_scan):
    actions = []
    if row and row["status"] not in READY | {"missing"}:
        if auto_scan:
            actions.append("Приложи карту для автоматической записи")
        actions.append("Enter — проверить карту")
    actions.extend(PRINTER_ACTIONS)
    if row:
        if row["status"] in READY:
            actions.append(":r — разрешить повторную запись")
        else:
            actions.append(":skip — пропустить аккаунт")
        actions.append(":next — перейти дальше")
    actions.extend((":card N — перейти к card_id", ":id N — перейти к ID",
                    ":skipped — показать пропущенные", ":report — показать итоги"))
    return actions


def read_with_maintenance(actions, read, printer, ask, **read_options):
    while True:
        print("\nДоступные действия", *actions, ":b, :q — сохранить и выйти", sep="\n")
        value = read("Действие: ", **read_options)
        if not isinstance(value, str) or not printer_menu.maintenance(printer, value, ask):
            return value


def confirm_label(journal, row, printer, ask, *, send_now, previous_removed=False):
    # Перед отправкой сохраняем неопределённый результат на случай обрыва связи.
    while True:
        if send_now:
            row["label_phase"] = "sending"
            journal.save()
            sent = printer_menu.print_label(printer, str(row["id"]), row["name"], ask=ask, previous_removed=previous_removed)
            previous_removed = False
            row["label_phase"] = "sent" if sent else "needed"
            journal.save()
        can_confirm = row["label_phase"] in ("sending", "sent")
        actions = ["1 — подтвердить, что наклейка напечаталась и приклеена"] if can_confirm else []
        actions.extend(("2 — повторить печать", *PRINTER_ACTIONS))
        answer = read_with_maintenance(actions, ask, printer, ask).strip().lower()
        if answer in (":b", ":q"):
            return False
        if answer == "1" and can_confirm:
            row.pop("label_phase")
            journal.event(row, "label_attached", uid=row["uid"])
            journal.save()
            return True
        send_now = answer == "2"


def process(db, journal, reader, ask=input, beep_t=10, *, printer=None, read_card_or_command=None):
    message = ""
    previous_removed = False

    def poll_card():
        try:
            return scan(reader)
        except reader.NoCardError:
            return None

    while True:
        row = journal.current()
        if row:
            message = sync_account(db, journal, row) or message
        if journal.data["remove_uid"]:
            header(journal, row, message)
            if row.get("label_phase"):
                if not confirm_label(journal, row, printer, ask, send_now=False):
                    return
                previous_removed = True
            wait_removed(reader, journal)
            message = ""
            continue
        header(journal, row, message)
        card = None
        if read_card_or_command and row and row["status"] not in READY | {"missing", "error", "writing"}:
            try:
                value = read_with_maintenance(card_actions(row, auto_scan=True),
                                              read_card_or_command, printer, ask, poll_card=poll_card)
            except reader.RFIDError as error:
                row["status"] = "error"
                journal.save()
                message = safe(error)
                continue
            if isinstance(value, str):
                command = value.strip()
            else:
                card, command = value, ""
        else:
            command = read_with_maintenance(card_actions(row, auto_scan=False),
                                            ask, printer, ask).strip()
        lower = command.lower()
        message = ""
        if lower in (":b", ":q"):
            return
        if lower in (":report", ":skipped"):
            report(journal, lower == ":skipped")
            command = ask("card_id — перейти к аккаунту\nEnter — назад\nДействие: ").strip()
            if not command:
                continue
            lower = ":card " + command
        try:
            if lower.startswith((":card ", ":id ")):
                key = "card_id" if lower.startswith(":card ") else "id"
                target = number(lower.split(" ", 1)[1])
                # Поиск ID делаем по свежей БД, затем проверяем принадлежность списку.
                found = db.batch_by_id(target) if key == "id" else {"card_id": target}
                target_row = next((r for r in journal.data["accounts"]
                                  if found and r["card_id"] == found["card_id"]), None)
                if not target_row:
                    raise BatchError("Аккаунта нет в сохранённом списке. Пропуски card_id не являются позициями.")
                journal.data["current"] = target_row["card_id"]
                journal.save()
                continue
            if row is None:
                raise BatchError("Список закончен. Открой :report или перейди к нужному card_id.")
            if lower == ":next":
                journal.advance()
                continue
            if lower == ":skip":
                if row["status"] not in READY:
                    row["status"] = "skipped"
                    journal.event(row, "skipped")
                journal.advance()
                continue
            if lower == ":r":
                if row["status"] in READY:
                    row["status"] = "pending"
                    journal.event(row, "repeat_allowed")
                    journal.save()
                    message = "Повтор разрешён. Приложи карту."
                continue
            if row["status"] in READY:
                raise BatchError("Аккаунт уже готов. Для повторной записи сначала выбери :r.")
            if row["status"] == "missing":
                raise BatchError("Удалённый аккаунт записать нельзя.")
            if command:
                raise BatchError("Неизвестная команда.")
            message = attempt(db, journal, row, reader, ask, beep_t, card)
            if journal.data["remove_uid"]:
                print(message)
                if not confirm_label(journal, row, printer, ask, send_now=True, previous_removed=previous_removed):
                    return
                previous_removed = True
            elif not command:
                # После отмены не запрашиваем перезапись той же карты автоматически.
                row["status"] = "error"
                journal.save()
        except (BatchError, reader.RFIDError) as error:
            message = safe(error) + " Текущий аккаунт сохранён."
            if row:
                journal.event(row, "attempt_rejected", error=safe(error))
                if row["status"] not in READY | {"skipped", "missing"}:
                    row["status"] = "error"
                journal.save()


def run(db, console, path, scope, *, ask=input, beep_t=10, printer=None):
    try:
        with journal_lock(path), console.batch_mode() as reader:
            journal = Journal.load(path, scope) if Path(path).exists() else None
            clear_screen()
            print("ПАКЕТНАЯ ЗАПИСЬ КАРТ")
            print("1 — продолжить работу" if journal else "Сохранённой работы нет.")
            print("2 — начать новую работу\n0, Enter, :b — назад")
            choice = ask("Действие: ").strip().lower()
            if choice == "1" and journal:
                pass
            elif choice == "2":
                fresh = Journal.create(path, scope, db.batch_accounts())
                if journal:
                    if ask("Начать заново? Введи НОВАЯ (старый журнал будет сохранён): ").strip().upper() != "НОВАЯ":
                        return
                    backup = Path(path).with_name(Path(path).stem + "-" + uuid.uuid4().hex[:12] + ".json")
                    shutil.copy2(path, backup)
                journal = fresh
                journal.save()
            else:
                return
            try:
                process(db, journal, reader, ask, beep_t, printer=printer, read_card_or_command=console.read)
            finally:
                # Не перезаписываем журнал в finally: предыдущий сбой диска мог
                # оставить только безопасное намерение вместо результата операции.
                print("\nПрогресс хранится в " + str(path))
    except (KeyboardInterrupt, EOFError):
        print("\nПакетная запись остановлена. Выбери «продолжить работу» при следующем входе.")
