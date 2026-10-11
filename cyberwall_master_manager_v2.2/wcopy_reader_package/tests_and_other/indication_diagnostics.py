"""Сравнение режимов индикации. Читает карты; никогда не записывает их память.

Запуск напрямую, в том числе кнопкой VS Code. Сначала выберите группу 1.
Сырые ответы устройства и наблюдения пользователя сохраняются отдельно.
"""
from __future__ import annotations

import json
import logging
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import wcopy_reader as reader

POLL_INTERVAL = 0.25
TIMEOUT_MS = 2500
# Длительности фаз: без карты / удерживать карту / снова без карты.
PHASES = ((5, "Уберите карту. Пока НЕ прикладывайте её."),
          (8, "ПРИЛОЖИТЕ одну карту и держите неподвижно."),
          (5, "УБЕРИТЕ карту и держите вдали от считывателя."))
LED_COMMANDS = {
    "red": bytes.fromhex("FF 00 40 0D 04 00 00 00 00"),
    "green": bytes.fromhex("FF 00 40 0E 04 00 00 00 00"),
    "off": bytes.fromhex("FF 00 40 0C 04 00 00 00 00"),
}
POLL_MODES = (
    ("A", "none", "Чтение без дополнительных действий (как --no-feedback)"),
    ("B", "adaptive", "Чтение + red без карты / green с картой (как обычный watch)"),
    ("C", "reopen", "Чтение + открыть/закрыть HID без отправки команды"),
    ("D", "delay", "Чтение + пауза вместо команды, по времени режима B"),
    ("E", "red", "Чтение + ВСЕГДА команда red, независимо от карты"),
    ("F", "green", "Чтение + ВСЕГДА команда green, независимо от карты"),
    ("G", "off", "Чтение + ВСЕГДА команда off, независимо от карты"),
)
LOG = logging.getLogger("diagnostics")


def say(message):
    print(message, flush=True)
    LOG.info(message)


def ask(message):
    say(message)
    answer = input("> ").strip()
    LOG.info("USER: %s", answer or "[пустой ответ]")
    return answer


def cold_start(label, card_present=False):
    say("\n" + "=" * 65 + "\n" + label)
    ask("Закройте другие программы считывателя. Уберите карту.\n"
        "Отключите USB считывателя, подождите 3 секунды, подключите снова.\n"
        "Дождитесь его готовности, затем нажмите Enter.")
    if card_present:
        ask("Теперь приложите одну карту и держите её.\n"
            "Дождитесь окончания штатного писка прикладывания, затем Enter.")


def send_led(session, color):
    # Обходим только проверку семантики статуса: сохраняем даже неизвестный ответ.
    return session.exchange(LED_COMMANDS[color], "diagnostic_led_" + color)


def send_beep(session, duration):
    if type(duration) is not int or duration not in (1, 3, 5, 10):
        raise ValueError("В диагностике разрешены T=1, 3, 5, 10")
    # Меняем только T в уже используемой команде nfcPro. Единицы неизвестны.
    return session.exchange(bytes.fromhex("FF 00 40 50 04") +
                            bytes([duration, 1, 1, 1]), "diagnostic_beep")


def polling_action(mode, present, delay_seconds):
    if mode == "none":
        return None
    if mode == "delay":
        time.sleep(delay_seconds)
        return None
    if mode == "reopen":
        with reader._open_session(None, TIMEOUT_MS):
            pass
        return None
    color = ("green" if present else "red") if mode == "adaptive" else mode
    with reader._open_session(None, TIMEOUT_MS) as session:
        return send_led(session, color)


