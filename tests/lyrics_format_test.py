"""Check supported lyric formats at the actual browser parser boundary."""
from pathlib import Path
import shutil
import subprocess
import unittest


class LyricsFormatTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node.js is unavailable')
    def test_preview_and_direct_render_input_formats(self):
        result = subprocess.run(['node', str(Path(__file__).with_suffix('.js'))],
                                capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count('PASS '), 5, result.stdout)
