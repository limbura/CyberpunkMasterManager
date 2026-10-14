"""Последнее успешное подключение. Windows: DPAPI текущего пользователя.

Файл находится в пользовательском каталоге, вне исходников и переносимой сборки.
На POSIX используется приватный каталог 0700 и файл 0600 (без шифрования).
"""
import ctypes as ct
import json
import os
from pathlib import Path
import tempfile


class InvalidProfile(ValueError):
    pass


def profile_directory():
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "CYBERWALL" / "cyberwall_master_manager"


def validate(profile):
    if not isinstance(profile, dict) or set(profile) != {"host", "port", "user", "database", "password"}:
        raise InvalidProfile("Некорректное сохранённое подключение.")
    if any(not isinstance(profile[k], str) or not profile[k].strip() for k in ("host", "user", "database")):
        raise InvalidProfile("В сохранённом подключении отсутствуют обязательные параметры.")
    if type(profile["port"]) is not int or not 1 <= profile["port"] <= 65535 or not isinstance(profile["password"], str):
        raise InvalidProfile("Некорректный порт или пароль сохранённого подключения.")
    return dict(profile)


def dpapi(data, *, decrypt=False):
    """CryptProtectData/CryptUnprotectData; без UI и без LOCAL_MACHINE."""
    class Blob(ct.Structure):
        _fields_ = [("size", ct.c_uint32), ("data", ct.POINTER(ct.c_ubyte))]
    crypt = ct.WinDLL("crypt32", use_last_error=True)
    kernel = ct.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ct.c_void_p]
    kernel.LocalFree.restype = ct.c_void_p
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ct.POINTER(Blob), ct.c_void_p, ct.POINTER(Blob),
                         ct.c_void_p, ct.c_void_p, ct.c_uint32, ct.POINTER(Blob)]
    function.restype = ct.c_int32
    buffer = (ct.c_ubyte * len(data)).from_buffer_copy(data)
    source = Blob(len(data), buffer)
    output = Blob()
    if not function(ct.byref(source), None, None, None, None, 1, ct.byref(output)):
        raise OSError("Windows не удалось защитить или прочитать сохранённое подключение.")
    try:
        return ct.string_at(output.data, output.size)
    finally:
        kernel.LocalFree(ct.cast(output.data, ct.c_void_p))


class ConnectionStore:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else profile_directory() / "connection.dat"

    def load(self):
        try:
            data = self.path.read_bytes()
        except FileNotFoundError:
            return None
        if data.startswith(b"DPAPI1\0") and os.name == "nt":
            data = dpapi(data[7:], decrypt=True)
        elif data.startswith(b"JSON1\0") and os.name != "nt":
            if self.path.stat().st_mode & 0o077:
                raise InvalidProfile("Файл подключения должен быть доступен только владельцу (chmod 600).")
            data = data[6:]
        else:
            raise InvalidProfile("Файл подключения повреждён или создан в другой системе.")
        try:
            return validate(json.loads(data.decode("utf-8")))
        except (UnicodeError, ValueError) as error:
            raise InvalidProfile("Не удалось прочитать сохранённое подключение. Введи параметры заново.") from error

    def save(self, profile):
        data = json.dumps(validate(profile), ensure_ascii=False).encode("utf-8")
        data = b"DPAPI1\0" + dpapi(data) if os.name == "nt" else b"JSON1\0" + data
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # mkstemp создаёт уникальный файл с правами 0600, os.replace атомарен.
        fd, temporary = tempfile.mkstemp(prefix=".connection-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def clear(self):
        self.path.unlink(missing_ok=True)
