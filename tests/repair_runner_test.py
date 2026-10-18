import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from repair_runner import run_worker


class WorkerTests(unittest.TestCase):
    def test_completed_worker_returns(self):
        with tempfile.TemporaryDirectory()as directory:
            run_worker([sys.executable,'-c','pass'],Path(directory)/'progress.json',2,.05)

    def test_stalled_worker_is_terminated_and_reported(self):
        with tempfile.TemporaryDirectory()as directory:
            with self.assertRaises(TimeoutError):
                run_worker([sys.executable,'-c','import time; time.sleep(10)'],Path(directory)/'progress.json',.2,.05)


if __name__=='__main__':unittest.main()
