"""Version chaining must preserve prior edits and the original alignment."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from pipeline import RestyleRequest,restyle,load_job

class VersionTest(unittest.TestCase):
    def test_incremental_and_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)
            (p/'job.json').write_text(json.dumps({'job':'test','media':'unused','media_info':{'width':640,'height':360},'options':{}}),encoding='utf-8')
            (p/'align.json').write_text(json.dumps({'lines':[{'raw':'ab','start':1,'end':3,'tokens':[{'text':'a','disp':'a','start':1,'end':1.5},{'text':'b','disp':'b','start':2,'end':3}]}]}),encoding='utf-8')
            with patch('pipeline.render_video',return_value='test'):
                first=restyle(p,RestyleRequest(line_offsets_ms={0:100},overrides={'font_size':55}))
                second=restyle(p,RestyleRequest(base_version=first['version'],token_offsets_ms={'0:0':50}))
            job,lines=load_job(p,second['version'])
            self.assertAlmostEqual(lines[0].tokens[0].start,1.15)
            self.assertAlmostEqual(lines[0].tokens[1].start,2.1)
            self.assertEqual(job['options']['font_size'],55)
            self.assertEqual(load_job(p)[1][0].start,1)

    def test_independent_boundaries_and_anchor_receives_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)
            (p/'job.json').write_text(json.dumps({'job':'test','media':'unused','media_info':{'width':640,'height':360,'duration':10},'options':{}}),encoding='utf-8')
            (p/'align.json').write_text(json.dumps({'lines':[{'raw':'ab','start':1,'end':3,'tokens':[{'text':'a','disp':'a','start':1,'end':1.5},{'text':'b','disp':'b','start':2,'end':3}]}]}),encoding='utf-8')
            with patch('pipeline.render_video',return_value='test'),patch('anchor_realign.realign_suffix',side_effect=lambda job,lines,*args:lines) as anchor:
                r=restyle(p,RestyleRequest(line_bounds={'0':{'start':2,'end':6}},anchor_row=0))
                self.assertEqual(anchor.call_args.args[1][0].end,6)
            lines=load_job(p,r['version'])[1]
            self.assertEqual((lines[0].start,lines[0].end),(2,6))
            self.assertEqual(lines[0].tokens[0].end,3)
            self.assertEqual(lines[0].tokens[1].start,4)
            for start,end in [(3,2),(-1,2),(1,11),(float('nan'),2)]:
                with self.assertRaises(ValueError):restyle(p,RestyleRequest(line_bounds={'0':{'start':start,'end':end}}))

    def test_manual_and_auto_lyric_insertion(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)
            (p/'job.json').write_text(json.dumps({'job':'test','media':'unused','lang':'zh','media_info':{'width':640,'height':360,'duration':10},'options':{}}),encoding='utf-8')
            rows=[{'raw':c,'start':i*2+1,'end':i*2+2,'tokens':[{'text':c,'disp':c,'start':i*2+1,'end':i*2+2}]} for i,c in enumerate('ac')]
            (p/'align.json').write_text(json.dumps({'lines':rows}),encoding='utf-8')
            with patch('pipeline.render_video',return_value='test'):
                manual=restyle(p,RestyleRequest(line_insertion={'after_row':0,'text':'新歌词','mode':'manual','start':2.1,'end':2.9}))
            inserted=load_job(p,manual['version'])[1]
            self.assertEqual([x.raw for x in inserted],['a','新歌词','c'])
            self.assertEqual((inserted[1].start,inserted[1].end),(2.1,2.9))
            self.assertEqual(len(inserted[1].tokens),3)
            with patch('pipeline.render_video',return_value='test'),patch('anchor_realign.realign_suffix',side_effect=lambda job,lines,after,prog:lines) as align:
                restyle(p,RestyleRequest(line_insertion={'after_row':0,'text':'自动','mode':'auto'}))
            self.assertEqual(align.call_args.args[2],0)
            self.assertEqual(align.call_args.args[1][1].raw,'自动')

if __name__=='__main__':unittest.main()
