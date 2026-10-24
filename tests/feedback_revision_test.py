import contextlib
import copy
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import feedback_revision
from feedback_revision import apply_feedback, duration_reference, verify_reviewed_file, live_reference


def example():
    return dict(audio_duration=20, lines=[dict(raw=f'line {i}',start=a,end=a+1,
        tokens=[dict(text=f'line {i}',disp=f'line {i}',start=a,end=a+1)])
        for i,a in enumerate([5,8,12],1)])


class FeedbackRevisionTests(unittest.TestCase):
    def test_live_reprise_keeps_original_and_requires_evidence(self):
        order=[dict(source_row=i) for i in [1,2,3,2]]
        with self.assertRaises(ValueError):live_reference(example(),order)
        order[-1]['reason']='Independent transcript confirms a live reprise'
        self.assertEqual([l['raw']for l in live_reference(example(),order)['lines']],['line 1','line 2','line 3','line 2'])
        with self.assertRaises(ValueError):live_reference(example(),order[:2])

    def test_omit_and_trim_preserve_original_identity(self):
        source=example();before=copy.deepcopy(source)
        result=apply_feedback(source,start=4,end=15,
            omissions=[dict(row=2,text='line 2',reason='Reviewer confirmed absent')])
        self.assertEqual(source,before)
        self.assertEqual([l['start'] for l in result['lines']],[1,8])
        self.assertEqual([r['row'] for r in result['feedback']['rows']],[1,None,2])
        self.assertFalse(result['feedback']['human_approved'])

    def test_cannot_silently_cut_sung_tokens_or_use_wrong_row(self):
        with self.assertRaises(ValueError):apply_feedback(example(),end=12.5)
        with self.assertRaises(ValueError):apply_feedback(example(),start=6)
        with self.assertRaises(ValueError):apply_feedback(example(),omissions=[dict(row=2,text='wrong',reason='confirmed')])
        with self.assertRaises(ValueError):apply_feedback(example(),omissions=[dict(row=2,text='line 2',reason='')])

    def test_extension_requires_available_source(self):
        with self.assertRaises(ValueError):apply_feedback(example(),end=24)
        result=apply_feedback(example(),end=24,available_duration=30)
        self.assertEqual(result['audio_duration'],24)

    def test_crop_quantization_overlap_ends_at_next_measured_onset(self):
        source=example();source['lines'][0]['tokens'][0]['end']=8.01
        result=apply_feedback(source)
        self.assertEqual(result['lines'][0]['end'],8)
        self.assertEqual(result['lines'][1]['start'],8)
        self.assertEqual(source['lines'][0]['tokens'][0]['end'],8.01)

    def test_measured_replacement_cannot_change_text_or_overlap(self):
        line=copy.deepcopy(example()['lines'][1]);line['tokens'][0].update(start=4,end=7)
        with self.assertRaises(ValueError):apply_feedback(example(),replacements=[dict(row=2,line=line)])
        line['raw']='a different verse'
        with self.assertRaises(ValueError):apply_feedback(example(),replacements=[dict(row=2,line=line)])

    def test_standard_duration_does_not_authorize_cut(self):
        reference=duration_reference(731,260,edition='studio',url='https://example.org/album')
        self.assertTrue(reference['advisory_only']);self.assertFalse(reference['automatic_trim'])

    def test_quarantine_retains_unresolved_reference_without_claiming_omission(self):
        result=apply_feedback(example(),quarantine=[dict(row=2,text='line 2',reason='Both routes failed to locate it')])
        self.assertTrue(result['feedback']['requires_reference_review'])
        self.assertEqual(result['feedback']['unresolved_rows'][0]['line']['raw'],'line 2')
        self.assertEqual(result['feedback']['rows'][1]['disposition'],'unresolved')
        self.assertFalse(result['feedback']['human_approved'])
        from repair_validation import validate_render
        result['options']={}
        validation=validate_render(result,[])
        self.assertFalse(validation['gate'])
        self.assertEqual(validation['unresolved_source_rows'],[2])

    def test_stale_review_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'sample.mp4';path.write_bytes(b'original')
            st=path.stat();review=dict(file_path=str(path),file_signature=f'{st.st_size}:{st.st_mtime_ns}')
            self.assertEqual(verify_reviewed_file(review),path.resolve())
            path.write_bytes(b'changed')
            with self.assertRaises(ValueError):verify_reviewed_file(review)

    def prepare_cli(self,folder):
        root=Path(folder);source=root/'input';source.mkdir()
        (source/'align.json').write_text(json.dumps(example(),ensure_ascii=False),encoding='utf8')
        return source,root/'output'

    def run_cli(self,source,decisions,target):
        (source/'decisions.json').write_text(json.dumps(decisions,ensure_ascii=False),encoding='utf8')
        out,err=io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
            code=feedback_revision.main(['--align',str(source/'align.json'),
                                         '--decisions',str(source/'decisions.json'),
                                         '--output',str(target)])
        return code,out.getvalue(),err.getvalue()

    def test_cli_writes_revised_alignment_and_feedback(self):
        decisions=dict(start=4,end=15,omissions=[dict(row=2,text='line 2',reason='Reviewer confirmed absent')])
        with tempfile.TemporaryDirectory() as folder:
            source,target=self.prepare_cli(folder)
            code,out,err=self.run_cli(source,decisions,target)
            self.assertEqual(code,0,err)
            revised=json.loads((target/'align.json').read_text(encoding='utf8'))
            record=json.loads((target/'feedback.json').read_text(encoding='utf8'))
            self.assertEqual([l['start'] for l in revised['lines']],[1,8])
            self.assertEqual([r['row'] for r in record['rows']],[1,None,2])
            self.assertEqual(record,revised['feedback'])
            summary=json.loads(out)
            self.assertEqual(summary['lines'],2)
            self.assertEqual(summary['unresolved_rows'],[])
            self.assertFalse(summary['requires_reference_review'])
            self.assertFalse(summary['human_approved'])

    def test_cli_rejects_stale_review_without_writing_output(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);media=root/'reviewed.mp4';media.write_bytes(b'original')
            st=media.stat();review=dict(file_path=str(media),file_signature=f'{st.st_size}:{st.st_mtime_ns}')
            source,target=self.prepare_cli(folder)
            media.write_bytes(b'changed after review')
            code,out,err=self.run_cli(source,dict(review=review,start=0),target)
            self.assertEqual(code,1)
            self.assertTrue(err.strip())
            self.assertFalse((target/'align.json').exists())

    def test_cli_rejects_invalid_omission_without_writing_output(self):
        with tempfile.TemporaryDirectory() as folder:
            source,target=self.prepare_cli(folder)
            code,out,err=self.run_cli(source,dict(start=0,
                omissions=[dict(row=2,text='wrong text',reason='Reviewer confirmed absent')]),target)
            self.assertEqual(code,1)
            self.assertTrue(err.strip())
            self.assertFalse((target/'align.json').exists())


if __name__=='__main__':unittest.main()
