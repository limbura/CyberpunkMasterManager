"""Точка входа переносимого EXE; проверка сборки не подключается к БД или USB."""
import json
from pathlib import Path
import sys
import traceback


def self_test():
    import inspect
    import pymysql
    import pymysql._auth
    import hid
    import wcopy_reader
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    from modules.settings import APP_DIR, SETTINGS
    from modules.card_console import rfid, READER_IMPORT_ERROR
    import cyberwall_master_manager as app
    from modules import batch_cards

    if not getattr(sys, "frozen", False):
        raise RuntimeError("Проверять нужно собранный EXE.")
    if APP_DIR != Path(sys.executable).resolve().parent:
        raise RuntimeError("Настройки загружены не из папки EXE.")
    if rfid is None or READER_IMPORT_ERROR:
        raise RuntimeError(f"Библиотека считывателя не загрузилась: {READER_IMPORT_ERROR}")
    for name in ("read_card", "write_card_number", "set_led", "beep"):
        if not callable(getattr(rfid, name, None)):
            raise RuntimeError(f"В API считывателя отсутствует {name}.")
    # Проверяем загрузку нативного HID-модуля без открытия устройства.
    if not callable(hid.enumerate):
        raise RuntimeError("hidapi недоступна.")
    if not {"expected_uid", "expected_block"} <= set(inspect.signature(wcopy_reader.write_card_number).parameters):
        raise RuntimeError("Пакетной записи требуется API wcopy_reader 1.4.4+.")
    if not callable(batch_cards.run):
        raise RuntimeError("Модуль пакетной записи отсутствует в сборке.")
    # Проверяем реальную RSA-ветку авторизации MySQL, включая нативную cryptography.
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = key.public_key().public_bytes(serialization.Encoding.PEM,
                                          serialization.PublicFormat.SubjectPublicKeyInfo)
    password, salt = b"offline-check", b"12345678901234567890"
    encrypted = pymysql._auth.sha2_rsa_encrypt(password, salt, public)
    decoded = key.decrypt(encrypted, padding.OAEP(mgf=padding.MGF1(hashes.SHA1()),
                                                algorithm=hashes.SHA1(), label=None))
    expected = bytes(value ^ salt[i % len(salt)] for i, value in enumerate(password + b"\0"))
    if decoded != expected:
        raise RuntimeError("Проверка RSA-авторизации не пройдена.")
    # Это локальная проверка наличия файлов; соединения и изменения БД не выполняются.
    print(json.dumps({"status": "ok", "application_version": app.VERSION,
                      "reader_version": wcopy_reader.__version__, "frozen": True,
                      "config_file": str(APP_DIR / "config.toml"),
                      "database": SETTINGS["database"]["name"]}, ensure_ascii=True))
    return 0


def main():
    diagnostic = "--self-test" in sys.argv[1:]
    try:
        if diagnostic:
            return self_test()
        import cyberwall_master_manager
        return cyberwall_master_manager.main()
    except SystemExit as error:
        return error.code if isinstance(error.code, int) else 1
    except (KeyboardInterrupt, EOFError):
        return 0
    except Exception:
        traceback.print_exc()
        return 1
    finally:
        if not diagnostic:
            try:
                input("\nНажми Enter, чтобы закрыть окно...")
            except (KeyboardInterrupt, EOFError):
                pass


if __name__ == "__main__":
    sys.exit(main())
