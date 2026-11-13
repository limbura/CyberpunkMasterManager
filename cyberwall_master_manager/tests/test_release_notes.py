"""Release notes must describe libraries inside the actual ZIP archive."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from release_notes import BUILD_INFO_PATH, library_release_notes


class ReleaseNotesTests(unittest.TestCase):
    @staticmethod
    def _archive(directory, info):
        path = Path(directory) / "manager.zip"
        with ZipFile(path, "w") as archive:
            if info is not None:
                archive.writestr(BUILD_INFO_PATH, json.dumps(info))
        return path

    def test_versions_come_from_packaged_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            zip_path = self._archive(folder, {
                "version": "2.11.1",
                "reader_version": "1.8.0",
                "printer_version": "0.4.1",
            })
            notes = library_release_notes(zip_path, "v2.11.1")
            self.assertIn("Считыватель:", notes)
            self.assertIn("wcopy_reader` v1.8.0", notes)
            self.assertIn("cyberwall_id_printer` v0.4.1", notes)
            self.assertIn("BUILD_INFO.json", notes)

    def test_mismatched_manager_tag_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            zip_path = self._archive(folder, {
                "version": "2.11.1",
                "reader_version": "1.8.0",
                "printer_version": "0.4.1",
            })
            with self.assertRaisesRegex(ValueError, "не совпадает"):
                library_release_notes(zip_path, "v2.12.0")
            with self.assertRaisesRegex(ValueError, "Ожидается тег"):
                library_release_notes(zip_path, "v2.11.1-rc1")

    def test_missing_or_invalid_component_version_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            zip_path = self._archive(folder, {
                "version": "2.11.1", "reader_version": "1.8.0"
            })
            with self.assertRaisesRegex(ValueError, "printer_version"):
                library_release_notes(zip_path, "v2.11.1")
            zip_path = self._archive(folder, {
                "version": "2.11.1",
                "reader_version": "unknown",
                "printer_version": "0.4.1",
            })
            with self.assertRaisesRegex(ValueError, "reader_version"):
                library_release_notes(zip_path, "v2.11.1")

    def test_zip_without_build_info_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            zip_path = self._archive(folder, None)
            with self.assertRaisesRegex(ValueError, "BUILD_INFO.json"):
                library_release_notes(zip_path, "v2.11.1")


if __name__ == "__main__":
    unittest.main()
