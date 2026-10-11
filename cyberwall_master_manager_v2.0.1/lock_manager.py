#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CYBERWALL — мастерская: аккаунты, карты и доступ к замкам. Python 3.11+.
Запуск: py lock_manager.py. См. README.md в архиве.
Настройки подключения находятся ниже. Пароль MySQL запрашивается скрыто.
Аккаунт всегда определяется по card_id; main.id меняется только через changeId.
"""

from dataclasses import dataclass
from getpass import getpass
import sys
import json
import os
from pathlib import Path
from card_console import CardConsole, ScannedCard
from typing import Optional

try:
    import pymysql
    from pymysql.cursors import DictCursor
except ImportError:
    print('Не установлен PyMySQL. Выполни: py -m pip install "PyMySQL[rsa]"')
    raise SystemExit(1)


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 3306
DEFAULT_USER = "master"
DATABASE_NAME = "cyberwall"
PAGE_SIZE = 15
READER_ENABLED = True
WRITE_BEEP_T = 10
PENDING_ID_PATH = Path(__file__).resolve().with_name("pending_id_change.json")
CONSOLE = None
ACCOUNT_COLUMNS = "id, card_id, name, money, losa, netrunner_lvl, police"
EDIT_FIELDS = {"name": "имя", "money": "деньги", "losa": "рейт",
               "netrunner_lvl": "ур. деки", "police": "полиц. права", "password": "пароль"}

BUSINESS_ERRORS = {
    1: "Аккаунт с этой картой не найден. Права не изменены.",
    2: "Замок не найден. Права не изменены.",
    3: "Доступ уже назначен. Права не изменены.",
    4: "Доступ уже отсутствует. Права не изменены.",
    5: "Доступ уже назначен. Права не изменены.",
    6: "Доступ уже отсутствует. Права не изменены.",
}


class QuitRequested(Exception):
    pass


class SelectionChanged(Exception):
    pass


class WriteUncertain(Exception):
    """Соединение прервалось: автоматически повторять запись нельзя."""


@dataclass(frozen=True)
class Entity:
    kind: str
    number: Optional[int]        # main.id или locks.lock_id; None для карты без main
    label: Optional[str]
    card_id: Optional[int] = None
    money: Optional[int] = None
    losa: Optional[int] = None
    netrunner_lvl: Optional[int] = None
    police: Optional[int] = None


def clean(value):
    """Не даём переносам, табуляциям и управляющим символам ломать консоль."""
    if value is None:
        return "—"
    return "".join(c if c.isprintable() else " " for c in str(value))


def format_number(value):
    """Только отображение: SQL получает исходные целые числа."""
    if value is None:
        return "—"
    return f"{value:,}".replace(",", " ")


def compact_number(value):
    """Принимает обычные и неразрывные пробелы в скопированных номерах."""
    compact = "".join(value.split())
    if compact.isascii() and compact.isdecimal():
        return compact
    return value


def entity_text(entity, widths=None):
    kind = {"account": "АККАУНТ", "lock": "ЗАМОК", "orphan": "НЕТ В MAIN"}[entity.kind]
    cells = [kind, "lock_id" if entity.kind == "lock" else "ID",
             format_number(entity.number), "card_id", format_number(entity.card_id),
             clean(entity.label) if entity.label else "(без имени / описания)"]
    for field in ("money", "losa", "netrunner_lvl", "police"):
        cells.extend((EDIT_FIELDS[field], format_number(getattr(entity, field))))
    if widths is not None:
        return " | ".join(value.ljust(widths[i]) for i, value in enumerate(cells))
    return " | ".join(cells)


def aligned_entity_lines(entities):
    """Одинаковая ширина всех колонок на всех страницах текущего списка."""
    if not entities:
        return []
    # Ширины считаем по ячейкам, а не по строке: имя может содержать символ |.
    widths = [max(len({"account": "АККАУНТ", "lock": "ЗАМОК", "orphan": "НЕТ В MAIN"}[e.kind]) for e in entities),
              7, max(len(format_number(e.number)) for e in entities), 7,
              max(len(format_number(e.card_id)) for e in entities),
              max(len(clean(e.label) if e.label else "(без имени / описания)") for e in entities)]
    for field in ("money", "losa", "netrunner_lvl", "police"):
        widths.extend((len(EDIT_FIELDS[field]), max(len(format_number(getattr(e, field))) for e in entities)))
    return [entity_text(e, widths) for e in entities]


def read_input(prompt, allow_card=False):
    value = CONSOLE.read(prompt, allow_card=allow_card) if CONSOLE else input(prompt)
    if isinstance(value, ScannedCard):
        return value
    value = value.strip()
    if value.lower() == ":q":
        raise QuitRequested
    return value


def like_pattern(value):
    # ESCAPE '=': %, _ и = ищутся буквально; пользовательский ввод — параметр.
    return "%" + value.replace("=", "==").replace("%", "=%").replace("_", "=_") + "%"


def account_from_row(row):
    return Entity("account", row["id"], row["name"], row["card_id"],
                  row["money"], row["losa"], row["netrunner_lvl"], row["police"])


def lock_from_row(row):
    return Entity("lock", row["lock_id"], row["description"])


class Database:
    def __init__(self, connection):
        self.connection = connection

    def rows(self, sql, args=()):
        with self.connection.cursor() as cursor:
            cursor.execute(sql, args)
            return list(cursor.fetchall())

    def check_read_access(self):
        # Одновременно проверяем права и ожидаемые имена столбцов.
        self.rows("SELECT id, card_id, name, money, losa, netrunner_lvl, police FROM main LIMIT 0")
        self.rows("SELECT lock_id, description FROM locks LIMIT 0")
        self.rows("SELECT lock_id, card_id FROM lock_access LIMIT 0")

    def account_by_card(self, card_id):
        rows = self.rows(f"SELECT {ACCOUNT_COLUMNS} FROM main WHERE card_id = %s", (card_id,))
        return account_from_row(rows[0]) if rows else None

    def field_value(self, card_id, field):
        if field not in EDIT_FIELDS:
            raise ValueError("Недопустимое поле аккаунта.")
        rows = self.rows(f"SELECT `{field}` AS value FROM main WHERE card_id = %s", (card_id,))
        if not rows:
            raise SelectionChanged("Аккаунт больше не найден.")
        return rows[0]["value"]

    def set_field(self, card_id, field, expected, value):
        if field not in EDIT_FIELDS:
            raise ValueError("Недопустимое поле аккаунта.")
        validate_field(field, value)
        try:
            self.connection.begin()
            with self.connection.cursor() as cursor:
                cursor.execute(f"SELECT `{field}` AS value FROM main WHERE card_id = %s FOR UPDATE", (card_id,))
                row = cursor.fetchone()
                if row is None:
                    raise SelectionChanged("Аккаунт больше не найден. Изменение отменено.")
                if row["value"] != expected:
                    raise SelectionChanged("Поле уже изменено другим подключением. Обнови аккаунт и повтори действие.")
                cursor.execute(f"UPDATE main SET `{field}` = %s WHERE card_id = %s", (value, card_id))
            self.connection.commit()
        except BaseException as error:
            self._rollback_write(error)
            raise

    def _rollback_write(self, error):
        try:
            self.connection.rollback()
        except Exception:
            pass
        if isinstance(error, pymysql.MySQLError) and (
            not self.connection.open or (error.args and error.args[0] in (2006, 2013, 2055))
        ):
            raise WriteUncertain(
                "Связь потеряна во время изменения. Результат не подтверждён. "
                "Перезапусти программу и проверь аккаунт. Автоматического повтора нет."
            ) from error

    def change_id(self, card_id):
        # changeId сама управляет транзакцией. Никакого UPDATE main.id и повторов CALL.
        with self.connection.cursor() as cursor:
            cursor.execute("SET @master_new_id = NULL, @master_id_err = NULL")
            cursor.execute("CALL changeId(%s, @master_new_id, @master_id_err)", (card_id,))
            while cursor.nextset():
                pass
            cursor.execute("SELECT @master_new_id AS new_id, @master_id_err AS errCode")
            row = cursor.fetchone()
        if row is None or row["errCode"] is None:
            raise WriteUncertain("changeId не вернула код результата. Проверь текущий ID при следующем запуске.")
        code = int(row["errCode"])
        if code == 1:
            raise SelectionChanged("Аккаунт не найден: changeId вернула код 1.")
        if code != 0 or row["new_id"] is None:
            raise WriteUncertain(f"Неожиданный результат changeId: код {code}. Автоматического повтора нет.")
        number = int(row["new_id"])
        if not 100000000 <= number <= 999999999:
            raise WriteUncertain("changeId вернула ID вне диапазона девятизначных номеров.")
        return number

    def search(self, text, kind=None):
        pattern = like_pattern(text)
        number_text = compact_number(text)
        number_pattern = like_pattern(number_text)
        found = []
        if kind in (None, "account"):
            rows = self.rows("""
                SELECT id, card_id, name, money, losa, netrunner_lvl, police FROM main
                WHERE name LIKE %s ESCAPE '='
                   OR CAST(id AS CHAR) LIKE %s ESCAPE '='
                   OR CAST(card_id AS CHAR) LIKE %s ESCAPE '='
                ORDER BY name, id
            """, (pattern, number_pattern, number_pattern))
            found.extend(account_from_row(row) for row in rows)
        if kind in (None, "lock"):
            rows = self.rows("""
                SELECT lock_id, description FROM locks
                WHERE description LIKE %s ESCAPE '='
                   OR CAST(lock_id AS CHAR) LIKE %s ESCAPE '='
                ORDER BY description, lock_id
            """, (pattern, number_pattern))
            found.extend(lock_from_row(row) for row in rows)
        # Точные совпадения номера/имени показываем перед частичными.
        needle = text.casefold()
        found.sort(key=lambda item: not (
            needle and (
                needle == (item.label or "").casefold()
                or number_text == str(item.number)
                or (item.card_id is not None and number_text == str(item.card_id))
            )
        ))
        return found

    def refresh(self, entity):
        if entity.kind == "lock":
            rows = self.rows("""
                SELECT lock_id, description FROM locks WHERE lock_id = %s
            """, (entity.number,))
            if not rows:
                raise SelectionChanged("Этот замок уже удалён или его номер изменён.")
            return lock_from_row(rows[0])
        if entity.card_id is None:
            raise SelectionChanged("У аккаунта не задан card_id. Выдача доступа невозможна.")
        rows = self.rows("""
            SELECT id, card_id, name, money, losa, netrunner_lvl, police FROM main WHERE card_id = %s
        """, (entity.card_id,))
        if not rows:
            raise SelectionChanged("Аккаунт с выбранным card_id больше не найден.")
        if len(rows) != 1:
            raise SelectionChanged(
                "Один card_id назначен нескольким аккаунтам. Исправь дубликаты в main."
            )
        return account_from_row(rows[0])

    def access_lines(self, entity):
        if entity.kind == "account":
            rows = self.rows("""
                SELECT l.lock_id, l.description
                FROM lock_access AS a
                JOIN locks AS l ON l.lock_id = a.lock_id
                WHERE a.card_id = %s
                ORDER BY l.description, l.lock_id
            """, (entity.card_id,))
            return aligned_entity_lines([lock_from_row(row) for row in rows])
        rows = self.rows("""
            SELECT a.card_id, u.id, u.name, u.money, u.losa, u.netrunner_lvl, u.police
            FROM lock_access AS a
            LEFT JOIN main AS u ON u.card_id = a.card_id
            WHERE a.lock_id = %s
            ORDER BY u.name, a.card_id, u.id
        """, (entity.number,))
        entities = []
        for row in rows:
            if row["id"] is None:
                entities.append(Entity(
                    "orphan", None,
                    "право сохранено, но checkLockAccess запретит проход",
                    row["card_id"],
                ))
            else:
                entities.append(account_from_row(row))
        return aligned_entity_lines(entities)

    def set_access(self, lock_id, card_id, grant):
        """Одна транзакция, без автоматического переподключения и повторной записи."""
        if type(grant) is not bool:
            raise ValueError("grant должен быть bool")
        try:
            self.connection.begin()
            with self.connection.cursor() as cursor:
                cursor.execute("SET @lock_manager_err = NULL")
                cursor.execute(
                    "CALL setLockAccess(%s, %s, %s, @lock_manager_err)",
                    (lock_id, card_id, int(grant)),
                )
                # CALL имеет дополнительные наборы результатов, включая пустой.
                while cursor.nextset():
                    pass
                cursor.execute("SELECT @lock_manager_err AS errCode")
                row = cursor.fetchone()
                if row is None or row["errCode"] is None:
                    raise ValueError("setLockAccess не вернула errCode. Изменения отменены.")
                code = int(row["errCode"])
                if code != 0 and code not in BUSINESS_ERRORS:
                    raise ValueError(
                        f"Неизвестный errCode={format_number(code)}. Изменения отменены."
                        " Проверь версию setLockAccess."
                    )
            if code == 0:
                self.connection.commit()
            else:
                self.connection.rollback()
            return code
        except BaseException as error:
            try:
                self.connection.rollback()
            except Exception:
                pass
            if isinstance(error, pymysql.MySQLError) and (
                not self.connection.open or (error.args and error.args[0] in (2006, 2013, 2055))
            ):
                raise WriteUncertain(
                    "Связь потеряна во время изменения. Результат не подтверждён. "
                    "После повторного запуска проверь список прав; "
                    "скрипт не повторяет изменение автоматически."
                ) from error
            raise


def show_page(lines, page):
    total_pages = max(1, (len(lines) + PAGE_SIZE - 1) // PAGE_SIZE)
    start = page * PAGE_SIZE
    index_width = max(3, len(format_number(len(lines))))
    for index, line in enumerate(lines[start:start + PAGE_SIZE], start + 1):
        print(f"{format_number(index):>{index_width}}. {line}")
    print(f"Страница {format_number(page + 1)}/{format_number(total_pages)}."
          f" Всего: {format_number(len(lines))}.")
    return total_pages


def choose_candidate(db, kind=None):
    title = {None: "аккаунтов и замков", "account": "аккаунтов", "lock": "замков"}[kind]
    allow_card = kind != "lock"
    while True:
        print(f"\nПоиск {title}: имя / описание / номер или его часть.")
        if allow_card:
            print("Можно просто приложить карту аккаунта (повторно — убрать и приложить).")
        text = read_input("Строка [Enter — все, :b — назад, :q — выход]: ", allow_card)
        if isinstance(text, ScannedCard):
            selected = db.account_by_card(text.number)
            if selected is not None:
                return selected
            print(f"Аккаунт с card_id {format_number(text.number)} не найден.")
            continue
        if text.lower() == ":b":
            return None
        found = db.search(text, kind)
        if not found:
            print("Совпадений нет. Попробуй другую строку.")
            continue
        page = 0
        lines = aligned_entity_lines(found)
        while True:
            pages = show_page(lines, page)
            command = read_input(
                "Номер кандидата; n/p — страницы; Enter — другой поиск; :b — назад: ", allow_card
            )
            if isinstance(command, ScannedCard):
                selected = db.account_by_card(command.number)
                if selected is not None:
                    return selected
                print(f"Аккаунт с card_id {format_number(command.number)} не найден.")
                continue
            command = compact_number(command.lower())
            if not command:
                break
            if command == ":b":
                return None
            if command == "n":
                page = min(page + 1, pages - 1)
            elif command == "p":
                page = max(page - 1, 0)
            elif command.isascii() and command.isdecimal() and 1 <= int(command) <= len(found):
                return found[int(command) - 1]
            else:
                print("Введи порядковый номер из списка (не ID) или команду.")


def change_access(db, selected, grant):
    other = choose_candidate(db, "lock" if selected.kind == "account" else "account")
    if other is None:
        return
    account = db.refresh(selected if selected.kind == "account" else other)
    lock = db.refresh(selected if selected.kind == "lock" else other)
    action = "НАЗНАЧИТЬ ДОСТУП" if grant else "ОТОЗВАТЬ ДОСТУП"
    print(f"\n{action}")
    for line in aligned_entity_lines([account, lock]):
        print(line)
    if read_input("Применить? [да / Enter — отмена]: ").lower() not in ("да", "д", "yes", "y"):
        print("Отменено.")
        return
    code = db.set_access(lock.number, account.card_id, grant)
    if code == 0:
        print("ГОТОВО: доступ назначен." if grant else "ГОТОВО: доступ отозван.")
    else:
        print(f"Код {format_number(code)}: {BUSINESS_ERRORS[code]}")


def validate_field(field, value):
    if field in ("name", "password"):
        if not isinstance(value, str) or len(value) > 255:
            raise ValueError("Не более 255 символов.")
        if field == "name" and not value.strip():
            raise ValueError("Имя не может быть пустым.")
        if any(not c.isprintable() for c in value):
            raise ValueError("Переносы строк и управляющие символы не допускаются.")
        return
    limits = {"money": (-2147483648, 2147483647), "losa": (-2147483648, 2147483647),
              "netrunner_lvl": (0, 255), "police": (0, 1)}
    if field not in limits:
        raise ValueError("Недопустимое поле аккаунта.")
    low, high = limits[field]
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"Допустимо целое число от {format_number(low)} до {format_number(high)}.")


def edit_account_field(db, selected, field):
    selected = db.refresh(selected)
    current = db.field_value(selected.card_id, field)
    print("\n" + entity_text(selected))
    print(f"Текущее значение «{EDIT_FIELDS[field]}»: {clean(current)}")
    if field == "password":
        print("Пароль — строка: ведущие нули сохраняются. :empty — задать пустой пароль.")
    if field == "police":
        print("0 — нет полицейских прав; 1 — есть полицейские права.")
    if field in ("money", "losa"):
        print("Вводится новый итоговый баланс, а не сумма прибавления или списания.")
    while True:
        # Пароль не strip(): пробелы и ведущие нули должны сохраниться буквально.
        raw = input("Новое значение [Enter / :b — назад, :q — выход]: ")
        command = raw.lower()
        if command == ":q":
            raise QuitRequested
        if command in ("", ":b"):
            return
        try:
            if field in ("name", "password"):
                value = "" if field == "password" and raw == ":empty" else raw
            else:
                number = "".join(raw.split())
                digits = number[1:] if number.startswith(("-", "+")) else number
                if not digits.isascii() or not digits.isdecimal():
                    raise ValueError("Введи целое число.")
                value = int(number)
            validate_field(field, value)
        except ValueError as error:
            print(clean(error))
            continue
        break
    if value == current:
        print("Значение не изменилось.")
        return
    print(f"Изменить «{EDIT_FIELDS[field]}»: {clean(current)} → {clean(value)}")
    if read_input("Применить? [да / Enter — отмена]: ").lower() not in ("да", "д", "yes", "y"):
        print("Отменено.")
        return
    db.set_field(selected.card_id, field, current, value)
    print("ГОТОВО: значение сохранено.")


def database_scope(db):
    name = db.connection.db
    if isinstance(name, bytes):
        name = name.decode("utf-8")
    return {"host": db.connection.host, "port": db.connection.port, "database": name}


def save_pending_id(data):
    temporary = PENDING_ID_PATH.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, PENDING_ID_PATH)


def confirm_sticker(db, pending):
    if pending.get("scope") != database_scope(db):
        raise WriteUncertain(
            "Осталась незавершённая смена ID в другом подключении. "
            "Подключись к тому же серверу и базе, чтобы подтвердить наклейку."
        )
    current = db.account_by_card(pending["card_id"])
    if current is None:
        raise WriteUncertain("Аккаунт незавершённой смены ID удалён. Требуется вручную проверить карту и pending_id_change.json.")
    number = current.number
    print("\nНЕОБХОДИМО ОБНОВИТЬ НАКЛЕЙКУ НА КАРТЕ")
    print(entity_text(current))
    if pending.get("new_id") is None:
        print("Прошлый вызов мог завершиться без полученного ответа. Новый ID повторно не генерируется.")
    elif pending["new_id"] != number:
        print("ID в БД уже отличается от сохранённого результата. На наклейке нужен текущий ID из БД.")
    print(f"Напиши на наклейке ID: {format_number(number)}")
    print(f"В памяти карты остаётся card_id {format_number(current.card_id)}; перезапись карты не требуется.")
    while True:
        try:
            raw = read_input("После изменения наклейки введи написанный ID (или :q — завершить): ")
        except KeyboardInterrupt:
            raise QuitRequested
        if compact_number(raw) == str(number):
            # Если ID успели изменить параллельно — подтверждать старый номер нельзя.
            refreshed = db.refresh(current)
            if refreshed.number != number:
                current = refreshed
                number = current.number
                print(f"ID в базе изменился. Обнови наклейку: {format_number(number)}")
                continue
            PENDING_ID_PATH.unlink()
            print("Подтверждено: наклейка обновлена.")
            return
        print("Для продолжения нужно изменить наклейку и ввести её новый ID. :q — завершить программу.")


def recover_pending_id(db):
    if not PENDING_ID_PATH.exists():
        return
    try:
        pending = json.loads(PENDING_ID_PATH.read_text(encoding="utf-8"))
        if not isinstance(pending, dict) or type(pending.get("card_id")) is not int or "scope" not in pending:
            raise ValueError("Неверный формат файла незавершённой смены ID.")
    except (OSError, ValueError) as error:
        raise WriteUncertain(f"Не удалось прочитать pending_id_change.json: {error}") from error
    confirm_sticker(db, pending)


def change_account_id(db, selected):
    selected = db.refresh(selected)
    print("\n" + entity_text(selected))
    print("ВНИМАНИЕ: после подтверждения будет сгенерирован новый ID.")
    print("Новый ID нужно обязательно написать на наклейке карты и подтвердить это в программе.")
    if read_input("Сгенерировать новый ID? [да / Enter / :b — назад]: ").lower() not in ("да", "д", "yes", "y"):
        return
    if PENDING_ID_PATH.exists():
        recover_pending_id(db)
        return
    pending = {"scope": database_scope(db), "card_id": selected.card_id,
               "old_id": selected.number, "new_id": None}
    # Сначала сохраняем намерение. Если папка недоступна для записи, CALL не выполняется.
    save_pending_id(pending)
    try:
        number = db.change_id(selected.card_id)
    except SelectionChanged:
        PENDING_ID_PATH.unlink()
        raise
    except (pymysql.MySQLError, WriteUncertain, ValueError, TypeError) as error:
        raise WriteUncertain(
            f"Смена ID не подтверждена: {clean(error)}. "
            "При следующем запуске программа проверит текущий ID и попросит обновить наклейку."
        ) from error
    pending["new_id"] = number
    print(f"НОВЫЙ ID: {format_number(number)}")
    save_pending_id(pending)
    confirm_sticker(db, pending)


def write_account_card(db, selected):
    selected = db.refresh(selected)
    print("\nЗАПИСАТЬ КАРТУ ДЛЯ АККАУНТА")
    print(entity_text(selected))
    print(f"В память будет записан card_id {format_number(selected.card_id)}, а не ID {format_number(selected.number)}.")
    print("Приложи одну карту. Прежний номер в байтах 8–15 блока 1 будет заменён.")
    print("Не убирай и не меняй карту до сообщения о результате.")
    if read_input("Карта приложена. Записать? [да / Enter / :b — отмена]: ").lower() not in ("да", "д", "yes", "y"):
        return
    selected = db.refresh(selected)
    try:
        if CONSOLE is None:
            raise RuntimeError("Считыватель не инициализирован.")
        number, warning = CONSOLE.write_number(selected.card_id, WRITE_BEEP_T)
    except (KeyboardInterrupt, EOFError):
        print("Запись прервана. Карта могла измениться; проверь её чтением перед использованием.")
        raise
    except Exception as error:
        print(f"Успех записи НЕ подтверждён: {clean(error)}")
        print("Карта могла измениться. Проверь её чтением. Автоматического повтора записи нет.")
        return
    print(f"ГОТОВО: card_id {format_number(number)} записан; весь блок и UID проверены обратным чтением.")
    if warning:
        print(f"Запись успешна, но писк не сработал: {clean(warning)}")


def inspect_entity(db, selected):
    page = 0
    while True:
        recover_pending_id(db)
        try:
            selected = db.refresh(selected)
        except SelectionChanged as error:
            print(clean(error))
            return
        print("\n" + entity_text(selected))
        print("Доступные замки:" if selected.kind == "account" else "Карты с правом доступа:")
        lines = db.access_lines(selected)
        page = min(page, max(0, (len(lines) - 1) // PAGE_SIZE))
        pages = show_page(lines, page) if lines else 1
        if not lines:
            print("Выданных прав нет.")
        print("\n1 — назначить доступ")
        print("2 — отозвать доступ")
        print("3 — обновить")
        if selected.kind == "account":
            print("4 — изменить имя")
            print("5 — изменить деньги")
            print("6 — изменить рейт")
            print("7 — изменить уровень деки")
            print("8 — изменить полицейские права")
            print("9 — посмотреть и изменить пароль")
            print("10 — изменить ID (наклейку)")
            print("11 — записать карту (card_id)")
        print("0 / Enter — новый поиск")
        print(":q — выход")
        if pages > 1:
            print("n — следующая страница")
            print("p — предыдущая страница")
        command = read_input("Действие: ").lower()
        if command in ("", "0", ":b"):
            return
        if command in ("1", "2"):
            try:
                change_access(db, selected, command == "1")
            except (SelectionChanged, ValueError) as error:
                print(clean(error))
            except pymysql.MySQLError as error:
                if not db.connection.open:
                    raise
                print_sql_error(error)
        elif selected.kind == "account" and command in ("4", "5", "6", "7", "8", "9", "10", "11"):
            try:
                fields = {"4": "name", "5": "money", "6": "losa", "7": "netrunner_lvl", "8": "police", "9": "password"}
                if command in fields:
                    edit_account_field(db, selected, fields[command])
                elif command == "10":
                    change_account_id(db, selected)
                else:
                    write_account_card(db, selected)
            except (SelectionChanged, ValueError) as error:
                print(clean(error))
            except pymysql.MySQLError as error:
                if not db.connection.open or (error.args and error.args[0] in (2006, 2013, 2055)):
                    raise
                print_sql_error(error)
        elif command == "n":
            page = min(page + 1, pages - 1)
        elif command == "p":
            page = max(0, page - 1)
        elif command != "3":
            print("Неизвестная команда.")


def prompt_default(label, default):
    return read_input(f"{label} [{default}]: ") or default


def connect_interactively():
    host, port, user = DEFAULT_HOST, DEFAULT_PORT, DEFAULT_USER
    while True:
        print("\nПодключение к MySQL. Enter — значение в скобках; :q — выход.")
        host = prompt_default("IP / имя сервера", host)
        while True:
            raw_port = compact_number(str(prompt_default("Порт", port)))
            if raw_port.isascii() and raw_port.isdecimal() and 1 <= int(raw_port) <= 65535:
                port = int(raw_port)
                break
            print("Порт должен быть числом от 1 до 65535.")
        user = prompt_default("Пользователь MySQL", user)
        password = getpass("Пароль (ввод скрыт): ")
        try:
            return pymysql.connect(
                host=host, port=port, user=user, password=password, database=DATABASE_NAME,
                charset="utf8mb4", cursorclass=DictCursor, autocommit=True,
                connect_timeout=10, read_timeout=30, write_timeout=30,
            )
        except pymysql.MySQLError as error:
            print_sql_error(error)
            if read_input("Enter — повторить подключение; :q — выход: "):
                print("Введи параметры подключения ещё раз.")


def print_sql_error(error):
    code = error.args[0] if error.args else "?"
    message = error.args[1] if len(error.args) > 1 else str(error)
    shown_code = format_number(code) if isinstance(code, int) else clean(code)
    print(f"Ошибка MySQL {shown_code}: {clean(message)}")
    if code in (1142, 1143, 1370):
        print("Нужны SELECT на main, locks, lock_access; UPDATE нужных полей main; EXECUTE на setLockAccess и changeId.")
    elif code == 1305:
        print(f"Проверь наличие setLockAccess и changeId в базе {DATABASE_NAME}.")


def main():
    global CONSOLE
    connection = None
    try:
        print("CYBERWALL — мастерская: аккаунты, карты и замки")
        connection = connect_interactively()
        db = Database(connection)
        db.check_read_access()
        recover_pending_id(db)
        CONSOLE = CardConsole(READER_ENABLED)
        print("Карта выбирает аккаунт только в поиске и списке кандидатов.")
        print("Для считывателя закрой nfcPro. USB можно подключить во время работы.")
        print("Подключено. Изменения выполняются только после подтверждения.")
        while True:
            selected = choose_candidate(db)
            if selected is not None:
                inspect_entity(db, selected)
    except (QuitRequested, EOFError, KeyboardInterrupt):
        print("\nРабота завершена.")
        return 0
    except OSError as error:
        print(f"Ошибка файла: {clean(error)}")
        print("Если смена ID уже началась, не удаляй pending_id_change.json: перезапусти программу для проверки наклейки.")
        return 1
    except WriteUncertain as error:
        print(clean(error))
        return 1
    except pymysql.MySQLError as error:
        print_sql_error(error)
        print("Работа остановлена. После восстановления связи запусти скрипт снова.")
        return 1
    finally:
        if CONSOLE is not None:
            CONSOLE.close()
            CONSOLE = None
        if connection is not None and connection.open:
            connection.close()


if __name__ == "__main__":
    sys.exit(main())
