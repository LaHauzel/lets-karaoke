"""Make integration success contingent on complete, valid synthetic output."""
import contextlib
import io
import unittest
from tests.e2e_p1 import main, quality_failures


class QualityGateTests(unittest.TestCase):
    def record(self):
        return {'pipeline_ok':True,'output_lines':2,'outputs_exist':True,
            'metrics':{'n_pred':4,'n':4,'skipped':0,'start_p90_ms':200},'health':{'ok':True}}

    def test_good_pipeline_output_passes(self):
        self.assertEqual(quality_failures(self.record(),2,4),[])

    def test_incomplete_mapping_cannot_pass_with_low_timing_error(self):
        record=self.record()
        record['metrics'].update(n=3,n_pred=3,start_p90_ms=10)
        self.assertIn('incomplete_token_mapping',quality_failures(record,2,4))

    def test_structure_and_timing_are_both_required(self):
        record=self.record()
        record['health']['ok']=False
        record['metrics']['start_p90_ms']=251
        self.assertEqual(quality_failures(record,2,4),[
            'invalid_subtitle_structure','start_p90_exceeds_budget'])

    def test_nan_and_missing_files_fail(self):
        record=self.record()
        record['outputs_exist']=False
        record['metrics']['start_p90_ms']=float('nan')
        self.assertIn('missing_output',quality_failures(record,2,4))
        self.assertIn('start_p90_exceeds_budget',quality_failures(record,2,4))

    def test_unmatched_selection_is_an_error(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main(['--only','does-not-exist'])
        self.assertEqual(error.exception.code,2)


if __name__=='__main__':
    unittest.main()
