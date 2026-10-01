"""Precisely interleave an old terminal write and a retry without timing sleeps."""
from contextlib import ExitStack
import json
from pathlib import Path
import queue
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import webui


class ObservedRLock:
    """Signal when the retry reaches the lock, before it blocks on acquisition."""
    def __init__(self):
        self.lock = threading.RLock()
        self.attempted = threading.Event()
        self.owner = None
        self.depth = 0

    def __enter__(self):
        if threading.current_thread().name == 'persistence-retry':
            self.attempted.set()
        self.lock.acquire()
        self.owner = threading.get_ident()
        self.depth += 1
        return self

    def __exit__(self, *_):
        self.depth -= 1
        if not self.depth:
            self.owner = None
        self.lock.release()

    def owned_by_current_thread(self):
        return self.owner == threading.get_ident()


class JobPersistenceTests(unittest.TestCase):
    def setUp(self):
        # Prior HTTP tests can leave the shared worker waiting on its queue.
        # Finish and stop it before replacing module globals with this fixture.
        webui.TASK_QUEUE.join()
        webui.STOP_WORKER.set()
        if webui.WORKER is not None:
            webui.WORKER.join(5)
            self.assertFalse(webui.WORKER.is_alive(), 'Previous worker did not stop')
        webui.STOP_WORKER.clear()

    def test_terminal_write_cannot_overwrite_retried_queue_attempt(self):
        for route in ('enqueue', 'resume'):
            with self.subTest(route=route), tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
                root = Path(temp)
                out = root / 'out/webui'
                directory = out / 'persistence-task'
                job = webui.Job('persistence-task', directory, state='cancelled',
                                attempt_id='old-attempt')
                observed = ObservedRLock()
                stack.enter_context(patch.object(webui, 'ROOT', root))
                stack.enter_context(patch.object(webui, 'OUT_ROOT', out))
                stack.enter_context(patch.object(webui, 'JOBS', {job.id: job}))
                stack.enter_context(patch.object(webui, 'LOCK', observed))
                stack.enter_context(patch.object(webui, 'TASK_QUEUE', queue.Queue()))
                # Queue the retry, but do not run any model or actual worker.
                stack.enter_context(patch.object(webui, 'WORKER', SimpleNamespace(is_alive=lambda: True)))
                config = {'media': str(directory / 'fixture.wav')}
                webui.atomic_json(directory / 'task_request.json', {
                    'job': job.id, 'directory': str(directory), 'kind': 'generate',
                    'attempt_id': job.attempt_id, 'config': config,
                })
                webui.persist_job(job)
                captured = threading.Event()
                retried = threading.Event()
                original_atomic = webui.atomic_json
                failures = []
                responses = []

                def delayed_atomic(path, data):
                    if (Path(path).name == 'history_status.json'
                            and data.get('attempt_id') == 'old-attempt'):
                        captured.set()
                        if not observed.attempted.wait(5):
                            raise AssertionError('Retry never reached the persistence lock')
                        # With the old unprotected persist_job, allow the new
                        # queued write to finish before replacing it with this
                        # terminal snapshot. The fixed implementation holds
                        # LOCK, forcing the terminal write to commit first.
                        if not observed.owned_by_current_thread() and not retried.wait(5):
                            raise AssertionError('Unprotected retry did not finish')
                    original_atomic(path, data)

                stack.enter_context(patch.object(webui, 'atomic_json', side_effect=delayed_atomic))

                def terminal_write():
                    try:
                        webui.persist_job(job)
                    except BaseException as error:
                        failures.append(error)

                def retry():
                    try:
                        if route == 'enqueue':
                            webui.enqueue_job(job, config)
                        else:
                            # Exercise the real endpoint dispatch, with an
                            # in-process local request and captured response.
                            handler = SimpleNamespace(
                                path='/api/resume', close_connection=False,
                                headers={'Host': '127.0.0.1:7870', 'Content-Type': 'application/json'},
                                server=SimpleNamespace(server_address=('127.0.0.1', 7870), server_port=7870),
                                _body=lambda: json.dumps({'job': job.id}).encode(),
                                _json=lambda payload, code=200: responses.append((code, payload)),
                            )
                            webui.Handler.do_POST(handler)
                    except BaseException as error:
                        failures.append(error)
                    finally:
                        retried.set()

                writer = threading.Thread(target=terminal_write, name='persistence-terminal')
                resume = threading.Thread(target=retry, name='persistence-retry')
                writer.start()
                self.assertTrue(captured.wait(5), 'Terminal snapshot was never captured')
                resume.start()
                writer.join(10)
                resume.join(10)
                self.assertFalse(writer.is_alive(), 'Terminal writer did not finish')
                self.assertFalse(resume.is_alive(), 'Retry did not finish')
                self.assertFalse(failures, failures)
                if route == 'resume':
                    self.assertEqual(responses[0][0], 202, responses)

                request = json.loads((directory / 'task_request.json').read_text(encoding='utf-8'))
                status = json.loads((directory / 'history_status.json').read_text(encoding='utf-8'))
                self.assertNotEqual(job.attempt_id, 'old-attempt')
                self.assertEqual(job.state, 'queued')
                self.assertEqual(status['state'], 'queued')
                self.assertEqual(request['attempt_id'], job.attempt_id)
                self.assertEqual(status['attempt_id'], job.attempt_id)
                webui.JOBS.clear()
                webui.restore_jobs(start_queued=False)
                restored = webui.JOBS[job.id]
                self.assertEqual(restored.state, 'queued')
                self.assertEqual(restored.attempt_id, job.attempt_id)


if __name__ == '__main__':
    unittest.main()
