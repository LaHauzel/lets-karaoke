"""Run real Windows installers with isolated venvs and harmless local pip stubs."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
LOGGER = '''import json, os, sys
from pathlib import Path
with Path(os.environ['SETUP_TEST_LOG']).open('a', encoding='utf-8') as stream:
    stream.write(json.dumps({'python': sys.executable, 'argv': sys.argv}) + '\\n')
'''


@unittest.skipUnless(os.name == 'nt' and sys.version_info[:2] == (3, 11),
                     'Windows Python 3.11 installer regression')
class SetupEnvironmentTests(unittest.TestCase):
    def setUp(self):
        # The root is explicitly under the system temporary directory. All
        # installer effects and cleanup are confined to this disposable tree.
        temporary = tempfile.TemporaryDirectory(prefix='karaoke setup test ')
        self.addCleanup(temporary.cleanup)
        self.fixture = Path(temporary.name).resolve()
        self.assertTrue(self.fixture.is_relative_to(Path(tempfile.gettempdir()).resolve()))
        self.log = self.fixture / 'calls.jsonl'
        for name in ('setup_profile.bat', 'setup_venv.bat', 'setup.bat'):
            shutil.copyfile(ROOT / name, self.fixture / name)
        for profile in ('concert', 'full'):
            (self.fixture / f'requirements-{profile}.txt').write_text('', encoding='utf-8')
        (self.fixture / 'pip').mkdir()
        (self.fixture / 'pip/__init__.py').write_text('', encoding='utf-8')
        (self.fixture / 'pip/__main__.py').write_text(LOGGER, encoding='utf-8')
        (self.fixture / 'src').mkdir()
        for name in ('check_environment.py', 'fetch_concert_model.py'):
            (self.fixture / 'src' / name).write_text(LOGGER, encoding='utf-8')
        # Make the GPU probe fail before it can allocate anything, so the full
        # profile exercises its pip branch using the harmless stub above.
        (self.fixture / 'torch.py').write_text(
            "__version__='2.9.0'\nclass cuda:\n    @staticmethod\n    def is_available(): return False\n",
            encoding='utf-8',
        )
        (self.fixture / 'torchaudio.py').write_text("__version__='2.9.0'\n", encoding='utf-8')
        self.env = os.environ.copy()
        self.env.update(SETUP_TEST_LOG=str(self.log), PYTHONPATH='', PYTHONUTF8='1')
        self.env.pop('PYTHONHOME', None)
        self.system32 = Path(os.environ['SystemRoot']) / 'System32'
        self.env['PATH'] = os.pathsep.join((str(Path(sys.executable).parent), str(self.system32)))

    def make_venv(self):
        result = subprocess.run(
            [sys.executable, '-m', 'venv', '--without-pip', str(self.fixture / '.venv')],
            capture_output=True, text=True, encoding='utf-8', timeout=45,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return self.fixture / '.venv/Scripts/python.exe'

    def hide_base_python(self):
        fake_bin = self.fixture / 'unavailable base commands'
        fake_bin.mkdir()
        # A native executable keeps cmd.exe control flow intact. A python.cmd
        # stand-in would silently transfer control out of the installer.
        for name in ('python.exe', 'py.exe'):
            shutil.copyfile(self.system32 / 'where.exe', fake_bin / name)
        self.env['PATH'] = os.pathsep.join((str(fake_bin), str(self.system32)))

    def run_installer(self, script, profile=None):
        args = [str(self.system32 / 'cmd.exe'), '/d', '/c', 'call', str(self.fixture / script)]
        if profile:
            args.append(profile)
        return subprocess.run(args, cwd=self.fixture, env=self.env, capture_output=True,
                              text=True, encoding='utf-8', errors='replace', timeout=60)

    def assert_installed_in(self, result, expected):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.log.exists(), 'No profile installation/check was invoked')
        calls = [json.loads(line) for line in self.log.read_text(encoding='utf-8').splitlines()]
        self.assertGreaterEqual(len(calls), 3)
        for call in calls:
            self.assertEqual(os.path.normcase(os.path.abspath(call['python'])),
                             os.path.normcase(os.path.abspath(expected)), call)
        self.assertTrue(any(call['argv'][1:2] == ['install'] for call in calls), calls)
        self.assertTrue(any('check_environment.py' in call['argv'][0] for call in calls), calls)

    def test_direct_profile_prefers_existing_project_venv(self):
        expected = self.make_venv()
        self.assert_installed_in(self.run_installer('setup_profile.bat', 'concert'), expected)

    def test_compatibility_full_installer_prefers_existing_project_venv(self):
        expected = self.make_venv()
        self.assert_installed_in(self.run_installer('setup.bat'), expected)

    def test_existing_valid_venv_works_without_base_python_on_path(self):
        expected = self.make_venv()
        self.hide_base_python()
        self.assert_installed_in(self.run_installer('setup_venv.bat', 'concert'), expected)

    def test_new_venv_is_created_and_used_for_profile(self):
        # ensurepip only writes inside this temporary environment. Dependency
        # installation then resolves the fake local pip module, never the network.
        expected = self.fixture / '.venv/Scripts/python.exe'
        self.assert_installed_in(self.run_installer('setup_venv.bat', 'concert'), expected)
        self.assertTrue(expected.exists())

    def test_invalid_existing_venv_is_refused_before_installation(self):
        interpreter = self.make_venv()
        shutil.copyfile(self.system32 / 'where.exe', interpreter)
        result = self.run_installer('setup_venv.bat', 'concert')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('existing .venv was not created with Python 3.11', result.stdout)
        self.assertFalse(self.log.exists())


if __name__ == '__main__':
    unittest.main()
