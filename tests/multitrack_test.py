"""Real remux, reference retention, selection identity and cleanup boundaries."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from archive_library import migrate, cleanup_plan
from multitrack import command, pack_variants, hashes, probe, selection, timelines, shift_timeline, compare_timelines
from repair_alignment import atomic, read
from review_catalog import build_catalog
from review_store import ReviewStore


class MultitrackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'out/collection/exports'
        self.root.mkdir(parents=True)
        self.paths = {i: self.root / f'v{i}.mkv' for i in range(1, 5)}
        source = self.root / 'source.mp4'
        command(['ffmpeg','-v','error','-y','-f','lavfi','-i','testsrc2=size=160x90:rate=25:duration=2',
                 '-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=2',
                 '-c:v','libx264','-c:a','aac','-shortest',source])
        accompaniment = self.root / 'accompaniment.m4a'
        command(['ffmpeg','-v','error','-y','-f','lavfi','-i','sine=frequency=220:duration=2','-c:a','aac',accompaniment])
        from ass_builder import build_ass, AssOptions, KaraokeLine, KaraokeToken
        for number, text in ((1,'Example one'),(3,'Example two')):
            ass = self.root / f'{number}.ass'
            line = KaraokeLine(raw=text,start=.4,end=1.5,tokens=[KaraokeToken(text,text,.4,1.5)])
            ass.write_text(build_ass([line],AssOptions(),160,90),encoding='utf8')
            command(['ffmpeg','-v','error','-y','-i',source,'-i',ass,'-map','0:v:0','-map','0:a:0',
                     '-map','1:0','-c','copy',self.paths[number]])
            command(['ffmpeg','-v','error','-y','-i',self.paths[number],'-i',accompaniment,
                     '-map','0:v:0','-map','1:a:0','-map','0:s:0','-c','copy',self.paths[number+1]])
        row = dict(project='demo',track='01_Example',quality=dict(total_rows=1,lower_confidence_rows=[],
            higher_confidence_rows=[1],translation_low_rows=[],alignment_median_model_score=.9),
            acceptance=dict(status='ok',label='ok',issues=[]),health=dict(ok=True),
            variants=[dict(file=str(p),variant=v,duration=2) for v,p in self.paths.items()])
        atomic(self.root/'track-manifest.json',[row])

    def tearDown(self):
        self.temp.cleanup()

    def test_packet_content_timestamps_and_default_tracks_survive_mux(self):
        dest = self.root/'archive.mkv'
        result = pack_variants(self.paths,dest)
        self.assertEqual([s['codec_type'] for s in probe(dest)['streams']],['video','audio','audio','subtitle','subtitle'])
        out = hashes(dest)
        for variant,(a,s) in ((1,(0,0)),(2,(1,0)),(3,(0,1)),(4,(1,1))):
            original=hashes(self.paths[variant])
            self.assertEqual((out['v'],out['a'][a],out['s'][s]),(original['v'],original['a'][0],original['s'][0]))
        original_time=timelines(self.paths[2]);delta=result['source_offsets_ms'][2]
        self.assertEqual(timelines(dest)['a'][1]['start_ms'],shift_timeline(original_time['a'],delta)[0]['start_ms'])
        self.assertLessEqual(result['timing_max_error_ms'],1)
        stamp=dest.stat().st_mtime_ns
        pack_variants(self.paths,dest)
        self.assertEqual(stamp,dest.stat().st_mtime_ns)

    def test_migration_keeps_approval_and_counts_shared_file_once(self):
        track=build_catalog(self.root)[0]['tracks'][0];old=track['files'][0]
        store=ReviewStore(self.root.parent/'human-reviews.json')
        store.save(track,dict(status='passed',revision=0,file_id=old['id'],file_signature=old['signature']))
        before=store.path.read_bytes()
        result=migrate(self.root,self.root.parent/'audit',apply=True)
        self.assertFalse(result['failures'])
        self.assertEqual(store.path.read_bytes(),before)
        catalog,allowed=build_catalog(self.root);store.attach(catalog);track=catalog['tracks'][0]
        multi=[f for f in track['files'] if f.get('multitrack')]
        self.assertEqual(len(multi),4);self.assertEqual(len({f['path'] for f in multi}),1)
        self.assertEqual(len({f['id'] for f in multi}),4)
        self.assertEqual(catalog['summary']['files'],2)
        self.assertEqual(catalog['summary']['bytes'],multi[0]['bytes']+old['bytes'])
        self.assertIn(old['id'],allowed);self.assertFalse(track['human_review']['stale'])
        self.assertFalse(any(f['id']==old['id'] for f in multi))
        cleanup=cleanup_plan(self.root);self.assertEqual(len(cleanup),3)
        for item in cleanup:Path(item['path']).unlink()
        after=build_catalog(self.root)[0];self.assertEqual(after['diagnostics'],[])
        store.attach(after);self.assertFalse(after['tracks'][0]['human_review']['stale'])

    def test_incomplete_variant_track_is_skipped_and_reported(self):
        manifest=self.root/'track-manifest.json'
        rows=json.loads(manifest.read_text(encoding='utf8'))
        rows[0]['variants']=rows[0]['variants'][:3]
        atomic(manifest,rows)
        result=migrate(self.root,self.root.parent/'audit',apply=True)
        self.assertEqual(result['processed'],0)
        self.assertEqual(len(result['failures']),1)
        self.assertIn('四个软字幕版本',result['failures'][0]['error'])
        self.assertTrue(all(p.exists() for p in self.paths.values()))
        self.assertFalse((self.root/'multitrack-manifest.json').exists())
        failures=json.loads((self.root.parent/'audit/migration-failures.json').read_text(encoding='utf8'))
        self.assertEqual(len(failures),1)

    def test_failure_retains_originals_and_never_registers_unverified_archive(self):
        with patch('multitrack.validate',side_effect=ValueError('failed validation')):
            result=migrate(self.root,self.root.parent/'audit',apply=True)
        self.assertEqual(len(result['failures']),1)
        self.assertTrue(all(p.exists() for p in self.paths.values()))
        self.assertFalse((self.root/'multitrack-manifest.json').exists())
        self.assertEqual(result['cleanup_files'],0)

    def test_reject_different_subtitle_and_changed_original_before_cleanup(self):
        self.paths[4].write_bytes(self.paths[2].read_bytes())
        with self.assertRaisesRegex(ValueError,'不同字幕'):
            pack_variants(self.paths,self.root/'bad.mkv')
        self.assertFalse((self.root/'bad.mkv').exists())

    def test_workflow_reference_and_late_approval_are_protected(self):
        atomic(self.root/'workflow-links.json',{'demo/01_Example':{'versions':{'baseline':{'media':str(self.paths[4])}}}})
        result=migrate(self.root,self.root.parent/'audit',apply=True)
        self.assertFalse(result['failures'])
        self.assertEqual({Path(r['path']).name for r in cleanup_plan(self.root)},{'v2.mkv','v3.mkv'})
        atomic(self.root.parent/'human-reviews.json.bak',{'history':{'example':[{'file_path':str(self.paths[2])}]}})
        self.assertEqual({Path(r['path']).name for r in cleanup_plan(self.root)},{'v3.mkv'})

    def test_manifest_cannot_redirect_outside_library(self):
        atomic(self.root/'multitrack-manifest.json',{'archives':[{'file':'../outside.mkv','signature':'bad','originals':[]}]})
        with self.assertRaisesRegex(ValueError,'超出成品库'):build_catalog(self.root)

    def test_same_lyric_packets_with_different_styles_cannot_merge(self):
        ass=self.root/'changed-style.ass'
        command(['ffmpeg','-v','error','-y','-i',self.paths[2],'-map','0:s:0','-c','copy',ass])
        text=ass.read_text(encoding='utf-8-sig')
        text=text.replace('&H00FFFFFF','&H0000FF00')
        ass.write_text(text,encoding='utf8')
        updated=self.root/'changed.mkv'
        command(['ffmpeg','-v','error','-y','-i',self.paths[2],'-i',ass,'-map','0:v:0','-map','0:a:0',
                 '-map','1:s:0','-c','copy',updated])
        self.assertEqual(hashes(updated),hashes(self.paths[2]))
        updated.replace(self.paths[2])
        with self.assertRaisesRegex(ValueError,'字幕样式'):
            pack_variants(self.paths,self.root/'bad.mkv')

    def test_missing_priming_metadata_exception_does_not_hide_later_drift(self):
        def timeline(values,start):return {'v':[],'s':[],'a':[dict(values=values,start_ms=start,packets=len(values))]}
        expected=timeline([[0,23],[23,23],[46,23]],-23)
        actual=timeline([[None,None],[0,23],[23,23]],0)
        self.assertEqual(compare_timelines(actual,expected),0)
        actual['a'][0]['values'][2][0]=30
        with self.assertRaisesRegex(ValueError,'7 毫秒'):compare_timelines(actual,expected)


if __name__=='__main__':unittest.main()
