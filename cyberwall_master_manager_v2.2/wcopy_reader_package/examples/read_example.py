"""Читает карту один раз. Ключ --debug показывает USB-пакеты. python examples/read_example.py [--debug]."""

import argparse
import logging

import sys
from pathlib import Path
# Добавляем основную папку проекта в пути поиска модулей.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from wcopy_reader import (
    read_card_number, NoCardError, AuthenticationError, CardReadError,
    ReaderNotFoundError, ReaderOpenError, ReaderTimeoutError, USBError, RFIDError, set_led, beep,
)

# Команда индикации после чтения. Прямое задание цвета не подтверждено.
# None отключает команду LED.
SUCCESS_LED = "green"
SUCCESS_BEEP = False  # оставляем штатный звук прошивки


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true", help="показывать USB-пакеты")
    parser.add_argument("--no-feedback", action="store_true", help="не отправлять команды индикации")
    args = parser.parse_args()
    if args.debug:
        logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")
    input("Закройте nfcPro, положите одну карту на считыватель и нажмите Enter: ")
    try:
        number = read_card_number()
    except NoCardError:
        print("Карта не обнаружена. Приложите карту и повторите запуск.")
    except AuthenticationError as exc:
        print(f"Авторизация не прошла (статус {exc.status:#04x}). "
              "Проверьте ключ A и положение карты; причина не обязательно в ключе.")
    except CardReadError as exc:
        print(f"Блок не прочитан (статус {exc.status:#04x}). Проверьте положение карты и права доступа.")
    except ReaderNotFoundError:
        print("Подключите считыватель по USB.")
    except ReaderOpenError:
        print("Не удалось открыть считыватель. Закройте nfcPro и проверьте подключение.")
    except ReaderTimeoutError as exc:
        print(f"Нет ответа от считывателя на этапе {exc.stage}. Проверьте подключение и повторите.")
    except USBError as exc:
        print(f"Сбой USB на этапе {exc.stage}: {exc}")
    except RFIDError as exc:
        # Например, несколько устройств, неизвестная карта, ошибка протокола.
        print(f"{exc.code}, этап {exc.stage}: {exc}")
    else:
        print(f"Число на карте: {number}")
        if not args.no_feedback:
            actions = []
            if SUCCESS_LED is not None:
                actions.append(("светодиод", lambda: set_led(SUCCESS_LED)))
            if SUCCESS_BEEP:
                actions.append(("звук", beep))
            for label, action in actions:
                try:
                    action()
                except RFIDError as error:
                    print(f"Карта прочитана, но не удалось включить {label}: {error.code}: {error}")
        return 0
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyboardInterrupt, EOFError):
        print("\nЧтение отменено.")
        raise SystemExit(130)
