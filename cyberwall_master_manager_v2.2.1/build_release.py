#!/usr/bin/env python3
"""Собрать переносимый исходный комплект из этой программы и отдельной библиотеки."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import tomllib
import zipfile

APP_DIR = Path(__file__).resolve().parent
APP_NAME = "cyberwall_master_manager"
READER_NAME = "wcopy_reader_package"
APP_FILES = ("cyberwall_master_manager.py", "config.toml", "start.bat", "README.md", "build_release.py")


def build(reader, output):
    reader = reader.resolve()
    if not (reader / "wcopy_reader.py").is_file() or not (reader / "requirements.txt").is_file():
        raise ValueError("Укажи папку библиотеки с wcopy_reader.py и requirements.txt.")
    if reader == APP_DIR or reader.is_relative_to(APP_DIR):
        raise ValueError("Библиотека должна храниться отдельно от папки программы.")
    files = {}
    for name in APP_FILES:
        files[f"{APP_NAME}/{name}"] = (APP_DIR / name).read_bytes()
    # Переносимая сборка всегда использует соседнюю библиотеку и локальный журнал.
    # Адрес сервера и остальные настройки сохраняются из config.toml программы.
    config_text = files[f"{APP_NAME}/config.toml"].decode("utf-8")
    for section, key, value in (("reader", "library_path", "../wcopy_reader_package"),
                                ("state", "pending_id_file", "pending_id_change.json")):
        pattern = rf'(?ms)(^\[{section}\]\s*\n(?:(?!^\[).)*?^{key}\s*=)[^\n]*'
        config_text, count = re.subn(pattern, lambda match: match[1] + " " + json.dumps(value), config_text)
        if count != 1:
            raise ValueError(f"Не удалось настроить [{section}] {key} в config.toml для сборки.")
    tomllib.loads(config_text)
    files[f"{APP_NAME}/config.toml"] = config_text.encode()
    for folder in ("modules", "tests"):
        for path in sorted((APP_DIR / folder).rglob("*.py")):
            files[f"{APP_NAME}/{path.relative_to(APP_DIR).as_posix()}"] = path.read_bytes()
    for path in sorted(reader.rglob("*")):
        relative = path.relative_to(reader)
        if path.is_symlink():
            raise ValueError(f"Символьные ссылки не включаются в сборку: {relative}")
        if not path.is_file() or any(part.startswith(".") or part == "__pycache__" for part in relative.parts):
            continue
        if path.suffix.lower() in (".pyc", ".pyo", ".log", ".zip"):
            continue
        files[f"{READER_NAME}/{relative.as_posix()}"] = path.read_bytes()
    # Один установочный файл у программы. Оригинальные файлы библиотеки не изменяем.
    reader_requirements = (reader / "requirements.txt").read_text(encoding="utf-8-sig")
    requirements = []
    for line in reader_requirements.splitlines():
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        if value.startswith(("-", ".", "/")) or "://" in value or " @ " in value or "\\" in value:
            raise ValueError("requirements библиотеки содержит локальные ссылки или параметры pip. Обнови сборщик для этого формата.")
        requirements.append(value)
    files[f"{APP_NAME}/requirements.txt"] = ("# Единая установка; сформировано build_release.py.\nPyMySQL[rsa]>=1.1,<2\n" + "\n".join(requirements) + "\n").encode()
    tree = ast.parse(files[f"{APP_NAME}/cyberwall_master_manager.py"].decode())
    version = next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == "VERSION" for target in node.targets))
    reader_tree = ast.parse(files[f"{READER_NAME}/wcopy_reader.py"].decode("utf-8-sig"))
    reader_version = next((ast.literal_eval(node.value) for node in reader_tree.body if isinstance(node, ast.Assign)
                           and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets)), "unknown")
    manifest = {"application": APP_NAME, "version": version,
                "reader_version": reader_version,
                "reader_source_folder": reader.name,
                "sha256": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())}}
    files["BUILD_MANIFEST.json"] = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
    output = output.resolve()
    if output.exists():
        raise ValueError(f"Архив уже существует: {output}. Выбери новое имя.")
    output.parent.mkdir(parents=True, exist_ok=True)
    # Создание исключительно нового файла; архив не содержит pending_id_change.json.
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            archive.writestr(name, data)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reader", type=Path, default=APP_DIR.parent / READER_NAME,
                        help="Папка выбранной версии библиотеки")
    parser.add_argument("--output", type=Path, required=True, help="Имя нового ZIP-архива")
    args = parser.parse_args()
    try:
        print(build(args.reader, args.output))
    except (OSError, ValueError, StopIteration, SyntaxError) as error:
        parser.exit(1, f"Ошибка сборки: {error}\n")


if __name__ == "__main__":
    main()
