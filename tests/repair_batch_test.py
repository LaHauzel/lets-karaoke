import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import repair_batch


class RepairBatchTests(unittest.TestCase):
    def setup_run(self, root, numbers=(1,)):
        jobs = root / 'jobs'
        jobs.mkdir()
        for number in numbers:
            job = jobs / f'{number:02}_Sample'
            job.mkdir()
            (job / 'align.json').write_text('{}', encoding='utf8')
        output = root / 'repair'
        argv = ['repair_batch.py', '--jobs-root', str(jobs), '--repair-root', str(output),
                '--catalog-root', str(root / 'catalog'), '--project', 'demo']
        return output, argv

    def test_failed_rerun_never_publishes_a_stale_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            output, argv = self.setup_run(Path(tmp))
            candidate = output / '01_Sample'
            candidate.mkdir(parents=True)
            old = candidate / 'repair-report.json'
            old.write_text('{"render_gate":true}', encoding='utf8')
            repair_batch.atomic(output / 'batch-summary.json', {'status': 'complete'})
            def fail(*args):
                repair_batch.atomic(output / 'failures.json', [{'track': '01_Sample', 'error': 'changed input failed'}])
            with patch.object(sys, 'argv', argv), patch('repair_batch.run_worker', side_effect=fail), \
                    patch('repair_batch.subprocess.run') as publish, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(repair_batch.main(), 1)
            publish.assert_not_called()
            self.assertEqual(json.loads((output / 'batch-summary.json').read_text())['status'], 'failed')
            self.assertEqual(old.read_text(), '{"render_gate":true}')

    def test_unknown_song_selection_does_not_process_old_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            output, argv = self.setup_run(Path(tmp))
            with patch.object(sys, 'argv', argv + ['--only', '9']), patch('repair_batch.run_worker') as worker, \
                    patch('repair_batch.subprocess.run') as publish:
                with self.assertRaisesRegex(ValueError, '没有匹配'):
                    repair_batch.main()
            worker.assert_not_called()
            publish.assert_not_called()

    def test_selection_reaches_preview_and_delivery_and_url_reaches_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            output, argv = self.setup_run(Path(tmp), (1, 2))
            with patch.object(sys, 'argv', argv + ['--only', '2', '--workspace-url', 'http://localhost:9001/']), \
                    patch('repair_batch.run_worker') as worker, patch('repair_batch.subprocess.run') as stage, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(repair_batch.main(), 0)
            self.assertEqual(worker.call_count, 1)
            for call in stage.call_args_list[:2]:
                command = call.args[0]
                self.assertEqual(command[command.index('--tracks') + 1], '02_Sample')
                self.assertNotIn('01_Sample', command)
            self.assertIn('http://localhost:9001/', stage.call_args_list[-1].args[0])


if __name__ == '__main__':
    unittest.main()
