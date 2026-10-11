"""Записывает карту с проверкой записи последующим чтением: python write_example.py [ID] [--debug]."""
import argparse
import logging

import sys
from pathlib import Path
# Добавляем основную папку проекта в пути поиска модулей.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from wcopy_reader import RFIDError, write_card_number, set_led, beep

# Индикацией управляет этот пример, а не write_card_number().
# Команда LED: None (не отправлять), "red", "green", "off".
# Прямое управление цветом не подтверждено. После записи — отдельный beep().
SUCCESS_LED = None
SUCCESS_BEEP = True
SUCCESS_BEEP_T = 10  # поле T команды; не миллисекунды


def main():
    parser = argparse.ArgumentParser(description="Запись ID в блок 1 и проверка чтением")
    parser.add_argument("number", nargs="?", type=int, help="ID: целое число 0..18446744073709551615")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--no-beep", action="store_true", help="не отправлять команду писка после записи")
    parser.add_argument("--no-feedback", action="store_true", help="не отправлять команды индикации")
    args = parser.parse_args()
    if args.debug:
        logging.basicConfig(level=logging.DEBUG)
    try:
        number = args.number
        if number is None:
            number = int(input("Введите ID для записи: ").strip(), 10)
        if not 0 <= number <= 18446744073709551615:
            raise ValueError("ID должен быть от 0 до 18446744073709551615")
        input(f"Приложите одну карту для записи ID {number} и нажмите Enter. "
              "Не убирайте карту до результата.")
        verified = write_card_number(number)
    except ValueError as error:
        print(f"Некорректный ID: {error}")
        return 2
    except RFIDError as error:
        print(f"Успех записи не подтверждён: {error.code}, этап {error.stage}: {error}")
        if error.stage in ("write", "verify", "close"):
            print("Данные карты уже могли измениться. Проверьте её через read_example.py.")
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nОперация прервана. Если запись уже началась, проверьте ID через read_example.py.")
        return 130
    print(f"УСПЕХ: записан ID {verified}; обратное чтение подтвердило весь блок.")
    # Этот этап уже не относится к записи: сбой пищалки не отменяет её успех.
    if not args.no_feedback:
        actions = []
        if SUCCESS_LED is not None:
            actions.append(("светодиод", lambda: set_led(SUCCESS_LED)))
        if SUCCESS_BEEP and not args.no_beep:
            actions.append(("звук", lambda: beep(t=SUCCESS_BEEP_T)))
        for label, action in actions:
            try:
                action()
            except RFIDError as error:
                print(f"Запись проверена, но не удалось включить {label}: {error.code}: {error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
