"""Настройки термопринтера и сохранение только выбранного устройства."""
from copy import deepcopy
from math import isfinite
from pathlib import Path
import os
import tempfile

DEFAULTS = {
    'enabled': True, 'address': '', 'name': '',
    'auto_connect': True, 'auto_reconnect': True,
    'reconnect_delay_seconds': 5.0, 'scan_timeout_seconds': 15.0,
    'connection_check_seconds': 2.0, 'blackening': 3, 'max_copies': 100,
    'library_path': '../../CyberpunkIdPrinter', 'timini_path': '../../TiMini-Print',
    'paper': {'type': 'continuous', 'roll_width_mm': 57.0, 'width_mm': 40.0,
              'height_mm': 30.0, 'offset_x_mm': 0.0, 'dpi': 203.2},
    'labels': {'feed_mm': 15.0, 'retract_mm': 25.0, 'gap_mm': 2.0, 'position_timeout': 20.0,
               'settle_seconds': 3.0, 'motion_settle_seconds': 5.0},
    'layout': {'inactive_segments': True, 'decorations': True},
}


def printer_settings(raw):
    if not isinstance(raw, dict):
        raise ValueError('[printer] должен быть таблицей TOML.')
    result = deepcopy(DEFAULTS)
    for key, value in raw.items():
        if key in ('paper', 'layout', 'labels'):
            if not isinstance(value, dict):
                raise ValueError(f'[printer.{key}] должен быть таблицей TOML.')
            result[key].update(value)
        else:
            result[key] = value
    for key in ('enabled', 'auto_connect', 'auto_reconnect'):
        if type(result[key]) is not bool:
            raise ValueError(f'[printer] {key}: true или false.')
    for key in ('address', 'name', 'library_path', 'timini_path'):
        if not isinstance(result[key], str):
            raise ValueError(f'[printer] {key}: строка.')
    for key in ('reconnect_delay_seconds', 'scan_timeout_seconds', 'connection_check_seconds'):
        value = result[key]
        if type(value) not in (int, float) or not isfinite(value) or not 0.1 <= value <= 300:
            raise ValueError(f'[printer] {key}: число 0.1…300 секунд.')
    for key, high in (('blackening', 5), ('max_copies', 100)):
        if type(result[key]) is not int or not 1 <= result[key] <= high:
            raise ValueError(f'[printer] {key}: целое число 1…{high}.')
    for key in ('inactive_segments', 'decorations'):
        if type(result['layout'][key]) is not bool:
            raise ValueError(f'[printer.layout] {key}: true или false.')
    labels = result['labels']
    for key in DEFAULTS['labels']:
        value = labels[key]
        if type(value) not in (int, float) or not isfinite(value) or value <= 0:
            raise ValueError(f'[printer.labels] {key}: положительное конечное число.')
        if key in ('feed_mm', 'retract_mm', 'gap_mm') and not 0.25 <= value <= 25:
            raise ValueError(f'[printer.labels] {key}: число 0.25…25 мм.')
        if key in ('settle_seconds', 'motion_settle_seconds') and value < 2:
            raise ValueError(f'[printer.labels] {key}: не менее 2 секунд.')
    unknown = labels.keys() - DEFAULTS['labels'].keys()
    if unknown:
        raise ValueError(f'[printer.labels] Неизвестные настройки: {", ".join(sorted(unknown))}.')
    paper = result['paper']
    if paper['type'] not in ('continuous', 'labels'):
        raise ValueError('[printer.paper] type: continuous или labels.')
    for key in ('roll_width_mm', 'width_mm', 'height_mm', 'offset_x_mm', 'dpi'):
        v = paper[key]
        if type(v) not in (int, float) or not isfinite(v) or (key != 'offset_x_mm' and v <= 0):
            raise ValueError(f'[printer.paper] Некорректное значение {key}.')
    if paper['width_mm'] > paper['roll_width_mm']:
        raise ValueError('Макет шире ленты термопринтера.')
    return result


def save_printer(path, address, name):
    """Read the latest file, preserve comments and replace atomically."""
    import tomlkit
    path = Path(path)
    document = tomlkit.parse(path.read_text(encoding='utf-8'))
    if 'printer' not in document:
        document['printer'] = tomlkit.table()
    document['printer']['address'] = address
    document['printer']['name'] = name
    fd, temporary = tempfile.mkstemp(prefix='.printer-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as stream:
            stream.write(tomlkit.dumps(document))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
