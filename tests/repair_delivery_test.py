"""Use a tiny synthetic clip to check gates, stream reuse and version files."""
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from repair_alignment import atomic
from repair_delivery import export_job, run, probe, bilingual_ass, register
from repair_alignment import read
import model_paths
from repair_policy import audit
from ass_builder import AssOptions,KaraokeLine,KaraokeToken


class RepairDeliveryTests(unittest.TestCase):
    def test_trimmed_release_registers_its_own_media_and_absolute_source_offset(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);catalog=root/'out/exports';catalog.mkdir(parents=True)
            result=dict(track='01_Sample',files=[dict(file=str(catalog/'render.mp4'))],supporting=[str(catalog/'align.json')])
            with patch.object(model_paths,'ROOT',root):
                register(result,catalog,'concert','r2',root/'out/original',media=catalog/'trim.mp4',source_start=123,source_path=root/'master.mp4')
                version=read(catalog/'workflow-links.json')['concert/01_Sample']['versions']['r2']
                self.assertEqual(version['media'],'exports/trim.mp4');self.assertEqual(version['source_start'],123)
                before=(catalog/'revisions/manifest.json').read_bytes()
                with self.assertRaises(ValueError):register(result,catalog,'concert','r3',root,media=root/'outside.mp4')
                self.assertEqual(before,(catalog/'revisions/manifest.json').read_bytes())

    def test_register_archives_previous_revision_in_versioned_history(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);catalog=root/'out/exports';catalog.mkdir(parents=True)
            (catalog/'align.json').write_text('{}');(catalog/'render.mp4').write_bytes(b'x')
            result=dict(track='01_Sample',files=[dict(file=str(catalog/'render.mp4'))],
                        supporting=[str(catalog/'align.json')])
            with patch.object(model_paths,'ROOT',root):
                register(result,catalog,'concert','r1',root/'out/original')
                register(result,catalog,'concert','r2',root/'out/original')
                history=read(catalog/'revisions/history.json')
                self.assertEqual(history['schema_version'],1)
                self.assertEqual([row['revision'] for row in history['revisions']],['r1'])
                self.assertEqual([row['revision'] for row in read(catalog/'revisions/manifest.json')],['r2'])
                register(result,catalog,'concert','r2',root/'out/original')
                history=read(catalog/'revisions/history.json')
                self.assertEqual([row['revision'] for row in history['revisions']],['r1'])

    def test_real_soft_mux_cache_and_stale_gate_rejection(self):
        with tempfile.TemporaryDirectory()as temp:
            root=Path(temp);source=root/'original';(source/'in/vg').mkdir(parents=True)
            run(['-f','lavfi','-i','color=size=320x180:rate=25:duration=2','-f','lavfi','-i','sine=frequency=440:duration=2',
                 '-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac','-shortest',source/'in/source.mp4'])
            run(['-f','lavfi','-i','sine=frequency=220:duration=2',source/'in/vg/accompaniment.wav'])
            candidate=root/'01_Sample';candidate.mkdir()
            line=dict(raw='Hello world',start=.4,end=1.5,tokens=[dict(text='Hello world',disp='Hello world',start=.4,end=1.5)])
            evidence=[dict(row=1,text=line['raw'],start=.4,end=1.5,confidence=.8,reasons=[])]
            align=dict(lines=[line],audio_duration=2,lang='en',options={},health=dict(ok=True),diagnostics=dict(lines=evidence))
            atomic(candidate/'align.json',align);atomic(candidate/'alignment_evidence.json',dict(lines=evidence))
            atomic(candidate/'acceptance.json',dict(status='ready_for_spot_check'))
            atomic(candidate/'input_lyrics.json',[line['raw']]);(candidate/'whisper_lyrics.lrc').write_text('[00:00.40]Hello world')
            report=dict(source=str(source),render_gate=True,changed_rows=[1],after=audit([line],evidence,2))
            atomic(candidate/'repair-report.json',report);atomic(root/'translation.json',{})
            result=export_job(candidate,root/'delivery',root/'translation.json',formats=['soft'])
            self.assertEqual(len(result['files']),4);self.assertFalse(result['human_approved'])
            self.assertEqual(len({f['file'] for f in result['files']}),1)
            self.assertEqual(result['files'][3]['multitrack']['audio'],1)
            self.assertEqual(result['files'][3]['multitrack']['subtitle'],1)
            dates=[Path(f['file']).stat().st_mtime_ns for f in result['files']]
            self.assertEqual(probe(result['files'][0]['file'])['streams'][0]['codec_name'],'h264')
            again=export_job(candidate,root/'delivery',root/'translation.json',formats=['soft'])
            self.assertEqual(dates,[Path(f['file']).stat().st_mtime_ns for f in again['files']])
            legacy=export_job(candidate,root/'separate',root/'translation.json',formats=['soft'],soft_layout='separate')
            self.assertEqual(len({f['file'] for f in legacy['files']}),4)
            line['end']=1.9;atomic(candidate/'align.json',align)
            with self.assertRaisesRegex(ValueError,'报告不一致'):export_job(candidate,root/'delivery2',formats=['soft'])

    def test_bilingual_requires_cached_japanese_but_preserves_english(self):
        line=KaraokeLine(raw='Hello world',tokens=[KaraokeToken('Hello world','Hello world',1,3)],start=1,end=3)
        self.assertIn('Hello world',bilingual_ass([line],AssOptions(),{},320,180))
        line.raw='歌う';line.tokens=[KaraokeToken('歌う','歌う',1,3)]
        with self.assertRaisesRegex(ValueError,'缺少已缓存译文'):bilingual_ass([line],AssOptions(),{},320,180)


if __name__=='__main__':unittest.main()
