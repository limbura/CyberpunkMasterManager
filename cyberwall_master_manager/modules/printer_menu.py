"""Console actions; background service never writes over a user's input line."""
from concurrent.futures import TimeoutError
from .thermal_printer import RemovalRequired

STATES = {'stopped': 'не запущен', 'disabled': 'отключён в настройках',
          'unavailable': 'модуль недоступен', 'unselected': 'не выбран',
          'disconnected': 'нет соединения', 'connecting': 'подключается',
          'connected': 'подключён'}


def safe(value):
    return ''.join(c if c.isprintable() else ' ' for c in str(value))


def show_status(service):
    if service is None:
        print('Термопринтер: сервис не запущен.')
        return
    status = service.status
    address = f' ({safe(status.address)})' if status.address else ''
    print(f"Термопринтер: {STATES.get(status.state, status.state)}{address}" +
          ('; выполняется операция' if status.busy else ''))
    if status.detail:
        print(safe(status.detail))


def wait(future):
    # Short waits leave KeyboardInterrupt responsive on Windows.
    while True:
        try:
            return future.result(timeout=0.2)
        except TimeoutError:
            if future.done():
                return future.result()  # A TimeoutError raised by the operation itself.
        except RemovalRequired:
            raise
        except Exception as error:
            raise RuntimeError(str(error)) from error


def print_label(service, account_id=None, name=None, *, mode='id-name', copies=1, ask=input):
    def send(**kwargs):
        if service is None:
            raise RuntimeError('Сервис термопринтера не запущен.')
        return wait(service.print_label(account_id, name, mode=mode, copies=copies, **kwargs))

    print(f'Передача на термопринтер: {copies} экз. …')
    try:
        result = send()
    except RemovalRequired:
        result = None
    except Exception as error:
        print(f'Печать: {safe(error)}')
        return
    if result is None:
        # Navigation exceptions from ask must reach the application's main loop.
        answer = ask('Сними все выданные наклейки и закрой крышку. Enter — снято, печатать; :b — отмена: ').strip().lower()
        if answer:
            print('Печать отменена.')
            return
        try:
            result = send(removed=True)
        except Exception as error:
            print(f'Печать: {safe(error)}')
            return
    print(f'Серия отправлена: {result.copies} экз. Физическое завершение печати не подтверждается.')


def choose_label_mode(ask):
    while True:
        print('\nПЕЧАТЬ НАКЛЕЙКИ ID')
        print('1 — ID и имя\n2 — только ID\n0, Enter, :b — назад')
        choice = ask('Вариант: ').strip().lower()
        if choice in ('', '0', ':b'):
            return None
        if choice in ('1', '2'):
            return 'id-name' if choice == '1' else 'id-only'
        print('Введи номер пункта меню.')


def templates(service, ask):
    if service is None:
        show_status(service)
        return
    while True:
        print('\nПечать шаблонов:')
        print('1 — ID и имя\n2 — только ID\n0, Enter, :b — назад')
        choice = ask('Шаблон: ').strip().lower()
        if choice in ('', '0', ':b'):
            return
        if choice not in ('1', '2'):
            continue
        limit = service.settings['max_copies']
        while True:
            value = ask(f'Количество 1…{limit} [Enter — 1; :b — назад]: ').strip()
            if value.lower() == ':b':
                break
            value = value or '1'
            if not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= limit:
                print(f'Введи целое число от 1 до {limit}.')
                continue
            print_label(service, mode='blank-id-name' if choice == '1' else 'blank-id-only', copies=int(value), ask=ask)
            return


def printer_menu(service, ask):
    while True:
        print()
        show_status(service)
        if service is None or service.status.state in ('disabled', 'unavailable', 'stopped'):
            return
        print('1 — найти и выбрать термопринтер\n2 — подключиться к последнему\n3 — отключиться')
        print('4 — калибровка этикеток\n0, Enter, :b — назад')
        choice = ask('Действие: ').strip().lower()
        if choice in ('', '0', ':b'):
            return
        try:
            if choice == '1':
                future = service.scan()
                print('Поиск термопринтеров…')
                devices = wait(future)
                if not devices:
                    print('Термопринтеры не найдены. Проверь питание и Bluetooth.')
                    continue
                for index, device in enumerate(devices, 1):
                    print(f"{index} — {safe(device['name'])} ({safe(device['address'])})")
                number = ask('Номер [Enter, :b — отмена]: ').strip()
                if number.lower() in ('', ':b'):
                    continue
                if not number.isascii() or not number.isdecimal() or not 1 <= int(number) <= len(devices):
                    print('Нет такого номера.')
                    continue
                device = devices[int(number)-1]
                print('Подключение…')
                wait(service.select(device['address'], device['name']))
            elif choice == '2':
                print('Подключение…')
                wait(service.reconnect())
            elif choice == '3':
                wait(service.disconnect())
            elif choice == '4':
                if service.settings['paper']['type'] != 'labels':
                    print('Калибровка требует printer.paper.type = "labels" в config.toml.')
                else:
                    wait(service.calibrate())
                    print('Калибровка выполнена.')
        except (RuntimeError, ValueError, OSError, ImportError) as error:
            print(f'Термопринтер: {safe(error)}')
