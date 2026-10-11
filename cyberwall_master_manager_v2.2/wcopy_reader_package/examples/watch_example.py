"""Постоянно ждет карту и по прикладыванию читает ее. Остановка: Ctrl+C."""
import argparse
import logging
import sys
from pathlib import Path
# Добавляем основную папку проекта в пути поиска модулей.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from wcopy_reader import watch_cards, CardPresented, CardState, RFIDError, set_led, beep

# Все решения об индикации находятся здесь, в приложении.
# Не отключайте эти команды, если нужна штатная индикация при опросе.
# На проверенной прошивке red/green/off не задают цвет напрямую, но вызовы
# после каждого опроса позволяют работать автоматическому цвету и писку.
USE_LED = True
IDLE_LED = "red"
PRESENT_LED = "green"
EXTRA_BEEP = False  # проверенный пользователем режим с одним штатным писком


def main():
    parser = argparse.ArgumentParser(description="Чтение карт со светом и звуком считывателя")
    parser.add_argument('--debug', action='store_true')
    parser.add_argument('--log-file', metavar='PATH', help='Записать подробный UTF-8 журнал прямо в файл')
    parser.add_argument('--no-beep', action='store_true', help='Отключить только нашу команду писка, сохранив LED')
    parser.add_argument('--beep', action='store_true', help='Дополнительная команда писка при новом чтении')
    parser.add_argument('--no-feedback', action='store_true', help='Не отправлять команды индикации; штатные LED/звук при опросе могут перестать работать')
    args = parser.parse_args()
    if args.beep and args.no_beep:
        parser.error('--beep и --no-beep нельзя использовать вместе')
    led_enabled = USE_LED and not args.no_feedback
    sound_enabled = (EXTRA_BEEP or args.beep) and not args.no_beep and not args.no_feedback
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if args.debug else logging.INFO)
    handlers = [console]
    if args.log_file:
        file_handler = logging.FileHandler(args.log_file, mode='w', encoding='utf-8')
        file_handler.setLevel(logging.DEBUG)
        handlers.append(file_handler)
    logging.basicConfig(level=logging.DEBUG if args.debug or args.log_file else logging.INFO,
                        handlers=handlers, format='%(asctime)s.%(msecs)03d %(levelname)s:%(name)s:%(message)s',
                        datefmt='%H:%M:%S', force=True)
    log = logging.getLogger('watch')
    log.info('Закройте nfcPro. Прикладывайте карты. Остановка: Ctrl+C.')
    log.info('Режим: LED=%s, дополнительный писк=%s', led_enabled, sound_enabled)
    try:
        for event in watch_cards(report_state=True):
            if isinstance(event, CardState):
                # Повторяем LED на каждом опросе, как в проверенной версии:
                # команды чтения/настройки поля могут изменять индикацию.
                actions = []
                if sound_enabled and event.is_new:
                    actions.append(beep)
                if led_enabled:
                    actions.append(lambda: set_led(PRESENT_LED if event.present else IDLE_LED))
                try:
                    for action in actions:
                        action()
                except RFIDError as error:
                    log.warning('Индикация отключена до перезапуска: %s; %s', error.code, error)
                    led_enabled = sound_enabled = False
            elif isinstance(event, CardPresented):
                log.info('Новая карта: UID=%s, число=%s', event.uid.hex(' ').upper(), event.number)
            else:
                error = event.error
                log.error('Ошибка: %s; этап=%s; %s', error.code, error.stage, error)
    except KeyboardInterrupt:
        log.info('Наблюдение остановлено.')
    finally:
        logging.shutdown()


if __name__ == '__main__':
    main()
