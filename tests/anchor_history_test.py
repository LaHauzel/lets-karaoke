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
    def test_suffix_is_offset_and_prefix_untouched(self):
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'audio.wav';source.touch()
            lines=[KaraokeLine(raw=c, start=i*2, end=i*2+1, tokens=[KaraokeToken(c,c,i*2,i*2+1)]) for i,c in enumerate('abc')]
            before=copy.deepcopy(lines[:2])
            with patch('anchor_realign.subprocess.run'), patch('whisper_align.align_words',return_value=[['c',.5,1.5,.9]]) as align:
                result=realign_suffix({'media':str(source),'media_info':{'duration':20},'lang':'en'},lines,1,lambda *x:None)
            self.assertEqual(result[:2],before)
            self.assertEqual(result[2].start,3.5)
            self.assertEqual(align.call_args.args[1],'c')
            with self.assertRaises(ValueError):realign_suffix({},lines,2,lambda *x:None)

    def test_missing_lyrics_fail_without_silent_drop(self):
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'audio.wav';source.touch()
            lines=[KaraokeLine(raw=c,start=i,end=i+.5,tokens=[KaraokeToken(c,c,i,i+.5)]) for i,c in enumerate('abc')]
            with patch('anchor_realign.subprocess.run'),patch('whisper_align.align_words',return_value=[['b',0,1,.8]]):
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
