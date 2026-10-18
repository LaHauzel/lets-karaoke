import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from repair_triage import review_queue


class TriageTests(unittest.TestCase):
    def test_compacted_unanchored_passage_is_one_structure_check(self):
        rows=[dict(row=i,text='Some possible lyric words',start=i,end=i+.1,score=.1,reasons=['collapsed_line'])for i in range(1,4)]
        report=dict(after=dict(rows=rows,blocking_rows=[1,2,3],duration_rows=[]),anchor_rows=[])
        queue=review_queue(report)
        self.assertEqual(len(queue),1)
        self.assertEqual(queue[0]['kind'],'reference_structure')
        self.assertEqual(queue[0]['lyrics'],[r['text']for r in rows])
        self.assertIn('不能',queue[0]['action'])

    def test_unmatched_lyrics_alone_never_imply_structure_failure(self):
        rows=[dict(row=i,text='Some lyric words',start=i*10,end=i*10+5,score=.1,reasons=['low_score'])for i in range(1,4)]
        report=dict(after=dict(rows=rows,blocking_rows=[],duration_rows=[]),anchor_rows=[])
        self.assertEqual(review_queue(report),[])


if __name__=='__main__':unittest.main()
