import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from alignment_diagnostics import attach, explain, snapshot
from asr_lyrics import AsrLine, Segment

class DiagnosticsTest(unittest.TestCase):
    def test_missing_is_not_zero(self):
        line=AsrLine('abc',1,2,[Segment('abc',1,2,.8)],.8)
        primary=snapshot([line],[0])
        d=explain([line],primary,{}, {id(line):(1,2)}, {id(line):0}, [])
        self.assertIsNone(d['lines'][0]['dual_disagreement_s'])
        self.assertIsNone(d['lines'][0]['outside_vocal_proxy'])

    def test_stale_and_repeated_text(self):
        ev={'lines':[{'text':'same','start':1,'end':2},{'text':'same','start':3,'end':4}]}
        aligned={'lines':[{'raw':'same','start':1,'end':2},{'raw':'same','start':3,'end':4}]}
        self.assertEqual(len(attach(aligned,ev)['diagnostics']['lines']),2)
        aligned['lines'][1]['start']=3.5
        diagnostic=attach(aligned,ev)['diagnostics']
        self.assertEqual(len(diagnostic['lines']),2)
        self.assertEqual(diagnostic['source'],'structural_only')
        self.assertIsNone(diagnostic['lines'][1]['confidence'])

    def test_small_manual_edit_also_invalidates(self):
        ev={'lines':[{'text':'a','start':1,'end':2}]}
        aligned={'lines':[{'raw':'a','start':1.02,'end':2.02}],'token_offsets':{'0:0':20}}
        diagnostic=attach(aligned,ev)['diagnostics']
        self.assertEqual(len(diagnostic['lines']),1)
        self.assertIsNone(diagnostic['lines'][0]['confidence'])

    def test_missing_evidence_keeps_rows_and_finds_overlap(self):
        aligned={'lines':[{'raw':'a','start':1,'end':2}, {'raw':'b','start':1.5,'end':1.6}]}
        d=attach(aligned,{})['diagnostics']
        self.assertEqual(len(d['lines']),2)
        self.assertIn('与上一句时间重叠',d['lines'][1]['reasons'])
        self.assertIn('句子显示时间不足0.3秒',d['lines'][1]['reasons'])

    def test_one_changed_boundary_does_not_discard_all_evidence(self):
        ev={'lines':[{'text':'a','start':1,'end':2,'confidence':.4,'reasons':['模型置信偏低']}, {'text':'b','start':3,'end':4,'confidence':.9}]}
        aligned={'lines':[{'raw':'a','start':1,'end':2},{'raw':'b','start':3,'end':4.2}]}
        d=attach(aligned,ev)['diagnostics']
        self.assertEqual(d['source'],'mixed')
        self.assertEqual(d['lines'][0]['confidence'],.4)
        self.assertIsNone(d['lines'][1]['confidence'])

    def test_expected_input_rows_reports_unaligned_lyrics(self):
        line=AsrLine('a',1,2,[Segment('a',1,2,.8)],.8)
        d=explain([line],snapshot([line],[0]),{}, {id(line):(1,2)}, {id(line):0}, [], 3)
        self.assertEqual([x['row'] for x in d['missing_input_rows']],[2,3])

if __name__=='__main__':unittest.main()
