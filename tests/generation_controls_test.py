"""Exercise generation capability controls against the actual UI functions."""
from pathlib import Path
import shutil
import subprocess
import unittest


class GenerationControlsTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node.js is unavailable')
    def test_capability_lock_recovery_and_existing_history_editing(self):
        result = subprocess.run(
            ['node', str(Path(__file__).with_suffix('.js'))], capture_output=True,
            text=True, encoding='utf-8', timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count('PASS '), 9, result.stdout)


if __name__ == '__main__':
    unittest.main()