def timed_poll(mode, delay_seconds):
    last_uid = None
    absent = 0
    costs = []
    samples = []
    previous_state = None
    # Порядок read -> закрытие HID -> индикация -> интервал соответствует watch.
    for phase_index, (seconds, instruction) in enumerate(PHASES):
        say(f"\nФаза {phase_index + 1}/3 — {seconds} с: {instruction}")
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            cycle_start = time.monotonic()
            try:
                card = reader.read_card(timeout_ms=TIMEOUT_MS)
            except reader.NoCardError:
                absent += 1
                if absent >= 2:
                    last_uid = None
                detected = False
            else:
                absent = 0
                last_uid = card.uid
                detected = True
            # Другие ошибки завершают опыт: не принимаем сбой связи за отсутствие.
            present = last_uid is not None
            action_start = time.monotonic()
            reply = polling_action(mode, present, delay_seconds)
            cost = time.monotonic() - action_start
            costs.append(cost)
            sample = {"phase": phase_index + 1, "detected": detected,
                      "present_debounced": present, "action_ms": round(cost * 1000, 3),
                      "read_ms": round((action_start - cycle_start) * 1000, 3),
                      "reply": reply.hex(" ") if reply is not None else None}
            samples.append(sample)
            LOG.info("POLL %s", json.dumps(sample))
            if present != previous_state:
                say("Результат чтения: " + ("карта есть" if present else "карты нет"))
                previous_state = present
            time.sleep(POLL_INTERVAL)
    return samples, statistics.median(costs)


def observations():
    return {
        "led_absent_before": ask("Какой цвет был БЕЗ карты в первой фазе?"),
        "led_present": ask("Какой цвет был С картой? Было ли мигание?"),
        "led_absent_after": ask("Какой цвет стал ПОСЛЕ снятия карты?"),
        "sound": ask("Звук: когда возникал, сколько писков, короткие/длинные?\n"
                     "Укажите отдельно прикладывание, удержание и снятие."),
    }


def poll_suite(records, save):
    delay_seconds = None
    for code, mode, title in POLL_MODES:
        if mode == "delay" and delay_seconds is None:
            say("D пропущен: режим B не завершён, эталон времени неизвестен.")
            continue
        cold_start(f"ОПРОС {code}: {title}")
        entry = {"suite": "poll", "case": code, "mode": mode,
                 "delay_seconds": delay_seconds if mode == "delay" else None}
        records.append(entry)
        save()
        try:
            entry["samples"], median = timed_poll(mode, delay_seconds or 0)
            entry["median_action_ms"] = median * 1000
            entry["status"] = "completed"
            if mode == "adaptive":
                delay_seconds = median
                say(f"В режиме B дополнительное действие занимало около {median * 1000:.1f} мс.")
        except reader.RFIDError as error:
            entry["status"] = "failed"
            entry["error"] = str(error)
            LOG.exception("Опыт прерван ошибкой устройства")
            say(f"Опыт прерван: {error.code}, {error.stage}: {error}")
        entry["observations"] = observations()
        save()
        if ask("Enter — следующий режим; q — закончить эту группу.").lower() == "q":
            break


def color_sequence(session, entry, save):
    for color in ("red", "green", "off", "red"):
        ask(f"Сейчас отправим ТОЛЬКО команду {color}. Enter.")
        say(f"ОТПРАВКА LED {color}")
        reply = send_led(session, color)
        say("Ответ устройства: " + reply.hex(" "))
        entry["commands"].append({"color_requested": color, "reply": reply.hex(" "),
            "observed": ask("Реальный цвет и звук после команды? (нет эффекта — так и напишите)")})
        save()


