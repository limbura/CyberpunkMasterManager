"""Границы переносимой сборки и независимость RFID-библиотеки."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_release


class ReleaseTests(unittest.TestCase):
    def test_archive_preserves_library_excludes_state_and_relocates_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = root / "source"
            shutil.copytree(build_release.APP_DIR, app, ignore=shutil.ignore_patterns("__pycache__"))
            (app / "pending_id_change.json").write_text('{"private_state":true}')
            config = (app / "config.toml").read_text().replace('"../wcopy_reader_package"', '"D:/external/reader"').replace('"pending_id_change.json"', '"D:/state/pending.json"')
            (app / "config.toml").write_text(config)
            reader = root / "reader-original"
            reader.mkdir()
            reader_bytes = b'__version__ = "9.8.7"\n'
            (reader / "wcopy_reader.py").write_bytes(reader_bytes)
            (reader / "requirements.txt").write_text('hidapi==0.15.0\n')
            (reader / "README.md").write_text("original reader docs")
            with patch.object(build_release, "APP_DIR", app):
                output = build_release.build(reader, root / "release.zip")
            with zipfile.ZipFile(output) as z:
                self.assertEqual(z.read("wcopy_reader_package/wcopy_reader.py"), reader_bytes)
                self.assertFalse(any("pending_id_change.json" in name or "__pycache__" in name for name in z.namelist()))
                self.assertNotIn("cyberwall_master_manager/requirements-reader.txt", z.namelist())
                data = tomllib.loads(z.read("cyberwall_master_manager/config.toml").decode())
                self.assertEqual(data["reader"]["library_path"], "../wcopy_reader_package")
                self.assertEqual(data["state"]["pending_id_file"], "pending_id_change.json")
                manifest = json.loads(z.read("BUILD_MANIFEST.json"))
                self.assertEqual(manifest["reader_version"], "9.8.7")
                for name, expected in manifest["sha256"].items():
                    self.assertEqual(hashlib.sha256(z.read(name)).hexdigest(), expected)
            self.assertEqual((app / "config.toml").read_text(), config)

    def test_missing_reader_still_accepts_keyboard(self):
        with tempfile.TemporaryDirectory() as temp:
            app = Path(temp) / "app"
            shutil.copytree(build_release.APP_DIR / "modules", app / "modules", ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copy2(build_release.APP_DIR / "config.toml", app / "config.toml")
            # Отдельный процесс: ни библиотека, ни импорт из текущего тестового процесса недоступны.
            code = "import sys; sys.path.insert(0, sys.argv[1]); from modules.card_console import CardConsole; c=CardConsole(); assert not c.enabled; assert c.read('Search: ', allow_card=True)=='Тест'; c.close()"
            result = subprocess.run([sys.executable, "-c", code, str(app)], input="Тест\n", text=True, capture_output=True, cwd=temp)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_config_validates_invalid_page_size(self):
        from modules.settings import load_settings
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.toml"
            path.write_text((build_release.APP_DIR / "config.toml").read_text().replace("page_size = 15", "page_size = 0"))
            with self.assertRaisesRegex(ValueError, "page_size"):
                load_settings(path)


if __name__ == "__main__":
    unittest.main()
