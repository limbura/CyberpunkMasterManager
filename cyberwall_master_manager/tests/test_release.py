"""Переносимая сборка: пути, содержимое, автономность и обработка ошибок."""
import ast
import contextlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import Mock, patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_release as build
from modules.settings import application_directory, load_settings


class ReleaseTests(unittest.TestCase):
    def test_redirected_windows_log_supports_russian(self):
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding='cp1252')
        with patch.object(sys, 'stdout', stream), patch.object(sys, 'stderr', stream):
            build.configure_output()
            print('Сборка готова')
            stream.flush()
            self.assertEqual(raw.getvalue().decode('utf-8').strip(), 'Сборка готова')

    def test_build_subprocess_utf8_does_not_override_runtime_environment(self):
        with patch.object(subprocess, 'run') as run:
            build.run(['python', '-c', 'pass'])
            self.assertEqual(run.call_args.kwargs['env']['PYTHONUTF8'], '1')
            runtime_env = build.clean_runtime_environment()
            build.run(['app.exe', '--self-test'], env=runtime_env)
            self.assertEqual(run.call_args.kwargs['env'], runtime_env)
            self.assertNotIn('PYTHONUTF8', run.call_args.kwargs['env'])

    def test_reader_default_uses_config_not_working_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = root / 'manager' / 'version'
            reader = root / 'external' / 'reader'
            app.mkdir(parents=True)
            reader.mkdir(parents=True)
            (reader / 'wcopy_reader.py').write_text('__version__="1.4.3"')
            (reader / 'requirements.txt').write_text('hidapi==0.15.0')
            (app / 'config.toml').write_text('[reader]\nlibrary_path="../../external/reader"\n')
            self.assertEqual(build.resolve_reader(app_dir=app), reader.resolve())
            with self.assertRaises(ValueError):
                build.resolve_reader(root / 'missing', app_dir=app)

    def test_frozen_config_is_next_to_executable(self):
        with patch.object(sys, 'frozen', True, create=True), patch.object(sys, 'executable', str(Path.cwd() / 'app.exe')):
            self.assertEqual(application_directory(), Path.cwd())

    def test_portable_config_preserves_values_and_localizes_state(self):
        source = (build.APP_DIR / 'config.toml').read_text(encoding="utf-8").replace('127.0.0.1', '192.168.1.20').replace('"pending_id_change.json"', '"D:/private/pending.json"')
        result = tomllib.loads(build.portable_config(source))
        self.assertEqual(result['database']['host'], '192.168.1.20')
        self.assertEqual(result['reader']['library_path'], 'bundled')
        self.assertEqual(result['state']['pending_id_file'], 'pending_id_change.json')
        self.assertNotIn('D:/private', build.portable_config(source))

    def test_source_staging_omits_tests_settings_and_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            reader = root / 'reader'; reader.mkdir()
            original = b'__version__="1.4.3"\n'
            (reader / 'wcopy_reader.py').write_bytes(original)
            stage = root / 'src'
            build.stage_source(reader, stage)
            self.assertEqual((stage / 'wcopy_reader.py').read_bytes(), original)
            self.assertTrue((stage / 'release_entry.py').is_file())
            for excluded in ('tests', 'config.toml', 'pending_id_change.json', 'start.bat', 'build_release.py'):
                self.assertFalse((stage / excluded).exists(), excluded)

    def test_zip_contains_only_runtime_tree_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); bundle = root / 'runtime'; bundle.mkdir()
            (bundle / 'cyberwall_master_manager.exe').write_bytes(b'MZ-test')
            (bundle / 'config.toml').write_text('[database]')
            internal = bundle / '_internal'; internal.mkdir()
            (internal / 'python313.dll').write_bytes(b'dll-test')
            output = root / 'release.zip'
            build.make_zip(bundle, output)
            with zipfile.ZipFile(output) as z:
                self.assertEqual(set(z.namelist()), {
                    'cyberwall_master_manager/cyberwall_master_manager.exe',
                    'cyberwall_master_manager/config.toml',
                    'cyberwall_master_manager/_internal/python313.dll'})
            original = output.read_bytes()
            with self.assertRaises(ValueError): build.make_zip(bundle, output)
            self.assertEqual(output.read_bytes(), original)

    def test_failed_archive_is_removed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); bundle = root / 'runtime'; bundle.mkdir()
            (bundle / 'app.exe').write_bytes(b'test')
            output = root / 'release.zip'
            with patch.object(zipfile.ZipFile, 'write', side_effect=OSError('disk full')):
                with self.assertRaises(OSError): build.make_zip(bundle, output)
            self.assertFalse(output.exists())

    def test_self_test_has_no_build_python_search_path(self):
        with patch.dict(os.environ, {'PYTHONPATH':'C:/source', 'PYTHONHOME':'C:/python', 'VIRTUAL_ENV':'C:/venv', 'PATH':'C:/python;C:/venv'}):
            env = build.clean_runtime_environment()
        self.assertNotIn('PYTHONPATH', env)
        self.assertNotIn('PYTHONHOME', env)
        self.assertNotIn('VIRTUAL_ENV', env)
        self.assertNotIn('C:/python', env['PATH'])

    def test_non_windows_fails_before_install_or_output(self):
        with patch.object(sys, 'platform', 'linux'), patch.object(build, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'Windows'):
                build.build()
        run.assert_not_called()

    def test_missing_reader_still_accepts_keyboard(self):
        with tempfile.TemporaryDirectory() as temp:
            app = Path(temp) / 'app'
            shutil.copytree(build.APP_DIR / 'modules', app / 'modules', ignore=shutil.ignore_patterns('__pycache__'))
            shutil.copy2(build.APP_DIR / 'config.toml', app / 'config.toml')
            code = "import sys; sys.path.insert(0, sys.argv[1]); from modules.card_console import CardConsole; c=CardConsole(); assert not c.enabled; assert c.read('Search: ', allow_card=True)=='Test'; c.close()"
            result = subprocess.run([sys.executable, '-c', code, str(app)], input='Test\n', text=True, capture_output=True, cwd=temp)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_config_validates_invalid_page_size(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'config.toml'
            path.write_text((build.APP_DIR / 'config.toml').read_text(encoding="utf-8").replace('page_size = 15', 'page_size = 0'), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, 'page_size'): load_settings(path)

    def test_release_self_test_failure_does_not_pause(self):
        import release_entry
        with patch.object(sys, 'argv', ['app.exe', '--self-test']), patch.object(release_entry, 'self_test', side_effect=ImportError('missing hid')), patch('builtins.input') as prompt, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(release_entry.main(), 1)
        prompt.assert_not_called()


if __name__ == '__main__':
    unittest.main()
