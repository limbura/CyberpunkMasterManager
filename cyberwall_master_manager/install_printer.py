"""
Служебный скрипт для запуска приложения через start.bat
Запускается из start.bat и читает из config.toml пути:
    - библиотеки принтера CyberpunkIdPrinter printer.library_path 
	- timmini printer.timini_path
"""

from pathlib import Path
import subprocess
import sys
import tomllib


def main():
    app_dir = Path(__file__).resolve().parent

    with (app_dir / "config.toml").open("rb") as file:
        config = tomllib.load(file)

    printer = config.get("printer", {})

    if not printer.get("enabled", True):
        print("Использование принтера отключено в настройках: установка его зависимостей пропущена.")
        return

    library_path = printer.get("library_path", "").strip()

    if not library_path:
        raise ValueError(
            "Укажи printer.library_path в config.toml — "
            "путь к папке CyberpunkIdPrinter."
        )

    source = Path(library_path).expanduser()
    if not source.is_absolute():
        source = app_dir / source
    source = source.resolve()

    if not (source / "pyproject.toml").is_file():
        raise FileNotFoundError(
            f"Не найден {source / 'pyproject.toml'}. "
            "Проверь printer.library_path в config.toml."
        )

    print(f"Устанавливаем библиотеку принтера: {source}", flush=True)

    subprocess.run(
        [
            sys.executable,
            "-m", "pip",
            "--disable-pip-version-check",
            "install",
            "--no-input",
            "-e", f"{source}[printer]",
        ],
        check=True,
    )


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Ошибка установки библиотеки принтера: {error}", file=sys.stderr)
        sys.exit(1)