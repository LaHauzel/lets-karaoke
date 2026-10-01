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
    def test_language_follows_repaired_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            (p/'job.json').write_text(json.dumps({'job':'test','media':'unused','lang':'ja',
                'media_info':{'width':640,'height':360},'options':{}}),encoding='utf-8')
            rows = [{'raw':'Hello','start':1,'end':2,'tokens':[
                {'text':'Hello','disp':'Hello','start':1,'end':2}]}]
            (p/'align.json').write_text(json.dumps({'lines':rows}),encoding='utf-8')
            (p/'align_v1.json').write_text(json.dumps({'lang':'en','lines':rows}),encoding='utf-8')
            (p/'karaoke_v1.ass').write_text('test',encoding='utf-8')
            self.assertEqual(load_job(p,1)[0]['lang'],'en')
            with patch('pipeline.render_video',return_value='test'):
                result = restyle(p,RestyleRequest(base_version=1))
            self.assertEqual(load_job(p,result['version'])[0]['lang'],'en')

    def test_per_line_positions_are_saved_and_rendered_in_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)
            options={'position_x':20,'position_y':30,'line_count':2,
                     'line_positions':[[20,30],[70,65]]}
            (p/'job.json').write_text(json.dumps({'job':'test','media':'unused',
                'media_info':{'width':640,'height':360},'options':options}),encoding='utf-8')
            (p/'align.json').write_text(json.dumps({'lines':[
                {'raw':'ab','start':1,'end':3,'tokens':[
                    {'text':'a','disp':'a','start':1,'end':1.5},
                    {'text':'b','disp':'b','start':2,'end':3}]},
                {'raw':'cd','start':4,'end':6,'tokens':[
                    {'text':'c','disp':'c','start':4,'end':4.5},
                    {'text':'d','disp':'d','start':5,'end':6}]},
            ]}),encoding='utf-8')
            with patch('pipeline.render_video',return_value='test'):
                result=restyle(p,RestyleRequest(overrides={'position_x':25,'position_y':35,
                    'line_count':2,'line_positions':[[25,35],[75,65]]}))
            version,lines=load_job(p,result['version'])
            self.assertEqual(version['options']['line_positions'],[[25,35],[75,65]])
            ass=(p/'karaoke_v1.ass').read_text(encoding='utf-8-sig')
            self.assertIn(r"{\an2\pos(160,126)}",ass)
            self.assertIn(r"{\an2\pos(480,234)}",ass)

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

    def test_multiple_anchor_rows_are_passed_and_saved_in_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)
            rows=[{'raw':c,'start':i*2+1,'end':i*2+2,'tokens':[
                {'text':c,'disp':c,'start':i*2+1,'end':i*2+2}]} for i,c in enumerate('abcd')]
            (p/'job.json').write_text(json.dumps({'job':'test','media':'unused','lang':'en',
                'media_info':{'width':640,'height':360,'duration':20},'options':{}}),encoding='utf-8')
            (p/'align.json').write_text(json.dumps({'lines':rows}),encoding='utf-8')
            with patch('pipeline.render_video',return_value='test'), \
                 patch('anchor_realign.realign_suffix',side_effect=lambda job,lines,*args:lines) as align:
                result=restyle(p,RestyleRequest(anchor_rows=[0,2]))
            self.assertEqual(align.call_args.args[2],[0,2])
            saved=json.loads((p/f"align_v{result['version']}.json").read_text(encoding='utf-8'))
            self.assertEqual(saved['anchor_rows'],[0,2])
            self.assertIsNone(saved['anchor_row'])
            self.assertEqual(saved['operation'],'anchor_realign')
            self.assertEqual(result['anchor_rows'],[0,2])

    def test_restyle_rejects_backwards_line_order_before_render(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)
            rows=[{'raw':c,'start':i*5+10,'end':i*5+12,'tokens':[
                {'text':c,'disp':c,'start':i*5+10,'end':i*5+12}]} for i,c in enumerate('ab')]
            (p/'job.json').write_text(json.dumps({'job':'test','media':'unused','lang':'en',
                'media_info':{'width':640,'height':360,'duration':30},'options':{}}),encoding='utf-8')
            (p/'align.json').write_text(json.dumps({'lines':rows}),encoding='utf-8')
            with patch('pipeline.render_video',return_value='test') as render:
                with self.assertRaisesRegex(ValueError,'时间轴倒退'):
                    restyle(p,RestyleRequest(line_bounds={'1':{'start':8,'end':9}}))
            render.assert_not_called()
            self.assertFalse((p/'align_v1.json').exists())

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

    def test_multiple_deferred_insertions_render_once_at_the_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)
            (p/'job.json').write_text(json.dumps({'job':'test','media':'unused','lang':'zh',
                'media_info':{'width':640,'height':360,'duration':20},'options':{}}),encoding='utf-8')
            rows=[{'raw':c,'start':i*8+1,'end':i*8+2,'tokens':[
                {'text':c,'disp':c,'start':i*8+1,'end':i*8+2}]} for i,c in enumerate('前后')]
            (p/'align.json').write_text(json.dumps({'lines':rows}),encoding='utf-8')
            with patch('pipeline.render_video',return_value='test') as render:
                first=restyle(p,RestyleRequest(preview_only=True,line_insertion={
                    'after_row':0,'text':'新增一','mode':'manual','start':3,'end':4}))
                self.assertTrue(first['preview'])
                self.assertEqual([row['raw'] for row in first['lines']],['前','新增一','后'])
                self.assertFalse((p/'align_v1.json').exists())
                second=restyle(p,RestyleRequest(preview_only=True,line_insertion={
                    'after_row':1,'text':'新增二','mode':'manual','start':5,'end':6}),
                    initial_lines=first['_draft_lines'])
                self.assertEqual([row['raw'] for row in second['lines']],['前','新增一','新增二','后'])
                self.assertFalse((p/'align_v1.json').exists())
                render.assert_not_called()
                result=restyle(p,RestyleRequest(draft_insertions=[
                    {'after_row':0,'text':'新增一','mode':'manual'},
                    {'after_row':1,'text':'新增二','mode':'manual'}]),
                    initial_lines=second['_draft_lines'])
            self.assertEqual(result['version'],1)
            render.assert_called_once()
            self.assertEqual([row.raw for row in load_job(p,1)[1]],['前','新增一','新增二','后'])
            saved=json.loads((p/'align_v1.json').read_text(encoding='utf-8'))
            self.assertEqual(saved['operation'],'insert_deferred')
            self.assertEqual(len(saved['line_insertions']),2)

    def test_lyric_text_edit_preserves_row_range_without_rendering_preview(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)
            (p/'job.json').write_text(json.dumps({'job':'test','media':'unused','lang':'zh',
                'media_info':{'width':640,'height':360,'duration':10},'options':{}}),encoding='utf-8')
            rows=[{'raw':'旧词','start':1,'end':3,'tokens':[
                {'text':'旧','disp':'旧','start':1,'end':2},
                {'text':'词','disp':'词','start':2,'end':3}]}]
            (p/'align.json').write_text(json.dumps({'lines':rows}),encoding='utf-8')
            with patch('pipeline.render_video') as render:
                result=restyle(p,RestyleRequest(preview_only=True,line_texts={0:'全新的歌词'}))
                render.assert_not_called()
            self.assertTrue(result['preview'])
            edited=result['lines'][0]
            self.assertEqual(edited['raw'],'全新的歌词')
            self.assertEqual((edited['start'],edited['end']),(1,3))
            self.assertEqual(len(edited['tokens']),5)
            self.assertEqual(edited['tokens'][0]['start'],1)
            self.assertEqual(edited['tokens'][-1]['end'],3)
            self.assertFalse((p/'align_v1.json').exists())
            with patch('pipeline.render_video',return_value='test') as render:
                final=restyle(p,RestyleRequest(line_texts={0:'全新的歌词'}),initial_lines=result['_draft_lines'])
            render.assert_called_once()
            self.assertEqual(load_job(p,final['version'])[1][0].raw,'全新的歌词')
            saved=json.loads((p/'align_v1.json').read_text(encoding='utf-8'))
            self.assertEqual(saved['line_texts'],{'0':'全新的歌词'})
            self.assertEqual(saved['operation'],'lyric_edit')

if __name__=='__main__':unittest.main()
