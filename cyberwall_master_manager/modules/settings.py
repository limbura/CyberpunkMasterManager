"""Чтение настроек; никаких секретов и изменений конфигурации при запуске."""
from pathlib import Path
import tomllib
import sys

def application_directory():
    """В EXE настройки и журнал лежат рядом с EXE, не в _internal."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


APP_DIR = application_directory()


def load_settings(path=APP_DIR / "config.toml"):
    with Path(path).open("rb") as stream:
        config = tomllib.load(stream)
    config.setdefault("state", {}).setdefault("batch_cards_file", "batch_cards.json")
    for section, keys in {
        "database": ("host", "port", "user", "name", "connect_timeout", "read_timeout", "write_timeout"),
        "interface": ("page_size",),
        "reader": ("enabled", "library_path", "write_beep_t", "timeout_ms", "poll_interval",
                   "error_retry_interval", "feedback_retry_interval"),
        "state": ("pending_id_file",),
    }.items():
        for key in keys:
            if key not in config.get(section, {}):
                raise ValueError(f"В config.toml отсутствует [{section}] {key}.")
    for section, key, low, high in (
        ("database", "port", 1, 65535), ("interface", "page_size", 1, 1000),
        ("reader", "write_beep_t", 1, 255), ("reader", "timeout_ms", 1, 60000),
        *(("database", key, 1, 3600) for key in ("connect_timeout", "read_timeout", "write_timeout")),
    ):
        value = config[section][key]
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"В config.toml [{section}] {key}: целое число {low}…{high}.")
    for key in ("poll_interval", "error_retry_interval", "feedback_retry_interval"):
        value = config["reader"][key]
        if type(value) not in (int, float) or not 0.01 <= value <= 60:
            raise ValueError(f"В config.toml [reader] {key}: число 0.01…60 секунд.")
    if type(config["reader"]["enabled"]) is not bool:
        raise ValueError("В config.toml [reader] enabled: true или false.")
    for section, keys in {"database": ("host", "user", "name"),
                          "reader": ("library_path",), "state": ("pending_id_file", "batch_cards_file")}.items():
        for key in keys:
            if not isinstance(config[section][key], str) or not config[section][key].strip():
                raise ValueError(f"В config.toml [{section}] {key}: непустая строка.")
    if (APP_DIR / config["state"]["batch_cards_file"]).resolve() == (APP_DIR / config["state"]["pending_id_file"]).resolve():
        raise ValueError("Журналы batch_cards_file и pending_id_file должны быть разными файлами.")
    return config


def app_path(value):
    return (APP_DIR / Path(value).expanduser()).resolve()


try:
    SETTINGS = load_settings()
except (OSError, ValueError) as error:
    print(f"Ошибка настроек: {error}")
    raise SystemExit(1)
