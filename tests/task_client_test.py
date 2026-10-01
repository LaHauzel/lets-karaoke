"""Run deterministic browser-task transport tests without a server or GPU."""
import shutil
import subprocess
import unittest
from pathlib import Path


class TaskClientTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node.js is unavailable')
    def test_transport_recovery_and_batch_cancellation(self):
        script = Path(__file__).with_suffix('.js')
        result = subprocess.run(['node', str(script)], capture_output=True, text=True,
                                encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count('PASS '), 11, result.stdout)


if __name__ == '__main__':
    unittest.main()
