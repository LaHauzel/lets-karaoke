import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from pipeline import KaraokeLine, KaraokeToken, RestyleRequest, restyle, load_job
from local_history import identify, resolve, scan
from anchor_realign import realign_suffix


class AnchorHistoryTest(unittest.TestCase):
    def test_realign_preserves_selected_model_device_and_can_cancel_cut(self):
        from process_runner import ProcessCancelled
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp)/'audio.wav'; source.touch()
            lines = [KaraokeLine(raw=c,start=i*2,end=i*2+1,
                tokens=[KaraokeToken(c,c,i*2,i*2+1)]) for i,c in enumerate('ab')]
            job = {'media':str(source),'media_info':{'duration':10},'lang':'en',
                   'alignment_config':{'model':'tiny','device':'cpu'}}
            with patch('anchor_realign.run_process') as cut, \
                    patch('whisper_align.align_words', return_value=[['b',.5,1.5,.9]]) as align:
                realign_suffix(job, lines, 0, lambda *args:None, cancel=lambda:False)
            self.assertEqual(align.call_args.kwargs['model_size'], 'tiny')
            self.assertEqual(align.call_args.kwargs['device'], 'cpu')
            self.assertIsNotNone(cut.call_args.kwargs['cancel'])
            with patch('anchor_realign.run_process') as cut:
                with self.assertRaises(ProcessCancelled):
                    realign_suffix(job, lines, 0, lambda *args:None, cancel=lambda:True)
                cut.assert_not_called()

    def test_suffix_is_offset_and_prefix_untouched(self):
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'audio.wav';source.touch()
            lines=[KaraokeLine(raw=c, start=i*2, end=i*2+1, tokens=[KaraokeToken(c,c,i*2,i*2+1)]) for i,c in enumerate('abc')]
            before=copy.deepcopy(lines[:2])
            with patch('anchor_realign.run_process'), patch('whisper_align.align_words',return_value=[['c',.5,1.5,.9]]) as align:
                result=realign_suffix({'media':str(source),'media_info':{'duration':20},'lang':'en'},lines,1,lambda *x:None)
            self.assertEqual(result[:2],before)
            self.assertEqual(result[2].start,3.5)
            self.assertEqual(align.call_args.args[1],'c')
            with self.assertRaises(ValueError):realign_suffix({},lines,2,lambda *x:None)

    def test_multiple_anchors_bound_each_realign_range(self):
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'audio.wav';source.touch()
            lines=[KaraokeLine(raw=c,start=i*2,end=i*2+1,
                tokens=[KaraokeToken(c,c,i*2,i*2+1)]) for i,c in enumerate('abcde')]
            anchors_before={i:copy.deepcopy(lines[i]) for i in (1,3)}
            def words_for_range(audio,text,**kwargs):
                return [[text,0.5,1.5,.9]]
            with patch('anchor_realign.run_process') as ffmpeg, \
                 patch('whisper_align.align_words',side_effect=words_for_range) as align:
                result=realign_suffix({'media':str(source),'media_info':{'duration':20},'lang':'en'},
                    lines,[1,3],lambda *x:None)
            self.assertEqual(len(align.call_args_list),2)
            self.assertEqual([call.args[1] for call in align.call_args_list],['c','e'])
            self.assertEqual(result[1],anchors_before[1]);self.assertEqual(result[3],anchors_before[3])
            self.assertAlmostEqual(result[2].start,3.5)
            self.assertAlmostEqual(result[4].start,7.5)
            commands=[call.args[0] for call in ffmpeg.call_args_list]
            self.assertEqual(commands[0][commands[0].index('-ss')+1],'3')
            self.assertEqual(commands[0][commands[0].index('-t')+1],'3')
            self.assertEqual(commands[1][commands[1].index('-ss')+1],'7')
            self.assertEqual(float(commands[1][commands[1].index('-t')+1]),13)

    def test_out_of_range_alignment_retries_from_left_anchor_with_right_anchor_context(self):
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'audio.wav';source.touch()
            lines=[KaraokeLine(raw=c,start=i*2,end=i*2+1,
                tokens=[KaraokeToken(c,c,i*2,i*2+1)]) for i,c in enumerate('abcde')]
            anchors_before={i:copy.deepcopy(lines[i]) for i in (1,3)}
            def words_for_range(audio,text,**kwargs):
                if text == 'c':
                    # The clipped attempt puts its final word beyond anchor 4.
                    return [['c',.5,3.5,.9]]
                if text == 'c\nd':
                    return [['c',.5,1.5,.9],['d',2,3,.9]]
                return [['e',.5,1.5,.9]]
            with patch('anchor_realign.run_process') as ffmpeg, \
                 patch('whisper_align.align_words',side_effect=words_for_range) as align:
                result=realign_suffix({'media':str(source),'media_info':{'duration':20},'lang':'en'},
                    lines,[1,3],lambda *x:None)
            self.assertEqual([call.args[1] for call in align.call_args_list],['c','c\nd','e'])
            self.assertEqual(result[1],anchors_before[1]);self.assertEqual(result[3],anchors_before[3])
            self.assertGreaterEqual(result[2].start,lines[1].end)
            self.assertLessEqual(result[2].end,lines[3].start)
            commands=[call.args[0] for call in ffmpeg.call_args_list]
            self.assertEqual(commands[1][commands[1].index('-ss')+1],'3')
            self.assertGreater(float(commands[1][commands[1].index('-t')+1]),3)

    def test_tiny_final_audio_tail_overrun_fits_suffix_without_changing_anchor(self):
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'audio.wav';source.touch()
            lines=[KaraokeLine(raw=c,start=i*2+1,end=i*2+2,
                tokens=[KaraokeToken(c,c,i*2+1,i*2+2)]) for i,c in enumerate('abc')]
            lines[0].end=3
            lines[0].tokens[0].end=3
            anchor_before=copy.deepcopy(lines[0])
            with patch('anchor_realign.run_process'), \
                 patch('whisper_align.align_words',return_value=[['b',1,2,.9],['c',6,7.09,.9]]):
                result=realign_suffix({'media':str(source),'media_info':{'duration':10},'lang':'en'},
                    lines,[0],lambda *x:None)
            self.assertEqual(result[0],anchor_before)
            self.assertGreaterEqual(result[1].start,lines[0].end)
            self.assertLessEqual(result[-1].end,10)
            self.assertAlmostEqual(result[-1].end,10)

    def test_missing_lyrics_fail_without_silent_drop(self):
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'audio.wav';source.touch()
            lines=[KaraokeLine(raw=c,start=i,end=i+.5,tokens=[KaraokeToken(c,c,i,i+.5)]) for i,c in enumerate('abc')]
            with patch('anchor_realign.run_process'),patch('whisper_align.align_words',return_value=[['b',0,1,.8]]):
                with self.assertRaises(ValueError):realign_suffix({'media':str(source),'media_info':{'duration':10}},lines,0,lambda *x:None)

    def test_history_restores_numeric_versions_and_confines_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);directory=root/'benchmark'/'same_name';directory.mkdir(parents=True)
            (directory/'job.json').write_text(json.dumps({'media':'song.mp4'}))
            for v in (0,2,10):
                suffix=f'_v{v}' if v else ''
                (directory/f'align{suffix}.json').write_text('{}')
                (directory/f'same_name_karaoke{suffix}.mp4').touch()
            data=scan(root)['records'][0]
            self.assertEqual([v['version'] for v in data['versions']],[0,2,10])
            self.assertEqual(data['result']['version'],10)
            self.assertEqual(resolve(identify(directory,root),root,root/'webui'),directory.resolve())
            with self.assertRaises(ValueError):resolve('../../escape',root,root/'webui')

if __name__=='__main__':unittest.main()