def led_suite(records, save):
    contexts = (
        ("startup", "После включения USB, БЕЗ INIT и чтения", False),
        ("init", "После INIT, БЕЗ чтения", False),
        ("same", "После чтения карты, В ТОМ ЖЕ HID-сеансе", True),
        ("reopen", "После чтения карты, В НОВОМ HID-сеансе", True),
    )
    for context, title, card_present in contexts:
        cold_start("ЦВЕТ: " + title, card_present)
        entry = {"suite": "led", "context": context, "commands": []}
        records.append(entry)
        save()
        try:
            if context == "reopen":
                reader.read_card(timeout_ms=TIMEOUT_MS)
            with reader._open_session(None, TIMEOUT_MS) as session:
                if context == "init":
                    session.initialize()
                elif context == "same":
                    session.read()
                entry["before_commands"] = ask("Подготовка завершена. Какой сейчас цвет и был ли звук?")
                save()
                color_sequence(session, entry, save)
            entry["status"] = "completed"
            entry["autonomous_after_close"] = ask(
                "HID закрыт. Программа больше НЕ отправляет команд.\n"
                "Снимите карту, приложите и снова снимите. Как ведут себя цвет и звук?")
        except reader.RFIDError as error:
            entry["status"] = "failed"
            entry["error"] = str(error)
            LOG.exception("Опыт прерван ошибкой устройства")
            say(f"Ошибка {error.code}, {error.stage}: {error}")
        save()
        if ask("Enter — следующий режим; q — закончить эту группу.").lower() == "q":
            break


def beep_suite(records, save):
    cold_start("ЗВУК: после чтения, отдельный HID-сеанс на каждую команду", True)
    reader.read_card(timeout_ms=TIMEOUT_MS)
    say("Чтение завершено. Держите карту на месте; после этого чтения больше не будет.")
    for duration in (1, 3, 5, 10):
        ask(f"Проверка T={duration}. Дождитесь тишины и нажмите Enter.")
        entry = {"suite": "beep", "T": duration}
        records.append(entry)
        save()
        say(f"ОТПРАВКА BEEP T={duration}")
        with reader._open_session(None, TIMEOUT_MS) as session:
            reply = send_beep(session, duration)
        entry["reply"] = reply.hex(" ")
        say("Ответ устройства: " + entry["reply"])
        entry["observed"] = ask("Был ли писк? Стал ли длиннее? Сколько сигналов? Что сделал LED?")
        entry["status"] = "completed"
        save()


def main():
    output_dir = ROOT / "diagnostics_logs"
    output_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    log_path = output_dir / f"indication_{stamp}.log"
    json_path = output_dir / f"indication_{stamp}.json"
    logging.basicConfig(level=logging.DEBUG, filename=log_path, encoding="utf-8",
                        format="%(asctime)s.%(msecs)03d %(levelname)s:%(name)s:%(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S", force=True)
    data = {"library_version": reader.__version__, "python": sys.version,
            "platform": sys.platform, "records": []}
    def save():
        json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    save()
    say("Диагностика индикации. Чтение карт возможно, ЗАПИСИ НА КАРТУ НЕТ.")
    say("Закройте nfcPro, watch и остальные программы считывателя. Подключите только один считыватель.")
    say("Ответ 90 01 не считается подтверждением реального цвета или звука.")
    say("Логи: " + str(output_dir))
    try:
        while True:
            choice = ask("\n1 — Опрос: сравнить семь режимов (НАЧНИТЕ С ЭТОГО, около 5 минут)\n"
                         "2 — Цвет: одиночные команды в четырёх состояниях устройства\n"
                         "3 — Звук: сравнить T=1, 3, 5, 10 (единицы времени пока неизвестны)\n"
                         "q — завершить")
            if choice == "1":
                poll_suite(data["records"], save)
            elif choice == "2":
                led_suite(data["records"], save)
            elif choice == "3":
                beep_suite(data["records"], save)
            elif choice.lower() == "q":
                break
    except (KeyboardInterrupt, EOFError):
        say("\nДиагностика прервана. Уже собранные результаты сохранены.")
    except reader.RFIDError as error:
        LOG.exception("Ошибка устройства")
        data["fatal_error"] = {"code": error.code, "stage": error.stage, "message": str(error)}
        say(f"Ошибка {error.code}, {error.stage}: {error}")
    finally:
        save()
        say("Пришлите оба файла:\n" + str(log_path) + "\n" + str(json_path))
        logging.shutdown()


if __name__ == "__main__":
    main()
