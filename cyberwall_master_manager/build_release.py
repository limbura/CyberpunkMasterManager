#!/usr/bin/env python3
"""Собрать офлайн-комплект Windows x64: EXE + config.toml + ZIP, без установки на целевом ПК."""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import tomllib
import venv
import zipfile

APP_DIR = Path(__file__).resolve().parent
APP_NAME = "cyberwall_master_manager"
PYINSTALLER_REQUIREMENT = "PyInstaller>=6.16,<7"

USER_README = """CYBERWALL - менеджмент для мастеров
Версия программы: {version}. Библиотека считывателя: {reader_version}.

1. Распакуй ВЕСЬ ZIP в отдельную папку, например в Документы.
2. При необходимости измени адрес БД, порт и пользователя в config.toml.
3. Запусти cyberwall_master_manager.exe. При первом входе введи параметры БД и пароль.
Следующие запуски подключаются автоматически. Для смены сервера, базы или пользователя:
:disconnect — Отключиться от БД (в поиске и меню выбранной записи).
Сохранённое подключение зашифровано Windows для текущего пользователя и хранится в
%LOCALAPPDATA%/CYBERWALL/cyberwall_master_manager/connection.dat. Оно не входит в ZIP.
При отключении сохранённый вход удаляется; новое успешное подключение заменяет его.

Стрелки вверх/вниз вызывают историю текущего поля; Enter подтверждает строку.
История только на время работы. Пароли, подтверждения и пакетный режим без истории.

Установка Python, pip, библиотек и подключение к интернету не требуются.
Нужен доступ к серверу MySQL, например по локальной сети.
Поддерживаемая система: Windows 10/11 x64.

Переноси всю папку целиком: EXE использует служебные файлы из _internal.
Не запускай EXE прямо из ZIP. Папка программы должна разрешать запись.
Настройки, pending_id_change.json и batch_cards.json хранятся рядом с EXE. Используй одну запущенную
копию программы из этой папки. При обновлении распакуй новую версию отдельно и
перенеси свои настройки и рабочие журналы pending_id_change.json / batch_cards.json, если они остались.

USB-считыватель опционален, подключать его можно во время работы.
Закрой nfcPro и другие приложения, работающие со считывателем.
Библиотека считывателя уже встроена; скачивать отдельный репозиторий не нужно.
Параметр reader.library_path в EXE не используется. enabled и остальные настройки
считывателя продолжают работать. Для обновления встроенной библиотеки нужна новая сборка.

Поиск: имя, описание, ID, card_id или часть номера. Enter — все записи.
Карта выбирает аккаунт в поиске. :new — создать аккаунт, :b — назад, :q — выход.
:batch — пакетная запись карт; «продолжить работу» восстанавливает прогресс.
Подпиши наклейку, положи карту и введи ID с наклейки. Дождись проверки и убери карту.
:skip — пропуск, :card N — переход к card_id, :id N — переход к ID, :report — итоги.
Старые журналы batch_cards-*.json — архивы предыдущей работы; текущий — batch_cards.json.
Для записи карты выбери аккаунт, затем пункт 11. На наклейке должен быть его ID.
Смена ID требует обновления наклейки и подтверждения номера.

LICENSES содержит лицензии компонентов. BUILD_INFO.json — версии состава сборки.
EXE упаковывает код программы; такая упаковка не является защитой исходников.
"""

# Выполняется выбранным сборочным Python, чтобы версии/лицензии относились к сборке.
METADATA_SCRIPT = r'''
import importlib.metadata as md
import json
from pathlib import Path
import shutil
import sys
root = Path(sys.argv[1]); root.mkdir(parents=True, exist_ok=True)
versions = {}
for distribution in md.distributions():
    name = distribution.metadata.get('Name', 'unknown')
    if name.lower() in ('pip', 'setuptools', 'pyinstaller', 'pyinstaller-hooks-contrib', 'altgraph', 'pefile', 'pywin32-ctypes', 'packaging'):
        continue
    versions[name] = distribution.version
    for file in distribution.files or ():
        if Path(str(file)).name.lower().startswith(('license', 'copying', 'notice')):
            source = Path(distribution.locate_file(file))
            if source.is_file():
                target = root / name / str(file)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
python_license = Path(sys.base_prefix) / 'LICENSE.txt'
if python_license.is_file():
    shutil.copy2(python_license, root / 'Python-LICENSE.txt')
pyinstaller = md.distribution('pyinstaller')
for file in pyinstaller.files or ():
    if Path(str(file)).name.lower().startswith(('copying', 'license')):
        source = Path(pyinstaller.locate_file(file))
        if source.is_file():
            target = root / 'PyInstaller' / str(file)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
print(json.dumps({'python': sys.version.split()[0],
                  'pyinstaller': pyinstaller.version, 'dependencies': versions}))
'''


