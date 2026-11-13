"""Generate release-description notes from the exact standalone ZIP being published.

Read the embedded BUILD_INFO.json rather than resolving dependencies again: this
guarantees that the displayed versions match those included in the release asset.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from zipfile import BadZipFile, ZipFile


BUILD_INFO_PATH = "cyberwall_master_manager/BUILD_INFO.json"
TAG_PATTERN = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+\Z")


def library_release_notes(archive_path: Path, release_tag: str) -> str:
    """Return Markdown description of libraries actually packed in archive_path."""
    if not TAG_PATTERN.fullmatch(release_tag):
        raise ValueError(f"Ожидается тег релиза vX.Y.Z, получено {release_tag!r}")

    try:
        with ZipFile(archive_path) as archive:
            info = json.loads(archive.read(BUILD_INFO_PATH))
    except (BadZipFile, KeyError, json.JSONDecodeError) as error:
        raise ValueError(
            f"В ZIP отсутствует корректный {BUILD_INFO_PATH}: {archive_path}"
        ) from error

    if not isinstance(info, dict):
        raise ValueError("BUILD_INFO.json должен содержать объект с версиями")

    if info.get("version") != release_tag[1:]:
        raise ValueError(
            f"Версия Master Manager в ZIP ({info.get('version')!r}) "
            f"не совпадает с тегом {release_tag}"
        )

    versions = {}
    for key in ("reader_version", "printer_version"):
        version = info.get(key)
        if not isinstance(version, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){2}(?:[a-zA-Z0-9.+-]*)?", version):
            raise ValueError(f"BUILD_INFO.json: некорректное или отсутствующее поле {key}")
        versions[key] = version

    return (
        "## Библиотеки в этой сборке\n\n"
        f"- **Считыватель:** `wcopy_reader` v{versions['reader_version']}\n"
        f"- **Принтер:** `cyberwall_id_printer` v{versions['printer_version']}\n\n"
        "Версии считаны из `BUILD_INFO.json` внутри опубликованного ZIP-архива.\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    notes = library_release_notes(args.archive, args.tag)
    args.output.write_text(notes, encoding="utf-8")
    print(notes)


if __name__ == "__main__":
    main()
