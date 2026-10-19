#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CYBERWALL - менеджмент для мастеров: аккаунты, карты и доступ к замкам. Python 3.11+.
Запуск: py cyberwall_master_manager.py. См. README.md в архиве.
Настройки находятся в config.toml. Последнее успешное подключение запоминается.
Аккаунт всегда определяется по card_id; main.id меняется только через changeId.
"""

from dataclasses import dataclass
from contextlib import contextmanager
from getpass import getpass
import sys
import json
import os
import unicodedata
from pathlib import Path
from modules.settings import SETTINGS, APP_DIR, app_path
from modules.thermal_printer import ThermalPrinterService
from modules import printer_menu
from modules.card_console import CardConsole, ScannedCard, CardNotPresentError, READER_IMPORT_ERROR
from modules.account_fields import validate_name, NAME_RULES, LABELS, column_hint, parse_column, editable_columns, validate_column
from modules import batch_cards
from modules.connection_store import ConnectionStore
from typing import Optional

try:
    import pymysql
    from pymysql.cursors import DictCursor
except ImportError:
    print('Не установлен PyMySQL. Выполни: py -m pip install "PyMySQL[rsa]"')
    raise SystemExit(1)


DEFAULT_HOST = SETTINGS["database"]["host"]
DEFAULT_PORT = SETTINGS["database"]["port"]
DEFAULT_USER = SETTINGS["database"]["user"]
DATABASE_NAME = SETTINGS["database"]["name"]
PAGE_SIZE = SETTINGS["interface"]["page_size"]
READER_ENABLED = SETTINGS["reader"]["enabled"]
WRITE_BEEP_T = SETTINGS["reader"]["write_beep_t"]
PENDING_ID_PATH = app_path(SETTINGS["state"]["pending_id_file"])
VERSION = "2.6.0"
BATCH_PATH = app_path(SETTINGS["state"]["batch_cards_file"])
CONSOLE = None
THERMAL_PRINTER = None
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


class DisconnectRequested(Exception):
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
        return "-"
    return "".join(c if c.isprintable() else " " for c in str(value))


def format_number(value):
    """Только отображение: SQL получает исходные целые числа."""
    if value is None:
        return "-"
    return f"{value:,}".replace(",", " ")


def compact_number(value):
    """Принимает обычные и неразрывные пробелы в скопированных номерах."""
    compact = "".join(value.split())
    if compact.isascii() and compact.isdecimal():
        return compact
    return value


TABLE_HEADERS = ("Тип", "ID", "lock_id", "card_id", "Имя / описание", "деньги", "рейт", "ур. деки", "полиц. права")


@dataclass
class EntityTable:
    header: str
    separator: str
    lines: list

    def __len__(self):
        return len(self.lines)


def entity_values(entity):
    return [
        {"account": "АККАУНТ", "lock": "ЗАМОК", "orphan": "НЕТ В MAIN"}[entity.kind],
        format_number(entity.number if entity.kind != "lock" else None),
        format_number(entity.number if entity.kind == "lock" else None),
        format_number(entity.card_id), clean(entity.label),
        *[format_number(getattr(entity, field)) for field in ("money", "losa", "netrunner_lvl")],
        format_number(entity.police or 0) if entity.kind == "account" else "-",
    ]


def make_entity_table(entities):
    rows = [entity_values(entity) for entity in entities]
    widths = [max(len(title), *(len(row[i]) for row in rows)) if rows else len(title)
              for i, title in enumerate(TABLE_HEADERS)]
    def line(cells):
        return " | ".join(cell.ljust(width) for cell, width in zip(cells, widths))
    return EntityTable(line(TABLE_HEADERS), "-+-".join("-" * width for width in widths),
                       [line(row) for row in rows])


def entity_text(entity):
    table = make_entity_table([entity])
    return "\n".join([table.header, table.separator, *table.lines])


def raw_input(prompt, *, history=None, allow_card=False):
    return CONSOLE.read(prompt, allow_card=allow_card, history=history) if CONSOLE else input(prompt)


def read_input(prompt, allow_card=False, *, history=None):
    value = raw_input(prompt, history=history, allow_card=allow_card)
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

    def batch_accounts(self):
        return self.rows("SELECT card_id, id, name FROM main ORDER BY card_id")

    def batch_account(self, card_id):
        rows = self.rows("SELECT card_id, id, name FROM main WHERE card_id = %s", (card_id,))
        return rows[0] if rows else None

    def batch_by_id(self, number):
        rows = self.rows("SELECT card_id, id, name FROM main WHERE id = %s", (number,))
        return rows[0] if rows else None

    @contextmanager
    def batch_guard(self, expected):
        self.connection.begin()
        try:
            rows = self.rows("SELECT card_id, id, name FROM main WHERE card_id = %s FOR UPDATE",
                             (expected["card_id"],))
            if not rows or rows[0] != expected:
                raise batch_cards.BatchError("Аккаунт удалён или его данные изменились. Заново проверь наклейку.")
            yield
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def account_schema(self):
        return editable_columns(self.rows("SHOW COLUMNS FROM main"))

    def create_account(self, values):
        columns = self.account_schema()
        names = [column["Field"] for column in columns]
        if set(values) != set(names):
            raise ValueError("Состав полей main изменился. Начни создание аккаунта заново.")
        for column in columns:
            validate_column(column, values[column["Field"]])
        quoted = ", ".join("`" + name.replace("`", "``") + "`" for name in names)
        placeholders = ", ".join(["%s"] * len(names))
        try:
            self.connection.begin()
            with self.connection.cursor() as cursor:
                cursor.execute(f"INSERT INTO main ({quoted}) VALUES ({placeholders})", tuple(values[name] for name in names))
                card_id = cursor.lastrowid
                cursor.execute(f"SELECT {ACCOUNT_COLUMNS} FROM main WHERE card_id = %s", (card_id,))
                row = cursor.fetchone()
                if row is None or row["id"] is None:
                    raise ValueError("Не получены card_id/ID новой записи. Проверь AUTO_INCREMENT и триггер main; создание отменено.")
                created = account_from_row(row)
                if not 100000000 <= created.number <= 999999999:
                    raise ValueError("Триггер не сформировал девятизначный ID. Создание отменено.")
            self.connection.commit()
            return created
        except BaseException as error:
            self._rollback_write(error)
            raise

    def lock_description_schema(self):
        columns = self.rows("SHOW COLUMNS FROM locks WHERE Field = 'description'")
        if not columns:
            raise ValueError("В locks отсутствует description.")
        return columns[0]

    def set_lock_description(self, selected, value):
        validate_column(self.lock_description_schema(), value)
        try:
            self.connection.begin()
            with self.connection.cursor() as cursor:
                cursor.execute("SELECT description FROM locks WHERE lock_id = %s FOR UPDATE", (selected.number,))
                row = cursor.fetchone()
                if row is None or row["description"] != selected.label:
                    raise SelectionChanged("Замок удалён или описание уже изменено. Обнови данные о записи.")
                cursor.execute("UPDATE locks SET description = %s WHERE lock_id = %s", (value, selected.number))
            self.connection.commit()
        except BaseException as error:
            self._rollback_write(error)
            raise

    def delete_entity(self, selected):
        if selected.kind not in ("account", "lock"):
            raise ValueError("Можно удалить только аккаунт или замок.")
        try:
            self.connection.begin()
            with self.connection.cursor() as cursor:
                if selected.kind == "account":
                    cursor.execute("SELECT id FROM main WHERE card_id = %s FOR UPDATE", (selected.card_id,))
                    row = cursor.fetchone()
                    if row is None or row["id"] != selected.number:
                        raise SelectionChanged("Аккаунт удалён или ID изменён. Удаление отменено; обнови данные о записи.")
                    cursor.execute("DELETE FROM lock_access WHERE card_id = %s", (selected.card_id,))
                    cursor.execute("DELETE FROM main WHERE card_id = %s AND id = %s", (selected.card_id, selected.number))
                else:
                    cursor.execute("SELECT description FROM locks WHERE lock_id = %s FOR UPDATE", (selected.number,))
                    row = cursor.fetchone()
                    if row is None or row["description"] != selected.label:
                        raise SelectionChanged("Замок удалён или описание изменено. Удаление отменено; обнови данные о записи.")
                    cursor.execute("DELETE FROM lock_access WHERE lock_id = %s", (selected.number,))
                    cursor.execute("DELETE FROM locks WHERE lock_id = %s", (selected.number,))
                if cursor.rowcount != 1:
                    raise SelectionChanged("Запись не удалена. Изменения отменены.")
            self.connection.commit()
        except BaseException as error:
            self._rollback_write(error)
            raise

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
        if field == "police" and value == 0:
            value = None
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
                "Перезапусти программу и проверь запись. Автоматического повтора нет."
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

    def access_table(self, entity):
        if entity.kind == "account":
            rows = self.rows("""
                SELECT l.lock_id, l.description
                FROM lock_access AS a
                JOIN locks AS l ON l.lock_id = a.lock_id
                WHERE a.card_id = %s
                ORDER BY l.description, l.lock_id
            """, (entity.card_id,))
            return make_entity_table([lock_from_row(row) for row in rows])
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
        return make_entity_table(entities)

    def set_access(self, lock_id, card_id, grant):
        """Одна транзакция, без автоматического переподключения и повторной записи."""
        if type(grant) is not bool:
            raise ValueError("grant должен быть bool")
        try:
            self.connection.begin()
            with self.connection.cursor() as cursor:
                cursor.execute("SET @cyberwall_access_err = NULL")
                cursor.execute(
                    "CALL setLockAccess(%s, %s, %s, @cyberwall_access_err)",
                    (lock_id, card_id, int(grant)),
                )
                # CALL имеет дополнительные наборы результатов, включая пустой.
                while cursor.nextset():
                    pass
                cursor.execute("SELECT @cyberwall_access_err AS errCode")
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


def show_page(table, page):
    total_pages = max(1, (len(table) + PAGE_SIZE - 1) // PAGE_SIZE)
    start = page * PAGE_SIZE
    index_width = max(3, len(format_number(len(table))))
    print(f"{'№':>{index_width}} | {table.header}")
    print("-" * index_width + "-+-" + table.separator)
    for index, line in enumerate(table.lines[start:start + PAGE_SIZE], start + 1):
        print(f"{format_number(index):>{index_width}} | {line}")
    print(f"Страница {format_number(page + 1)}/{format_number(total_pages)}."
          f" Всего: {format_number(len(table))}.")
    return total_pages


def general_menu(db):
    """One named entry point; all common actions use numeric choices."""
    while True:
        print("\nОБЩЕЕ МЕНЮ")
        print("1 — создать аккаунт")
        print("2 — пакетная запись карт (новая работа или продолжение)")
        print("3 — печать шаблонов ID")
        print("4 — термопринтер")
        print("5 — отключиться от БД")
        print("6 — выход из программы")
        print("0, Enter, :b — назад")
        choice = read_input("Пункт меню: ", history="menu:general").lower()
        if choice in ("", "0", ":b"):
            return None
        if choice == "5":
            raise DisconnectRequested
        if choice == "6":
            raise QuitRequested
        try:
            if choice == "1":
                created = create_account(db)
                if created is not None:
                    return created
            elif choice == "2":
                if CONSOLE is None:
                    raise RuntimeError("Считыватель не инициализирован.")
                batch_cards.run(db, CONSOLE, BATCH_PATH, database_scope(db),
                                beep_t=WRITE_BEEP_T, ask=CONSOLE.read)
            elif choice == "3":
                printer_menu.templates(THERMAL_PRINTER, read_input)
            elif choice == "4":
                printer_menu.printer_menu(THERMAL_PRINTER, read_input)
            else:
                print("Введи номер пункта меню.")
        except (SelectionChanged, ValueError, batch_cards.BatchError, RuntimeError) as error:
            print(clean(error))
        except pymysql.MySQLError as error:
            if not db.connection.open or (error.args and error.args[0] in (2006, 2013, 2055)):
                raise
            print_sql_error(error)


def choose_candidate(db, kind=None):
    title = {None: "аккаунтов и замков", "account": "аккаунтов", "lock": "замков"}[kind]
    allow_card = kind != "lock"
    while True:
        print(f"\nПоиск {title}: имя, описание, номер или его часть.")
        if allow_card:
            print("Можно просто приложить карту аккаунта (повторно — убрать и приложить).")
        if kind is None:
            print(":menu — общее меню")
            printer_menu.show_status(THERMAL_PRINTER)
        text = read_input("Строка [Enter — все; :b — назад; :q — выход]: ", allow_card,
                          history=f"search:{kind or 'all'}")
        if kind is None and isinstance(text, str) and text.lower() == ":menu":
            created = general_menu(db)
            if created is not None:
                return created
            continue
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
        lines = make_entity_table(found)
        while True:
            pages = show_page(lines, page)
            command = read_input(
                "Номер кандидата; n — следующая страница; p — предыдущая страница; Enter — другой поиск; :b — назад: ", allow_card,
                history=f"candidates:{kind or 'all'}"
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
    show_page(make_entity_table([account, lock]), 0)
    if read_input("Применить? [да — подтвердить; Enter — отмена]: ").lower() not in ("да", "д", "yes", "y"):
        print("Отменено.")
        return
    code = db.set_access(lock.number, account.card_id, grant)
    if code == 0:
        print("ГОТОВО: доступ назначен." if grant else "ГОТОВО: доступ отозван.")
    else:
        print(f"Код {format_number(code)}: {BUSINESS_ERRORS[code]}")


def validate_field(field, value):
    if field == "police" and value is None:
        return
    if field in ("name", "password"):
        if not isinstance(value, str) or len(value) > 255:
            raise ValueError("Не более 255 символов.")
        if field == "name":
            validate_name(value)
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


def display_field(field, value):
    return clean(value or 0) if field == "police" else clean(value)


def create_account(db):
    print("\nСОЗДАТЬ НОВЫЙ АККАУНТ")
    print("card_id и ID создаёт БД автоматически. :b — отмена; :q — выход.")
    columns = db.account_schema()
    values = {}
    for column in columns:
        if column["Field"] == "joinrpg_id":
            values["joinrpg_id"] = None
            continue
        print("\n" + column_hint(column))
        while True:
            # Не удаляем пробелы из пароля и не преобразуем его в число.
            raw = raw_input("Значение: ", history=None if column["Field"] == "password" else "field:" + column["Field"])
            if raw.lower() == ":q":
                raise QuitRequested
            if raw.lower() == ":b":
                print("Создание отменено. Запись не добавлена.")
                return None
            try:
                values[column["Field"]] = parse_column(column, raw)
                break
            except ValueError as error:
                print(f"ОШИБКА: {clean(error)}")
    print("\nПРОВЕРЬ ДАННЫЕ НОВОГО АККАУНТА")
    for field, value in values.items():
        print(f"{LABELS.get(field, field)}: {display_field(field, value)}")
    if read_input("Создать аккаунт? [да — подтвердить; Enter — отмена]: ").lower() not in ("да", "д", "yes", "y"):
        print("Создание отменено. Запись не добавлена.")
        return None
    created = db.create_account(values)
    print("\nАККАУНТ СОЗДАН")
    print(entity_text(created))
    print(f"Напиши на наклейке карты аккаунта ID: {format_number(created.number)}.")
    print("Для записи card_id в память карты выбери пункт 11 в меню аккаунта.")
    return created


def edit_lock_description(db, selected):
    selected = db.refresh(selected)
    column = db.lock_description_schema()
    print("\n" + entity_text(selected))
    print(column_hint(column, defaults=False))
    print("Ограничения имени аккаунта по словам и длине слов здесь не применяются.")
    while True:
        raw = read_input("Новое описание [Enter, :b — назад; :empty — пустая строка]: ", history="lock:description")
        if raw.lower() in ("", ":b"):
            return
        try:
            value = parse_column(column, raw)
            break
        except ValueError as error:
            print(f"ОШИБКА: {clean(error)}")
    if value == selected.label:
        print("Значение не изменилось.")
        return
    print(f"Описание: {clean(selected.label)} → {clean(value)}")
    if read_input("Применить? [да — подтвердить; Enter — отмена]: ").lower() not in ("да", "д", "yes", "y"):
        return
    db.set_lock_description(selected, value)
    print("ГОТОВО: описание замка сохранено.")


def delete_entity(db, selected):
    selected = db.refresh(selected)
    is_account = selected.kind == "account"
    noun = "аккаунт" if is_account else "замок"
    key = "ID" if is_account else "lock_id"
    print(f"\nУДАЛИТЬ {noun.upper()}")
    print(entity_text(selected))
    print("Запись и её права доступа к замкам будут удалены из БД.")
    if is_account:
        print("Физическая карта не стирается. Программа не удаляет историю logs и реестр выданных ID.")
    while True:
        raw = read_input(f"Для удаления введи {key} {format_number(selected.number)} [Enter, :b — отмена]: ")
        if raw.lower() in ("", ":b"):
            print("Удаление отменено.")
            return False
        if compact_number(raw) != str(selected.number):
            print(f"ОШИБКА: введён неправильный {key}. Запись не удалена.")
            continue
        db.delete_entity(selected)
        print(f"ГОТОВО: {noun} удалён.")
        return True


def edit_account_field(db, selected, field):
    selected = db.refresh(selected)
    current = db.field_value(selected.card_id, field)
    print("\n" + entity_text(selected))
    print(f"Текущее значение «{EDIT_FIELDS[field]}»: {display_field(field, current)}")
    if field == "name":
        print(NAME_RULES)
    if field == "password":
        print("Пароль — строка: ведущие нули сохраняются. :empty — задать пустой пароль.")
    if field == "police":
        print("1 — назначить полицейские права; 0 — снять права.")
    if field in ("money", "losa"):
        print("Вводится новый итоговый баланс, а не сумма прибавления или списания.")
    while True:
        # Пароль не strip(): пробелы и ведущие нули должны сохраниться буквально.
        raw = raw_input("Новое значение [Enter, :b — назад; :q — выход]: ",
                        history=None if field == "password" else "field:" + field)
        command = raw.lower()
        if command == ":q":
            raise QuitRequested
        if command in ("", ":b"):
            return
        try:
            if field == "police" and raw.strip().lower() in ("-", "0", "null"):
                value = None
            elif field in ("name", "password"):
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
    if value == current or (field == "police" and (value or 0) == (current or 0)):
        print("Значение не изменилось.")
        return
    print(f"Изменить «{EDIT_FIELDS[field]}»: {display_field(field, current)} → {display_field(field, value)}")
    if read_input("Применить? [да — подтвердить; Enter — отмена]: ").lower() not in ("да", "д", "yes", "y"):
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
    if pending.get("new_id") is not None or number != pending.get("old_id"):
        print("\nID ИЗМЕНЕН")
    else:
        print("\nПРОВЕРКА ID ПОСЛЕ ПРЕРВАННОЙ ОПЕРАЦИИ")
    print("НЕОБХОДИМО ОБНОВИТЬ НАКЛЕЙКУ НА КАРТЕ")
    print(entity_text(current))
    if pending.get("new_id") is None:
        print("Прошлый вызов мог завершиться без полученного ответа. Новый ID повторно не генерируется.")
    elif pending["new_id"] != number:
        print("ID в БД уже отличается от сохранённого результата. На наклейке нужен текущий ID из БД.")
    print(f"Напиши на наклейке ID: {format_number(number)}")
    while True:
        try:
            raw = read_input("После изменения наклейки введи написанный ID (:disconnect — отключиться; :q — выход): ")
            if raw.lower() == ":disconnect":
                raise DisconnectRequested
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
        print(f"ОШИБКА: введён неправильный ID. Нужно ввести {format_number(number)}. Попробуй ещё раз или :q — выход.")


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
    if read_input("Сгенерировать новый ID? [да — подтвердить; Enter, :b — назад]: ").lower() not in ("да", "д", "yes", "y"):
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
    save_pending_id(pending)
    confirm_sticker(db, pending)


def write_account_card(db, selected):
    selected = db.refresh(selected)
    print("\nЗАПИСАТЬ КАРТУ ДЛЯ АККАУНТА")
    print(entity_text(selected))
    print(f"В память будет записан card_id {format_number(selected.card_id)}.")
    print("Прежний номер будет заменен.")
    print("Приложи одну карту к считывателю.")
    print("Не убирай и не меняй карту до сообщения о результате.")
    if read_input("Записать? [да — подтвердить; Enter, :b — отмена]: ").lower() not in ("да", "д", "yes", "y"):
        return
    selected = db.refresh(selected)
    try:
        if CONSOLE is None:
            raise RuntimeError("Считыватель не инициализирован.")
        number, warning = CONSOLE.write_number(selected.card_id, WRITE_BEEP_T)
    except CardNotPresentError:
        print("Карта не обнаружена. Запись не выполнялась. Приложи карту и снова выбери пункт 11.")
        return
    except (KeyboardInterrupt, EOFError):
        print("Запись прервана. Карта могла измениться; проверь её чтением перед использованием.")
        raise
    except Exception as error:
        print(f"Успех записи НЕ подтверждён: {clean(error)}")
        print("Карта могла измениться. Проверь её чтением. Автоматического повтора записи нет.")
        return
    print(f"ГОТОВО: card_id {format_number(number)} записан. Запись успешна и проверена обратным чтением.")
    if warning:
        print(f"Запись успешна, но писк не сработал: {clean(warning)}")
    print(f"Убедись, что на наклейке карты написан правильный ID: {format_number(selected.number)}.")


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
        print("\nДоступные замки:" if selected.kind == "account" else "Аккаунты с правом доступа:")
        lines = db.access_table(selected)
        page = min(page, max(0, (len(lines) - 1) // PAGE_SIZE))
        pages = show_page(lines, page) if lines else 1
        if not lines:
            print("Выданных прав нет.")
        print("\n1 — назначить доступ к замкам")
        print("2 — отозвать доступ к замкам")
        print("3 — обновить данные о записи")
        if selected.kind == "account":
            print("4 — изменить имя")
            print("5 — изменить деньги")
            print("6 — изменить рейт")
            print("7 — изменить уровень деки")
            print("8 — изменить полицейские права")
            print("9 — посмотреть и изменить пароль")
            print("10 — изменить ID")
            print("11 — записать карту (card_id)")
            print("12 — удалить аккаунт")
            print("13 — печать наклейки ID")
        else:
            print("4 — изменить описание замка")
            print("5 — удалить замок")
        print("0, Enter, :b — новый поиск")
        print(":menu — общее меню")
        print(":q — выход")
        if pages > 1:
            print("n — следующая страница")
            print("p — предыдущая страница")
        command = read_input("Действие: ", history="menu:" + selected.kind).lower()
        if command == ":menu":
            created = general_menu(db)
            if created is not None:
                selected, page = created, 0
            continue
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
        elif (selected.kind == "lock" and command in ("4", "5")) or (selected.kind == "account" and command == "12"):
            try:
                if selected.kind == "lock" and command == "4":
                    edit_lock_description(db, selected)
                elif delete_entity(db, selected):
                    return
            except (SelectionChanged, ValueError) as error:
                print(clean(error))
            except pymysql.MySQLError as error:
                if not db.connection.open or (error.args and error.args[0] in (2006, 2013, 2055)):
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
        elif selected.kind == "account" and command == "13":
            # Refresh once more at the actual print action; account number may have changed.
            try:
                mode = printer_menu.choose_label_mode(read_input, THERMAL_PRINTER)
                if mode is not None:
                    current = db.refresh(selected)
                    printer_menu.print_label(THERMAL_PRINTER, str(current.number).zfill(9),
                        current.label if mode == "id-name" else None, mode=mode, ask=read_input)
            except SelectionChanged as error:
                print(clean(error))
        elif command == "n":
            page = min(page + 1, pages - 1)
        elif command == "p":
            page = max(0, page - 1)
        elif command != "3":
            print("Неизвестная команда.")


def prompt_default(label, default):
    return read_input(f"{label} [{default}]: ", history="connect:" + label) or default


def prompt_database_password():
    while True:
        password = getpass("Пароль (ввод скрыт, :q — выход): ")
        if password.lower() == ":q":
            raise QuitRequested
        if any("CYRILLIC" in unicodedata.name(char, "") for char in password):
            print("Пароль введён кириллицей. Переключи раскладку клавиатуры и повтори ввод пароля.")
            continue
        return password


def open_database(profile):
    connection = pymysql.connect(
        **profile, charset="utf8mb4", cursorclass=DictCursor, autocommit=True,
        connect_timeout=SETTINGS["database"]["connect_timeout"],
        read_timeout=SETTINGS["database"]["read_timeout"],
        write_timeout=SETTINGS["database"]["write_timeout"],
    )
    try:
        Database(connection).check_read_access()
    except BaseException:
        connection.close()
        raise
    return connection


def connect_interactively(*, automatic=True, defaults=None, store=None):
    store = store if store is not None else ConnectionStore()
    profile = {"host": DEFAULT_HOST, "port": DEFAULT_PORT, "user": DEFAULT_USER,
               "database": DATABASE_NAME}
    if defaults:
        profile.update(defaults)
    saved = None
    if automatic:
        try:
            saved = store.load()
        except (OSError, ValueError):
            print("Не удалось прочитать сохранённое подключение. Введи параметры заново.")
        if saved:
            profile.update(saved)
            print(f"Автоматическое подключение: {clean(profile['user'])}@{clean(profile['host'])}:"
                  f"{profile['port']} / {clean(profile['database'])}")
            try:
                return open_database(profile)
            except (pymysql.MySQLError, UnicodeError, OSError):
                # Не печатаем исключение с потенциальными секретами из авторизации.
                print("Автоматическое подключение не удалось. Проверь параметры и введи пароль заново.")
    while True:
        print("\nПодключение к MySQL. Enter — значение в скобках; :q — выход.")
        profile["host"] = prompt_default("IP или имя сервера", profile["host"])
        while True:
            raw_port = compact_number(str(prompt_default("Порт", profile["port"])))
            if raw_port.isascii() and raw_port.isdecimal() and 1 <= int(raw_port) <= 65535:
                profile["port"] = int(raw_port)
                break
            print("Порт должен быть числом от 1 до 65535.")
        profile["user"] = prompt_default("Пользователь MySQL", profile["user"])
        profile["database"] = prompt_default("База данных", profile["database"])
        profile["password"] = prompt_database_password()
        try:
            connection = open_database(profile)
        except UnicodeError:
            print("Не удалось обработать символы пароля. Проверь раскладку и повтори подключение.")
            continue
        except (pymysql.MySQLError, OSError) as error:
            print_sql_error(error)
            if read_input("Enter — повторить подключение; :q — выход: "):
                print("Введи параметры подключения ещё раз.")
            continue
        try:
            store.save(profile)
        except (OSError, ValueError):
            print("Подключено, но сохранить параметры не удалось. Следующий запуск может использовать прежний вход или запросить параметры.")
        else:
            print("Подключение сохранено для автоматического входа при следующем запуске.")
            if os.name != "nt":
                print("На этой ОС пароль хранится в личном файле с правами 0600, без шифрования.")
        return connection


def print_sql_error(error):
    code = error.args[0] if error.args else "?"
    message = error.args[1] if len(error.args) > 1 else str(error)
    shown_code = format_number(code) if isinstance(code, int) else clean(code)
    print(f"Ошибка MySQL {shown_code}: {clean(message)}")
    if code in (1142, 1143, 1370):
        print("Нужны SELECT на main, locks, lock_access; INSERT/DELETE на main; UPDATE нужных полей main и locks.description; DELETE на locks и lock_access; EXECUTE на setLockAccess и changeId.")
    elif code == 1305:
        print("Проверь наличие setLockAccess и changeId в подключённой базе.")


def main():
    global CONSOLE, THERMAL_PRINTER
    connection = None
    try:
        print(f"CYBERWALL - менеджмент для мастеров (v{VERSION})")
        CONSOLE = CardConsole(READER_ENABLED)
        THERMAL_PRINTER = ThermalPrinterService(SETTINGS['printer'], APP_DIR / 'config.toml')
        THERMAL_PRINTER.start()
        store = ConnectionStore()
        automatic, defaults = True, None
        while True:
            connection = connect_interactively(automatic=automatic, defaults=defaults, store=store)
            db = Database(connection)
            scope = database_scope(db)
            user = connection.user
            if isinstance(user, bytes):
                user = user.decode(connection.encoding, errors="replace")
            print(f"Подключено: {clean(user)}@{clean(scope['host'])}:{scope['port']} / {clean(scope['database'])}.")
            print("Изменения выполняются только после подтверждения.")
            print("Карта выбирает аккаунт только в поиске и списке кандидатов.")
            print("USB считыватель можно подключить во время работы, все другие программы работающие со считывателем должны быть закрыты.")
            if READER_ENABLED and READER_IMPORT_ERROR:
                print(f"Модуль считывателя недоступен: {clean(READER_IMPORT_ERROR)}. Ручной поиск работает.")
            try:
                try:
                    recover_pending_id(db)
                except WriteUncertain as error:
                    print(clean(error))
                    while True:
                        command = read_input(":disconnect — Отключиться от БД; :q — выход: ")
                        if command.lower() == ":disconnect":
                            raise DisconnectRequested
                while True:
                    selected = choose_candidate(db)
                    if selected is not None:
                        inspect_entity(db, selected)
            except DisconnectRequested:
                defaults = {**scope, "user": user}
                connection.close()
                connection = None
                CONSOLE.histories.clear()
                try:
                    store.clear()
                except OSError:
                    print("Не удалось удалить сохранённое подключение: при следующем запуске возможен вход с прежними параметрами.")
                print("Отключено от БД. Введи параметры нового подключения.")
                automatic = False
    except (QuitRequested, EOFError, KeyboardInterrupt):
        print("\nРабота завершена.")
        return 0
    except OSError as error:
        print(f"Ошибка файла: {clean(error)}")
        print("Если смена ID уже началась, не удаляй pending_id_change.json: перезапусти программу для проверки наклейки.")
        print("При пакетной записи сохрани batch_cards.json и выбери «продолжить работу»: карта могла измениться.")
        return 1
    except WriteUncertain as error:
        print(clean(error))
        return 1
    except pymysql.MySQLError as error:
        print_sql_error(error)
        print("Работа остановлена. После восстановления связи запусти скрипт снова.")
        return 1
    finally:
        if THERMAL_PRINTER is not None:
            THERMAL_PRINTER.close()
            THERMAL_PRINTER = None
        if CONSOLE is not None:
            CONSOLE.close()
            CONSOLE = None
        if connection is not None and connection.open:
            connection.close()


if __name__ == "__main__":
    sys.exit(main())