def run(command, **kwargs):
    """Аргументы списком: пути с пробелами не проходят через shell."""
    if "env" not in kwargs:
        kwargs["env"] = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    return subprocess.run([str(value) for value in command], check=True, **kwargs)


def configure_output():
    """Русские сообщения работают и в консоли, и в перенаправленном журнале Windows."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")


def read_version(path, variable):
    tree = ast.parse(Path(path).read_text(encoding="utf-8-sig"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == variable for t in node.targets):
            value = ast.literal_eval(node.value)
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", value):
                return value
    raise ValueError(f"Не найдена корректная строка {variable} в {path}.")


def resolve_reader(reader=None, app_dir=None):
    app_dir = Path(app_dir or APP_DIR)
    if reader is None:
        with (app_dir / "config.toml").open("rb") as stream:
            config = tomllib.load(stream)
        value = config.get("reader", {}).get("library_path")
        if not isinstance(value, str) or not value.strip():
            raise ValueError("В config.toml не задан reader.library_path; укажи --reader.")
        reader = app_dir / Path(value).expanduser()
    reader = Path(reader).expanduser().resolve()
    for name in ("wcopy_reader.py", "requirements.txt"):
        if not (reader / name).is_file():
            raise ValueError(f"Не найден {reader / name}. Проверь library_path или --reader.")
    return reader


def portable_config(source):
    """Меняем только пути в копии; остальные пользовательские настройки сохраняются."""
    if "batch_cards_file" not in tomllib.loads(source).get("state", {}):
        source = re.sub(r"(?m)^\[state\]\s*$", '[state]\nbatch_cards_file = "batch_cards.json"', source, count=1)
    for section, key, value in (("reader", "library_path", "bundled"),
                                ("state", "pending_id_file", "pending_id_change.json"),
                                ("state", "batch_cards_file", "batch_cards.json")):
        pattern = rf'(?ms)(^\[{section}\]\s*\n(?:(?!^\[).)*?^{key}\s*=)[^\n]*'
        source, count = re.subn(pattern, lambda match: match[1] + " " + json.dumps(value), source)
        if count != 1:
            raise ValueError(f"Не удалось настроить [{section}] {key} для EXE.")
    tomllib.loads(source)
    return "# Переносимая EXE-сборка: reader.library_path не используется, библиотека встроена.\n" + source


def require_windows():
    if sys.platform != "win32":
        raise ValueError("Windows EXE нужно собирать на Windows. Запусти скрипт там или GitHub Actions Windows portable build.")
    if sys.version_info < (3, 11):
        raise ValueError("Нужен Python 3.11 или новее; рекомендуется Python 3.13 x64.")
    if struct.calcsize("P") != 8 or platform.machine().lower() not in ("amd64", "x86_64"):
        raise ValueError("Эта сборка рассчитана на Windows x64. Используй 64-битный Python x64.")


def stage_source(reader, destination):
    destination.mkdir(parents=True)
    for name in ("cyberwall_master_manager.py", "release_entry.py"):
        shutil.copy2(APP_DIR / name, destination / name)
    for path in (APP_DIR / "modules").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        target = destination / path.relative_to(APP_DIR)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    shutil.copy2(reader / "wcopy_reader.py", destination / "wcopy_reader.py")


def clean_runtime_environment():
    env = os.environ.copy()
    for key in list(env):
        if key.upper().startswith(("PYTHON", "PYINSTALLER", "_PYI")) or key.upper() == "VIRTUAL_ENV":
            env.pop(key, None)
    # Самопроверка не должна находить DLL из сборочного Python через PATH.
    windows = env.get("SystemRoot", r"C:\Windows")
    env["PATH"] = os.pathsep.join((str(Path(windows) / "System32"), windows))
    env["PYTHONNOUSERSITE"] = "1"
    return env


def verify_executable(bundle, cwd):
    exe = bundle / f"{APP_NAME}.exe"
    if not exe.is_file():
        raise ValueError("PyInstaller не создал ожидаемый EXE.")
    result = run([exe, "--self-test"], cwd=cwd, env=clean_runtime_environment(),
                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", timeout=120)
    try:
        report = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as error:
        raise ValueError(f"Самопроверка EXE не вернула результат: {result.stdout}\n{result.stderr}") from error
    if report.get("status") != "ok" or not report.get("frozen"):
        raise ValueError("Самопроверка EXE не подтвердила готовность сборки.")
    if Path(report["config_file"]).resolve() != (bundle / "config.toml").resolve():
        raise ValueError("EXE читает настройки не из переносимой папки.")
    return report


def make_zip(bundle, output):
    output = Path(output)
    if output.exists():
        raise ValueError(f"Архив уже существует: {output}. Укажи новое имя.")
    output.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        handle = output.open("xb")
        created = True
        with handle, zipfile.ZipFile(handle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(bundle.rglob("*")):
                if path.is_file():
                    archive.write(path, (Path(APP_NAME) / path.relative_to(bundle)).as_posix())
    except BaseException:
        if created:
            output.unlink(missing_ok=True)
        raise
    return output


def build(reader=None, output=None, *, no_install=False):
    require_windows()
    reader = resolve_reader(reader)
    version = read_version(APP_DIR / "cyberwall_master_manager.py", "VERSION")
    reader_version = read_version(reader / "wcopy_reader.py", "__version__")
    output = Path(output or APP_DIR.parent / "builds" / f"{APP_NAME}_v{version}_windows_x64.zip").resolve()
    if output.exists():
        raise ValueError(f"Архив уже существует: {output}. Укажи новое имя.")
    config = portable_config((APP_DIR / "config.toml").read_text(encoding="utf-8"))
    print(f"Сборка {version}; библиотека {reader_version}: {reader}", flush=True)
    with tempfile.TemporaryDirectory(prefix="cyberwall-build-") as temp:
        work = Path(temp)
        python = Path(sys.executable)
        if not no_install:
            print("Создаём отдельное сборочное окружение и устанавливаем зависимости...", flush=True)
            env_dir = work / "venv"
            venv.EnvBuilder(with_pip=True).create(env_dir)
            python = env_dir / "Scripts" / "python.exe"
            run([python, "-m", "pip", "--disable-pip-version-check", "install", "--no-input",
                 "--only-binary=:all:", PYINSTALLER_REQUIREMENT,
                 "-r", APP_DIR / "requirements.txt", "-r", reader / "requirements.txt"])
        else:
            print("Используем заранее подготовленное окружение; pip не запускается.", flush=True)
        run([python, "-c", "import PyInstaller, pymysql, hid, cryptography"])
        src = work / "src"
        stage_source(reader, src)
        print("Упаковываем приложение...", flush=True)
        run([python, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--console", "--noupx",
             "--name", APP_NAME, "--distpath", work / "dist", "--workpath", work / "build",
             "--specpath", work, "--paths", src,
             "--hidden-import", "wcopy_reader", "--hidden-import", "hid",
             "--collect-submodules", "pymysql", "--collect-submodules", "cryptography",
             "--collect-binaries", "cryptography", "--collect-data", "cryptography",
             "--copy-metadata", "PyMySQL", "--copy-metadata", "hidapi", "--copy-metadata", "cryptography",
             src / "release_entry.py"], cwd=src)
        bundle = work / "dist" / APP_NAME
        (bundle / "config.toml").write_text(config, encoding="utf-8")
        (bundle / "README.txt").write_text(USER_README.format(version=version, reader_version=reader_version), encoding="utf-8-sig")
        metadata = run([python, "-c", METADATA_SCRIPT, bundle / "LICENSES"],
                       capture_output=True, text=True, encoding="utf-8")
        build_info = json.loads(metadata.stdout)
        build_info.update(application=APP_NAME, version=version, reader_version=reader_version,
                          target="windows-x64", reader_sha256=hashlib.sha256((reader / "wcopy_reader.py").read_bytes()).hexdigest())
        (bundle / "BUILD_INFO.json").write_text(json.dumps(build_info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        # Проверяем перенос в путь с пробелами/кириллицей, вне папки исходников.
        moved = work / "Проверка переноса с пробелами" / APP_NAME
        moved.parent.mkdir()
        shutil.move(str(bundle), str(moved))
        print("Проверяем EXE без Python в PATH, без БД и USB...", flush=True)
        report = verify_executable(moved, work)
        if report["reader_version"] != reader_version or report["application_version"] != version:
            raise ValueError("Версии внутри EXE не совпали с исходниками сборки.")
        make_zip(moved, output)
    print(f"Готово: {output}\nПередай пользователю весь ZIP. Установка Python на его ПК не нужна.")
    return output


def main():
    configure_output()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reader", type=Path, help="Папка библиотеки; по умолчанию reader.library_path из config.toml")
    parser.add_argument("--output", type=Path, help="Новый ZIP; по умолчанию ../builds/<имя>_v<версия>_windows_x64.zip")
    parser.add_argument("--no-install", action="store_true", help="Не устанавливать сборочные зависимости; использовать текущий Python")
    args = parser.parse_args()
    try:
        build(args.reader, args.output, no_install=args.no_install)
    except subprocess.CalledProcessError as error:
        if error.stdout:
            print(error.stdout)
        if error.stderr:
            print(error.stderr, file=sys.stderr)
        parser.exit(1, "Ошибка сборки. Проверь сообщения выше. Для сборки рекомендуется Python 3.13 x64; "
                    "при установке зависимостей нужен интернет. ZIP не считается готовым.\n")
    except (OSError, ValueError, SyntaxError, subprocess.TimeoutExpired) as error:
        parser.exit(1, f"Ошибка сборки: {error}\n")


if __name__ == "__main__":
    main()
