"""Exercise actual Windows menu dispatch without installing or downloading anything."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


@unittest.skipUnless(os.name == 'nt', 'Windows cmd.exe menu')
class SetupMenuTest(unittest.TestCase):
    def test_each_menu_choice_and_return_to_menu(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / 'setup_guide.bat').read_text(encoding='utf-8')
        # Retain real branching. Supply answers as arguments because SET /P can
        # consume multiple lines at once from redirected stdin on Windows.
        menu, separator, _ = source.partition('\n:profile\n')
        self.assertTrue(separator, 'setup menu handler boundary missing')
        prompt = 'set /p "answer=Choose an option [1-10/Q]: "'
        self.assertIn(prompt, menu)
        menu = menu.replace(prompt, 'set "answer=%~1"\nshift')
        handlers = '\n:profile\necho ROUTE:profile:%~1\nexit /b\n'
        for name in ('download_whisper', 'download_qwen', 'models', 'launch', 'guide'):
            handlers += f'\n:{name}\necho ROUTE:{name}\nexit /b\n'
        handlers += '\n:done\necho ROUTE:quit\nexit /b 0\n'
        expected = ['profile:whisper', 'profile:concert', 'profile:qwen',
                    'profile:sofa', 'profile:full', 'download_whisper',
                    'download_qwen', 'models', 'launch', 'guide']
        with tempfile.TemporaryDirectory() as temp:
            script = Path(temp) / 'menu.bat'
            script.write_text(menu + '\n' + handlers, encoding='utf-8')
            for choice, route in enumerate(expected, 1):
                with self.subTest(choice=choice):
                    result = subprocess.run(
                        ['cmd.exe', '/d', '/c', str(script), str(choice), 'q'],
                        capture_output=True,
                        text=True, encoding='utf-8', errors='replace', timeout=5,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    routes = [line.strip() for line in result.stdout.splitlines()
                              if line.strip().startswith('ROUTE:')]
                    self.assertEqual(routes, [f'ROUTE:{route}', 'ROUTE:quit'])


if __name__ == '__main__':
    unittest.main()
