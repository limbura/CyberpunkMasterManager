"""Чтение и запись MIFARE Classic: блок 1, ключ A = FF FF FF FF FF FF.

Устройство: wCopy NSR109-HIDIC V806N / PMX-V624-C, USB 2518:6018.
Пример: number = read_card_number()
Ошибки оборудования/протокола: RFIDError и его подклассы.
Неверные аргументы API: ValueError. См. README.md и PROTOCOL.md.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import importlib
import logging
import threading
import time

__version__ = "1.4.3"
VID, PID = 0x2518, 0x6018
BLOCK = 1
KEY_A = b"\xff" * 6
_LOCK = threading.Lock()
_LOG = logging.getLogger(__name__)
_LOG.addHandler(logging.NullHandler())


class RFIDError(Exception):
    """Общая ошибка. code/stage стабильны; status — сырой код, если он есть."""
    code = "RFID_ERROR"

    def __init__(self, message: str, *, stage: str,
                 status: int | None = None, response: bytes | None = None):
        super().__init__(message)
        self.stage = stage
        self.status = status
        self.response = response


class DependencyError(RFIDError):
    code = "DEPENDENCY_ERROR"


class ReaderNotFoundError(RFIDError):
    code = "READER_NOT_FOUND"


class MultipleReadersError(RFIDError):
    code = "MULTIPLE_READERS"


class ReaderOpenError(RFIDError):
    code = "READER_OPEN_ERROR"


class USBError(RFIDError):
    code = "USB_ERROR"


class ReaderTimeoutError(RFIDError):
    code = "READER_TIMEOUT"


class ProtocolError(RFIDError):
    code = "PROTOCOL_ERROR"


class ReaderCommandError(RFIDError):
    code = "READER_COMMAND_ERROR"


class NoCardError(RFIDError):
    code = "NO_CARD"


class UnsupportedCardError(RFIDError):
    code = "UNSUPPORTED_CARD"


class AuthenticationError(RFIDError):
    """Сбой этапа авторизации; не гарантирует, что причина именно в ключе."""
    code = "AUTHENTICATION_FAILED"


class CardReadError(RFIDError):
    code = "CARD_READ_FAILED"


class CardWriteError(RFIDError):
    code = "CARD_WRITE_FAILED"


class VerificationError(RFIDError):
    """После команды записи результат не подтверждён; карта могла измениться."""
    code = "VERIFICATION_FAILED"


class FeedbackError(RFIDError):
    """Сбой индикации считывателя; не отменяет уже прочитанные данные карты."""
    code = "FEEDBACK_FAILED"


@dataclass(frozen=True)
class ReaderInfo:
    path: bytes
    product: str
    manufacturer: str
    interface: int | None


def _validate_format(offset: int, length: int, byteorder: str) -> None:
    if type(offset) is not int or type(length) is not int:
        raise ValueError("offset и length должны быть целыми числами")
    if offset < 0 or length < 1 or offset + length > 16:
        raise ValueError("Диапазон должен лежать внутри 16 байт блока")
    if byteorder not in ("big", "little"):
        raise ValueError("byteorder должен быть 'big' или 'little'")


@dataclass(frozen=True)
class CardData:
    uid: bytes
    block: bytes
    atqa: bytes
    sak: int

    def to_int(self, *, offset: int = 8, length: int = 8,
               byteorder: str = "big") -> int:
        """Беззнаковое двоичное число, не ASCII-строка и не MIFARE value block."""
        _validate_format(offset, length, byteorder)
        return int.from_bytes(self.block[offset:offset + length], byteorder, signed=False)

    @property
    def number(self) -> int:
        """Последние 8 байт блока как unsigned big-endian (uint64)."""
        return self.to_int()


def _get_hid():
    try:
        hid = importlib.import_module("hid")
    except (ImportError, OSError) as exc:
        raise DependencyError("Не удалось загрузить hidapi; установите requirements.txt",
                              stage="dependency") from exc
    if not hasattr(hid, "device") or not hasattr(hid, "enumerate"):
        raise DependencyError("Нужен пакет hidapi, импортируемый как hid",
                              stage="dependency")
    return hid


def _enumerate(hid) -> tuple[ReaderInfo, ...]:
    try:
        devices = hid.enumerate(VID, PID)
    except OSError as exc:
        raise USBError("Ошибка перечисления HID-устройств", stage="enumerate") from exc
    return tuple(ReaderInfo(d["path"], d.get("product_string") or "",
                            d.get("manufacturer_string") or "", d.get("interface_number"))
                 for d in devices if d.get("interface_number") in (0, -1, None))


def list_readers() -> tuple[ReaderInfo, ...]:
    """Найти 2518:6018, HID interface 0. Пустой результат — пустой tuple."""
    return _enumerate(_get_hid())


def _request(payload: bytes, sequence: int) -> bytes:
    if not 1 <= len(payload) <= 48:
        raise ValueError("Длина команды должна быть 1..48 байтов")
    report = bytearray([1, len(payload) + 6, sequence & 255, sequence >> 8])
    report.extend(payload)
    report.extend([~sum(report) & 255, 0xFE])
    return b"\0" + bytes(report).ljust(64, b"\0")


def _parse_report(raw: bytes, stage: str) -> tuple[int, bytes]:
    report = raw[1:] if len(raw) == 65 and raw[0] == 0 else raw
    if len(report) != 64 or report[0] != 2:
        raise ProtocolError("Некорректный размер/заголовок HID-ответа",
                            stage=stage, response=raw)
    size = report[1]
    if not 6 <= size <= 64 or report[size - 1] != 0xFD:
        raise ProtocolError("Некорректная длина/конец пакета", stage=stage, response=raw)
    if report[size - 2] != (~sum(report[:size - 2]) & 255):
        raise ProtocolError("Контрольная сумма ответа не совпала", stage=stage, response=raw)
    return int.from_bytes(report[2:4], "little"), report[4:size - 2]


def _fixed_body(body: bytes, size: int, stage: str) -> bytes:
    # Никогда не удаляем 90 00 из последних двух байтов самих данных блока.
    if len(body) == size:
        return body
    if len(body) == size + 2:
        if body[-2:] == b"\x90\0":
            return body[:-2]
        raise ReaderCommandError("Считыватель вернул ошибку статуса APDU",
                                 stage=stage, status=int.from_bytes(body[-2:], "big"),
                                 response=body)
    raise ProtocolError(f"Неожиданная длина ответа: {len(body)}, ожидается {size} или {size + 2}",
                        stage=stage, response=body)


class _Session:
    def __init__(self, device, timeout_ms: int):
        self.device = device
        self.timeout_ms = timeout_ms
        self.sequence = 0

    def exchange(self, payload: bytes, stage: str) -> bytes:
        sequence = self.sequence
        self.sequence = (sequence + 2) & 0xFFFF
        packet = _request(payload, sequence)
        _LOG.debug("%s TX %s", stage, packet.hex(" "))
        try:
            written = self.device.write(packet)
        except OSError as exc:
            raise USBError("Ошибка отправки команды по USB", stage=stage) from exc
        if written != len(packet):
            raise USBError(f"Неполная передача по USB: {written}/{len(packet)}", stage=stage)
        deadline = time.monotonic() + self.timeout_ms / 1000
        last = None
        for _ in range(20):
            remaining = int((deadline - time.monotonic()) * 1000)
            if remaining <= 0:
                break
            try:
                raw = bytes(self.device.read(65, remaining))
            except OSError as exc:
                raise USBError("Ошибка получения ответа по USB", stage=stage) from exc
            if not raw:
                break
            _LOG.debug("%s RX %s", stage, raw.hex(" "))
            received_sequence, data = _parse_report(raw, stage)
            if received_sequence == (sequence | 1):
                return data
            last = raw
        if last is not None:
            raise ProtocolError("Получены ответы с неподходящим номером запроса",
                                stage=stage, response=last)
        raise ReaderTimeoutError("Считыватель не ответил за отведённое время", stage=stage)

    def pn(self, command: bytes, stage: str) -> bytes:
        payload = b"\xff\0\0\0" + bytes([len(command) + 1, 0xD4]) + command
        data = self.exchange(payload, stage)
        if data[:2] != bytes([0xD5, command[0] + 1]):
            raise ProtocolError("Неожиданный ответ на PN-совместимую команду",
                                stage=stage, response=data)
        return data[2:]

    def configure(self, command: bytes, stage: str) -> None:
        _fixed_body(self.pn(command, stage), 0, stage)

    def initialize(self) -> None:
        init = self.exchange(bytes.fromhex("FF 00 9A 5A A5 54 69 61 6E"), "initialize")
        if init != b"\0":
            raise ReaderCommandError("Считыватель не подтвердил инициализацию",
                                     stage="initialize", response=init)

    def feedback_command(self, payload: bytes) -> bytes:
        body = self.exchange(payload, "feedback")
        # На проверенном wCopy ответ 90 01 не отражает реальный цвет LED.
        # Принимаем известные формы ответа, но не трактуем их как физическое состояние.
        if body == b"\0" or (len(body) == 2 and body[0] == 0x90 and body[1] <= 3):
            return body
        raise FeedbackError("Считыватель не подтвердил команду света/звука: " + body.hex(" "),
                            stage="feedback", response=body)

    def feedback(self, color: str, beep: bool) -> tuple[bytes, ...]:
        replies = []
        if beep:
            replies.append(self.beep())
        replies.append(self.set_led(color))
        return tuple(replies)

    def beep(self, t: int = 5) -> bytes:
        # Та же команда, которую формирует nfcPro_x64: FF 00 40 50 04 T 01 01 01.
        # T — значение поля команды, не миллисекунды. Цвет этим вызовом не задаём.
        _validate_beep_t(t)
        return self.feedback_command(bytes.fromhex("FF 00 40 50 04") + bytes([t, 1, 1, 1]))

    def set_led(self, color: str) -> bytes:
        control = {"off": 0x0C, "red": 0x0D, "green": 0x0E}[color]
        return self.feedback_command(bytes([0xFF, 0, 0x40, control, 4, 0, 0, 0, 0]))

    def read(self) -> CardData:
        self.initialize()
        # Сохраняем последовательность из успешного аппаратного теста.
        self.configure(bytes.fromhex("32 01 00"), "rf_off")
        time.sleep(0.05)
        self.configure(bytes.fromhex("32 01 01"), "rf_on")
        time.sleep(0.05)
        self.configure(bytes.fromhex("32 05 00 01 02"), "configure")
        body = self.pn(bytes.fromhex("4A 01 00"), "poll")
        # Наблюдение на NSR109-HIDIC V806N: без карты тело ответа D5 4B = 63 00.
        # Это firmware-specific трактовка результата поиска, не универсальный
        # смысл статуса 6300. Возможен также неуспешный поиск при наличии карты.
        if body == b"\x63\x00":
            raise NoCardError("Карта не обнаружена или поиск не удался (статус 63 00)",
                              stage="poll", status=0x6300, response=body)
        if body and body[0] == 0:
            _fixed_body(body, 1, "poll")
            raise NoCardError("Карта не обнаружена", stage="poll")
        if len(body) < 6 or body[0] != 1 or body[1] != 1:
            raise ProtocolError("Некорректный ответ поиска карты", stage="poll", response=body)
        sak, uid_size = body[4], body[5]
        if uid_size not in (4, 7):
            raise UnsupportedCardError("Неподдерживаемая длина UID", stage="poll", response=body)
        if len(body) < 6 + uid_size:
            raise ProtocolError("Неполный UID в ответе", stage="poll", response=body)
        if sak not in (0x08, 0x18, 0x09, 0x88):
            raise UnsupportedCardError(f"Неожиданный для Classic SAK=0x{sak:02X}",
                                       stage="poll", response=body)
        body = _fixed_body(body, 6 + uid_size, "poll")
        uid, atqa = body[6:], body[2:4]
        auth = bytes([0x40, 1, 0x60, BLOCK]) + KEY_A + uid[-4:]
        self.data_exchange(auth, 0, "authenticate", AuthenticationError)
        block = self.data_exchange(bytes([0x40, 1, 0x30, BLOCK]), 16, "read", CardReadError)
        return CardData(uid, block, atqa, sak)

    def data_exchange(self, command: bytes, size: int, stage: str, error_type) -> bytes:
        body = self.pn(command, stage)
        if not body:
            raise ProtocolError("В ответе отсутствует статус операции", stage=stage, response=body)
        if body[0] != 0:
            raise error_type(f"Операция {stage} завершилась со статусом 0x{body[0]:02X}",
                             stage=stage, status=body[0], response=body)
        return _fixed_body(body, size + 1, stage)[1:]


    def write_number(self, encoded: bytes, offset: int) -> CardData:
        before = self.read()
        expected = before.block[:offset] + encoded + before.block[offset + len(encoded):]
        # PN InDataExchange / MIFARE Write: один блок целиком, 16 байт.
        # Не повторяем запись автоматически при тайм-ауте/сбое.
        self.data_exchange(bytes([0x40, 1, 0xA0, BLOCK]) + expected,
                           0, "write", CardWriteError)
        try:
            # Новый RF-сеанс, поиск и авторизация: проверяем память карты,
            # а также UID, чтобы не принять другую карту за записанную.
            after = self.read()
        except RFIDError as exc:
            raise VerificationError(
                f"Команда записи выполнена, но обратное чтение не удалось: {exc}",
                stage="verify", status=exc.status, response=exc.response) from exc
        if after.uid != before.uid:
            raise VerificationError("При обратном чтении обнаружена другая карта (UID изменился)",
                                    stage="verify", response=after.uid)
        if after.block != expected:
            raise VerificationError("Обратное чтение не совпало с записанным блоком",
                                    stage="verify", response=after.block)
        return after


def read_card(*, path: bytes | None = None, timeout_ms: int = 2500) -> CardData:
    """Однократно прочитать блок 1 ключом A=FF…FF; открыть/закрыть USB автоматически.

    path — из list_readers(); при None требуется ровно одно устройство.
    timeout_ms — ожидание одного HID-ответа, НЕ срок всей функции.
    Карта должна уже лежать на считывателе. Повторный вызов снова читает карту.
    Функция блокирующая. Вызовы этого модуля сериализуются внутри процесса.
    """
    with _open_session(path, timeout_ms) as session:
        return session.read()


@contextmanager
def _open_session(path: bytes | None, timeout_ms: int) -> Iterator[_Session]:
    if type(timeout_ms) is not int or not 1 <= timeout_ms <= 60000:
        raise ValueError("timeout_ms должен быть целым числом от 1 до 60000")
    if path is not None and (not isinstance(path, bytes) or not path):
        raise ValueError("path должен быть непустым bytes из list_readers() либо None")
    with _LOCK:
        hid = _get_hid()
        readers = _enumerate(hid)
        if path is not None:
            readers = tuple(r for r in readers if r.path == path)
        if not readers:
            raise ReaderNotFoundError("Считыватель 2518:6018 не найден", stage="enumerate")
        if len(readers) != 1:
            raise MultipleReadersError("Найдено несколько считывателей; задайте path из list_readers()",
                                       stage="enumerate")
        device = None
        primary_error = None
        try:
            try:
                device = hid.device()
                device.open_path(readers[0].path)
            except OSError as exc:
                raise ReaderOpenError("Не удалось открыть HID-интерфейс; возможно, он занят или отключён",
                                      stage="open") from exc
            yield _Session(device, timeout_ms)
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            if device is not None:
                try:
                    device.close()
                except OSError as exc:
                    if primary_error is None:
                        raise USBError("Ошибка закрытия HID-интерфейса", stage="close") from exc
                    primary_error.add_note(f"Дополнительно не удалось закрыть HID: {exc}")


def read_card_number(*, path: bytes | None = None, timeout_ms: int = 2500,
                     offset: int = 8, length: int = 8, byteorder: str = "big") -> int:
    """Вернуть беззнаковое число из блока 1. При ошибке поднять RFIDError.

    По умолчанию: последние 8 байт, big-endian (uint64). Ноль — корректное значение карты.
    Например, последние 4 байта: offset=12, length=4.
    Формат задаёт записывающее приложение; автоматически он не определяется.
    """
    _validate_format(offset, length, byteorder)
    return read_card(path=path, timeout_ms=timeout_ms).to_int(
        offset=offset, length=length, byteorder=byteorder)


def write_card_number(number: int, *, path: bytes | None = None,
                      timeout_ms: int = 2500, offset: int = 8,
                      length: int = 8, byteorder: str = "big") -> int:
    """Записать ID в блок 1 ключом A=FF…FF и обязательно проверить чтением.

    По умолчанию меняются байты 8..15 (uint64 big-endian); остальные сохраняются.
    Возвращает число, фактически прочитанное после записи, только при совпадении
    всего блока и UID. Карта должна лежать на считывателе до завершения.
    RFIDError означает, что успех НЕ подтверждён; после команды write данные
    уже могли измениться. Автоматических повторов записи нет.
    Формат и диапазон проверяются до обращения к оборудованию.
    """
    _validate_format(offset, length, byteorder)
    if type(number) is not int or not 0 <= number < (1 << (8 * length)):
        raise ValueError(f"number должен быть целым числом от 0 до {(1 << (8 * length)) - 1}")
    encoded = number.to_bytes(length, byteorder, signed=False)
    with _open_session(path, timeout_ms) as session:
        return session.write_number(encoded, offset).to_int(
            offset=offset, length=length, byteorder=byteorder)


def set_led(color: str, *, path: bytes | None = None, timeout_ms: int = 2500,
            initialize: bool = False) -> bytes:
    """Послать команду индикации red/green/off; вернуть сырой ответ.

    На NSR109-HIDIC V806N прямое задание цвета этими масками не подтверждено:
    red/green/off ведут себя одинаково. Однако вызов после КАЖДОГО опроса,
    включая «нет карты», нужен в проверенной схеме для штатного красного/
    зелёного LED и писка прошивки. Сохраняйте вызовы, как в watch_example.
    off также не гарантирует выключение LED. Ответ 90 01 не доказывает цвет.
    Самостоятельный вызов после записи не дал видимого переключения.
    Отдельной команды писка здесь нет; штатный звук может возникнуть сам.
    initialize=True добавляет INIT перед командой; обычно после read/write
    используется False. Память карты не читается и не записывается.
    """
    if color not in ("red", "green", "off"):
        raise ValueError("color должен быть red, green или off")
    if type(initialize) is not bool:
        raise ValueError("initialize должен быть bool")
    with _open_session(path, timeout_ms) as session:
        if initialize:
            session.initialize()
        return session.set_led(color)


def _validate_beep_t(t: int) -> None:
    if type(t) is not int or not 1 <= t <= 255:
        raise ValueError("t должен быть целым числом от 1 до 255 (поле T, не миллисекунды)")


def beep(*, t: int = 5, path: bytes | None = None, timeout_ms: int = 2500,
         initialize: bool = False) -> bytes:
    """Подать один сигнал командой nfcPro; вернуть сырой ответ.

    t — сырое поле T (целое 1..255), НЕ миллисекунды. По умолчанию 5,
    как в обычных вызовах nfcPro. Аппаратно проверены T=1,3,5,10; увеличение
    удлиняет писк. T=10 используется в write_example после проверки записи.
    Для больших T может потребоваться увеличить timeout_ms (например, 5000).
    Недопустимое t вызывает ValueError до обращения к USB.
    initialize=True выполняет INIT; после read/write он обычно не требуется.
    Не посылает команду установки цвета и не обращается к памяти карты.
    """
    _validate_beep_t(t)
    if type(initialize) is not bool:
        raise ValueError("initialize должен быть bool")
    with _open_session(path, timeout_ms) as session:
        if initialize:
            session.initialize()
        return session.beep(t)


def set_reader_feedback(color: str, *, beep: bool = False, path: bytes | None = None,
                        timeout_ms: int = 2500, initialize: bool = False) -> tuple[bytes, ...]:
    """Совместимый комбинированный вызов команд индикации.

    color: red/green/off с ограничениями set_led (прямой цвет не подтверждён).
    beep=True: один сигнал с T=5. Для выбора T используйте отдельный beep(t=...).
    Возвращает ответы устройства: подтверждение команды не доказывает цвет/звук.
    initialize=True для самостоятельного теста, False после чтения в watch.
    Не отправляет команд чтения/записи памяти карты.
    """
    if color not in ("red", "green", "off"):
        raise ValueError("color должен быть red, green или off")
    if type(beep) is not bool or type(initialize) is not bool:
        raise ValueError("beep и initialize должны быть bool")
    with _open_session(path, timeout_ms) as session:
        if initialize:
            session.initialize()
        return session.feedback(color, beep)


@dataclass(frozen=True)
class CardPresented:
    """Новая успешно прочитанная карта. number — uint64, uid — байты UID."""
    card: CardData

    @property
    def number(self) -> int:
        return self.card.number

    @property
    def uid(self) -> bytes:
        return self.card.uid


@dataclass(frozen=True)
class CardError:
    """Ошибка чтения/подключения во время наблюдения."""
    error: RFIDError


@dataclass(frozen=True)
class CardState:
    """Состояние после каждого успешного опроса или ответа «нет карты».

    present учитывает removal_checks. is_new=True только при новом успешном
    чтении; затем отдельно выдаётся CardPresented с данными. Ошибки USB и
    авторизации не превращаются в CardState(False). Включается report_state=True.
    """
    present: bool
    is_new: bool = False


def watch_cards(*, stop: threading.Event | None = None,
                interval: float = 0.25, removal_checks: int = 2,
                path: bytes | None = None, timeout_ms: int = 2500,
                feedback: bool = False, sound: bool = True,
                report_state: bool = False) -> Iterator[CardPresented | CardError | CardState]:
    """Блокирующий генератор CardPresented / CardError; см. README.

    Событие карты — только после успешного чтения. Повторы одного UID подавляются
    до removal_checks последовательных ответов «нет карты» или другого UID.
    USB-сбой, тайм-аут и ошибка ключа НЕ считаются снятием карты.
    stop.set() завершает цикл после текущей операции. Запускать в рабочем потоке.
    Исключения вызывающего кода не перехватываются.
    feedback=True: команды для штатной индикации прошивки (не прямого цвета).
    Ошибка индикации выдаётся отдельно и отключает индикацию до перезапуска
    генератора; чтение продолжается. Удерживаемая карта повторно не пищит.
    sound=False отключает только нашу команду писка, сохраняя команды LED.
    На самостоятельные звуки прошивки этот параметр не влияет.
    report_state=True добавляет CardState на каждом опросе для индикации
    в приложении. По умолчанию команд LED/звука нет. feedback/sound сохранены
    для совместимости; новым приложениям лучше вызывать set_led/beep самим.
    """
    import math
    if not isinstance(interval, (int, float)) or not math.isfinite(interval) or interval <= 0:
        raise ValueError("interval должен быть положительным конечным числом секунд")
    if type(removal_checks) is not int or removal_checks < 1:
        raise ValueError("removal_checks должен быть целым числом >= 1")
    if type(timeout_ms) is not int or not 1 <= timeout_ms <= 60000:
        raise ValueError("timeout_ms должен быть целым числом от 1 до 60000")
    if path is not None and (not isinstance(path, bytes) or not path):
        raise ValueError("path должен быть непустым bytes либо None")
    if type(feedback) is not bool:
        raise ValueError("feedback должен быть bool")
    if type(sound) is not bool:
        raise ValueError("sound должен быть bool")
    if type(report_state) is not bool:
        raise ValueError("report_state должен быть bool")
    stop = stop if stop is not None else threading.Event()
    last_uid = None
    absent = 0
    last_error = None
    feedback_enabled = feedback

    def update_feedback(color, beep=False):
        nonlocal feedback_enabled
        if not feedback_enabled:
            return None
        try:
            set_reader_feedback(color, beep=beep, path=path, timeout_ms=timeout_ms)
        except RFIDError as error:
            feedback_enabled = False
            failure = FeedbackError(
                f"Индикация отключена до перезапуска наблюдения: {error}",
                stage="feedback", status=error.status, response=error.response)
            failure.__cause__ = error
            return CardError(failure)
        return None

    while not stop.is_set():
        try:
            card = read_card(path=path, timeout_ms=timeout_ms)
        except NoCardError:
            absent += 1
            last_error = None
            if absent >= removal_checks:
                last_uid = None
            warning = update_feedback("green" if last_uid is not None else "red")
            if report_state:
                yield CardState(last_uid is not None)
            if warning is not None:
                yield warning
        except RFIDError as error:
            absent = 0
            signature = (error.code, error.stage, error.status)
            if signature != last_error:
                last_error = signature
                yield CardError(error)
            # USB/ошибки поиска не доказывают наличие рабочего канала управления.
            # Не создаём повторные задержки дополнительными запросами при сбоях.
        else:
            absent = 0
            last_error = None
            is_new = card.uid != last_uid
            warning = update_feedback("green", beep=is_new and sound)
            if report_state:
                yield CardState(True, is_new)
            if is_new:
                last_uid = card.uid
                yield CardPresented(card)
            if warning is not None:
                yield warning
        if stop.wait(interval):
            break
