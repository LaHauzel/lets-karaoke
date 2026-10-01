"""Real cancellation/deadline checks and preservation of existing outputs."""
import os
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from process_runner import ProcessCancelled, run_process
from pipeline import extract_wav, render_video


def process_active(pid):
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        status = wintypes.DWORD()
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(status)):
            raise ctypes.WinError(ctypes.get_last_error())
        return status.value == 259
    finally:
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle(handle)


class ProcessTests(unittest.TestCase):
    def test_capture_check_and_deadline_are_consistent(self):
        result = run_process([sys.executable, '-c', "import sys; print('ok'); print('err', file=sys.stderr)"], timeout=10, check=True)
        self.assertEqual(result.stdout.strip(), b'ok')
        self.assertEqual(result.stderr.strip(), b'err')
        with self.assertRaises(subprocess.CalledProcessError):
            run_process([sys.executable, '-c', 'raise SystemExit(7)'], timeout=10, check=True)
        with self.assertRaises(subprocess.TimeoutExpired):
            run_process([sys.executable, '-c', 'import time; time.sleep(30)'], timeout=.15)

    def test_cancel_mid_operation_terminates_wait_instead_of_waiting_for_work(self):
        started = time.monotonic()
        with self.assertRaises(ProcessCancelled):
            run_process([sys.executable, '-c', 'import time; time.sleep(30)'],
                        cancel=lambda: time.monotonic() - started >= .15)
        self.assertLess(time.monotonic() - started, 5)

    @unittest.skipUnless(os.name == 'nt', 'Windows worker-tree cancellation')
    def test_cancel_kills_spawned_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = Path(tmp)/'worker.pid'
            code = ('import subprocess,sys,time; from pathlib import Path; '
                    'child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"]); '
                    f'Path({str(pidfile)!r}).write_text(str(child.pid)); time.sleep(30)')
            with self.assertRaises(ProcessCancelled):
                run_process([sys.executable, '-c', code],
                            cancel=lambda: pidfile.exists() and pidfile.read_text().isdigit(), timeout=10)
            pid = int(pidfile.read_text())
            self.assertFalse(process_active(pid), 'The spawned worker must no longer be active')

    @unittest.skipUnless(os.name == 'nt' and importlib.util.find_spec('win32job'), 'Windows Job Object dependency')
    def test_abrupt_owner_exit_reclaims_running_worker_without_finally(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            pidfile, bound = folder/'worker.pid', folder/'bound.txt'
            child = (f'import os,time; from pathlib import Path; '
                     f'Path({str(pidfile)!r}).write_text(str(os.getpid())); time.sleep(30)')
            source = str(Path(__file__).resolve().parents[1] / 'src')
            code = (f'import sys; sys.path.insert(0,{source!r})\n'
                    'import process_runner; from pathlib import Path\n'
                    'original = process_runner.attach_kill_on_close_job\n'
                    'def attach(process):\n'
                    '    job = original(process)\n'
                    f'    Path({str(bound)!r}).write_text("yes" if job else "no")\n'
                    '    return job\n'
                    'process_runner.attach_kill_on_close_job = attach\n'
                    f'process_runner.run_process([sys.executable,"-c",{child!r}],timeout=60)\n')
            owner = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
            worker_pid = None
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if bound.exists() and bound.read_text() and pidfile.exists() and pidfile.read_text().isdigit():
                        break
                    time.sleep(.02)
                self.assertTrue(pidfile.exists(), 'Worker must start before the simulated server crash')
                worker_pid = int(pidfile.read_text())
                self.assertEqual(bound.read_text(), 'yes', 'Job Object must bind before this test can validate abrupt exit')
                owner.kill(); owner.wait(timeout=5)
                deadline = time.monotonic() + 3
                while process_active(worker_pid) and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertFalse(process_active(worker_pid), 'An abrupt server exit must not leave its worker alive')
            finally:
                if owner.poll() is None:
                    owner.kill(); owner.wait(timeout=5)
                if worker_pid is not None and process_active(worker_pid):
                    subprocess.run(['taskkill','/PID',str(worker_pid),'/T','/F'],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW, timeout=10, check=False)


class OutputTests(unittest.TestCase):
    def test_cancelled_extract_preserves_prior_wav_and_removes_partial_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)/'audio.wav'; output.write_bytes(b'previous complete wav')
            def interrupted(cmd, **kwargs):
                Path(cmd[-1]).write_bytes(b'partial')
                raise ProcessCancelled('cancelled')
            with patch('pipeline.run_process', side_effect=interrupted):
                with self.assertRaises(ProcessCancelled):
                    extract_wav('unused', output)
            self.assertEqual(output.read_bytes(), b'previous complete wav')
            self.assertEqual(list(Path(tmp).iterdir()), [output])

    def test_cancelled_render_preserves_prior_video_and_never_starts_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)/'result.mp4'; output.write_bytes(b'previous complete video')
            with patch('pipeline.run_process', side_effect=ProcessCancelled('cancelled')) as process:
                with self.assertRaises(ProcessCancelled):
                    render_video('unused', 'test.ass', Path(tmp), output, None, {'has_video':True})
            self.assertEqual(output.read_bytes(), b'previous complete video')
            self.assertEqual(process.call_count, 1)
            self.assertEqual(list(Path(tmp).iterdir()), [output])

    def test_encoder_fallback_publishes_only_the_completed_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)/'result.mp4'; output.write_bytes(b'old')
            attempts = []
            def encode(cmd, **kwargs):
                attempts.append(cmd[cmd.index('-c:v') + 1])
                self.assertEqual(output.read_bytes(), b'old')
                Path(cmd[-1]).write_bytes(b'new complete video')
                return subprocess.CompletedProcess(cmd, 1 if len(attempts)==1 else 0, b'', b'nvenc unavailable')
            with patch('pipeline.run_process', side_effect=encode):
                encoder = render_video('unused', 'test.ass', Path(tmp), output, None, {'has_video':True})
            self.assertEqual(encoder, 'x264')
            self.assertEqual(attempts, ['h264_nvenc', 'libx264'])
            self.assertEqual(output.read_bytes(), b'new complete video')
            self.assertEqual(list(Path(tmp).iterdir()), [output])


if __name__ == '__main__':
    unittest.main()
