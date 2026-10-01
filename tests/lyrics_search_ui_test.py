"""Exercise the lyrics-search UI without a server or live network requests."""
from pathlib import Path
import shutil
import subprocess
import unittest


class LyricsSearchUITests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node.js is unavailable')
    def test_safe_search_preview_and_context_scoped_import(self):
        result = subprocess.run(
            ['node', str(Path(__file__).with_suffix('.js'))], capture_output=True,
            text=True, encoding='utf-8', timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count('PASS '), 24, result.stdout)


if __name__ == '__main__':
    unittest.main()
