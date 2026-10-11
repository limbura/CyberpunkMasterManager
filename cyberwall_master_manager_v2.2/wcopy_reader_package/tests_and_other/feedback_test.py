"""Этот скрипт просто отправляет разные команды индикации на считыватель чтобы выяснить как работает индикация. Включён журнал HID-пакетов."""
import logging
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from wcopy_reader import RFIDError, _open_session


def main():
    logging.basicConfig(level=logging.DEBUG)
    print('Закройте nfcPro и watch_example.py. Уберите карту со считывателя.')
    print('Тест не читает и не записывает память карты.')
    input('Нажмите Enter для начала...')
    steps = [
        ('Красный без звука', 'FF 00 40 0D 04 00 00 00 00'),
        ('Зелёный без звука', 'FF 00 40 0E 04 00 00 00 00'),
        ('Выключение обоих цветов', 'FF 00 40 0C 04 00 00 00 00'),
        ('Сигнал командой nfcPro (возможна вспышка LED)', 'FF 00 40 50 04 01 01 01 01'),
        ('Возврат красного', 'FF 00 40 0D 04 00 00 00 00'),
    ]
    with _open_session(None, 2500) as session:
        session.initialize()
        for label, command in steps:
            input(f'\n{label}. Нажмите Enter...')
            # Печатаем даже неизвестный ответ для диагностики, без утверждения об успехе.
            reply = session.exchange(bytes.fromhex(command), 'feedback_test')
            print('Ответ:', reply.hex(' ').upper(), flush=True)
            print('Наблюдайте реальный цвет/звук — ответ сам по себе этого не подтверждает.')
    print('\nТест завершён. Пришлите вывод и какие шаги сработали.')


if __name__ == '__main__':
    try:
        main()
    except RFIDError as error:
        print(f'Ошибка: {error.code}; {error.stage}; {error}')
        raise SystemExit(1)
    except (KeyboardInterrupt, EOFError):
        print('\nТест остановлен.')
