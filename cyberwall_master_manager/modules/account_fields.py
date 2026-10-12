"""Правила ввода и требования к полям из фактической структуры таблицы."""
import re

NAME_RULES = "Имя: от 1 до 3 слов, длина каждого слова — не более 11 символов (включая дефис)."
LABELS = {"name": "имя", "money": "деньги", "losa": "рейт", "netrunner_lvl": "ур. деки",
          "police": "полицейские права", "password": "пароль", "occupancy": "принадлежность",
          "joinrpg_id": "ID заявки JoinRPG"}


def validate_name(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Имя не может быть пустым: нужно от 1 до 3 слов.")
    words = value.split()
    errors = []
    if len(words) > 3:
        errors.append(f"В имени {len(words)} слов; допускается не более 3.")
    for i, word in enumerate(words, 1):
        if len(word) > 11:
            errors.append(f"Слово №{i} «{word}» содержит {len(word)} символов; допускается не более 11.")
    if any(not c.isprintable() for c in value):
        errors.append("Переносы строк и управляющие символы не допускаются.")
    if errors:
        raise ValueError(" ".join(errors))


def column_limits(column):
    sql_type = column["Type"].lower()
    match = re.match(r"(tinyint|smallint|mediumint|int|bigint)(?:\(\d+\))?(?: unsigned)?$", sql_type)
    if match:
        bits = {"tinyint": 8, "smallint": 16, "mediumint": 24, "int": 32, "bigint": 64}[match[1]]
        if "unsigned" in sql_type:
            return "integer", 0, 2**bits - 1
        return "integer", -(2**(bits-1)), 2**(bits-1) - 1
    match = re.fullmatch(r"(?:var)?char\((\d+)\)", sql_type)
    if match:
        return "text", 0, int(match[1])
    if sql_type in ("tinytext", "text", "mediumtext", "longtext"):
        return "text_bytes", 0, {"tinytext": 255, "text": 65535, "mediumtext": 16777215, "longtext": 4294967295}[sql_type]
    raise ValueError(f"Поле {column['Field']}: тип {column['Type']} пока не поддерживается формой создания. Запись не создана.")


def editable_columns(columns):
    fields = []
    for column in columns:
        extra = column.get("Extra", "").lower()
        if column["Field"] in ("id", "card_id") or "auto_increment" in extra or "virtual generated" in extra or "stored generated" in extra:
            continue
        column_limits(column)  # Проверяем всю форму до ввода и INSERT.
        fields.append(column)
    if not fields or "name" not in {c["Field"] for c in fields}:
        raise ValueError("Структура main не соответствует ожидаемой: отсутствует поле name.")
    return fields


def column_hint(column, *, defaults=True):
    name = column["Field"]
    kind, low, high = column_limits(column)
    hints = [f"{LABELS.get(name, name)} ({name}):"]
    if name == "name":
        hints.append(NAME_RULES)
    if name == "police":
        hints.append("0 — нет прав, 1 — есть права.")
    elif kind == "integer":
        hints.append(f"Целое число от {low} до {high}.")
    elif kind == "text":
        hints.append(f"Строка, не более {high} символов.")
    else:
        hints.append(f"Строка, не более {high} байт UTF-8.")
    if name == "password":
        hints.append("Ведущие нули и пробелы сохраняются.")
    if name == "joinrpg_id":
        hints.append("Номер должен быть уникальным; без заявки оставь поле пустым, если разрешено NULL.")
    if column.get("Key") in ("UNI", "PRI") and name != "joinrpg_id":
        hints.append("Значение должно быть уникальным.")
    if name != "name":
        if defaults and column.get("Default") is not None and "default_generated" not in column.get("Extra", "").lower():
            hints.append(f"Enter — значение по умолчанию: {column['Default']}.")
        elif defaults and column.get("Null") == "YES":
            hints.append("Enter — пустое поле (NULL).")
        elif defaults:
            hints.append("Поле обязательно.")
        if column.get("Null") == "YES":
            hints.append(":null — NULL.")
        if kind.startswith("text"):
            hints.append(":empty — пустая строка.")
    return " ".join(hints)


def validate_column(column, value):
    name = column["Field"]
    kind, low, high = column_limits(column)
    if name == "name":
        validate_name(value)
    if value is None:
        if column.get("Null") != "YES":
            raise ValueError("Это поле не допускает NULL.")
        return
    if kind == "integer":
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"Допустимо целое число от {low} до {high}.")
    else:
        if not isinstance(value, str) or any(not c.isprintable() for c in value):
            raise ValueError("Нужна строка без переносов и управляющих символов.")
        length = len(value.encode("utf-8")) if kind == "text_bytes" else len(value)
        if length > high:
            raise ValueError(f"Введено {length}; допустимо не более {high} {'байт UTF-8' if kind == 'text_bytes' else 'символов'}.")
    if name == "police" and value not in (0, 1):
        raise ValueError("Полицейские права: только 0 или 1.")


def parse_column(column, raw):
    name = column["Field"]
    kind, _, _ = column_limits(column)
    if name != "name" and raw.lower() == ":null":
        value = None
    elif name != "name" and raw == "" and column.get("Default") is None and column.get("Null") == "YES":
        value = None
    else:
        if name != "name" and raw == "" and column.get("Default") is not None and "default_generated" not in column.get("Extra", "").lower():
            raw = str(column["Default"])
        elif name != "name" and raw == "" and kind.startswith("text"):
            raise ValueError("Введи значение; для намеренно пустой строки используй :empty.")
        if kind == "integer":
            number = "".join(raw.split())
            if not re.fullmatch(r"[+-]?[0-9]+", number):
                raise ValueError("Введи целое число.")
            value = int(number)
        else:
            value = "" if raw == ":empty" else raw
    validate_column(column, value)
    if name == "police" and value == 0 and column.get("Null") == "YES":
        return None
    return value
