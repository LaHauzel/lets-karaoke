"""Review persistence, same-origin routes, media boundaries and revision identity."""
import json
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
import webui
import review_web
from review_catalog import build_catalog, revision_lyrics
from review_store import ReviewStore
from revision_policy import shift_alignment, revision_fingerprint


class ReviewWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)/'exports'
        self.root.mkdir()
        self.file = self.root/'track.mp4'
        self.file.write_bytes(b'0123456789')
        row = dict(project='demo', track='01_Song', quality=dict(total_rows=1,
            lower_confidence_rows=[],higher_confidence_rows=[1],translation_low_rows=[],alignment_median_model_score=.9),
            acceptance=dict(status='ok',label='ok',issues=[]),health=dict(ok=True),
            variants=[dict(file=str(self.file),variant=1,duration=10)])
        (self.root/'track-manifest.json').write_text(json.dumps([row]))
        self.store = ReviewStore(self.root.parent/'reviews.json')
        self.native = Path(self.temp.name)/'webui'
        self.native.mkdir()
        self.patches = [patch.object(review_web,'ROOT',self.root),patch.object(review_web,'REVIEWS',self.store),
                        patch.object(review_web,'NATIVE_ROOT',self.native)]
        for p in self.patches:p.start()
        self.server = ThreadingHTTPServer(('127.0.0.1',0),webui.Handler)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown();self.server.server_close()
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()

    def test_tab_and_range_containment(self):
        page = requests.get(self.base+'/review',timeout=5)
        self.assertIn('/api/review/reviews/export',page.text)
        self.assertEqual(page.headers['X-Frame-Options'],'SAMEORIGIN')
        self.assertIn('tab-review',requests.get(self.base,timeout=5).text)
        result = requests.get(self.base+'/review-exports/track.mp4',headers={'Range':'bytes=3-6'},timeout=5)
        self.assertEqual((result.status_code,result.content),(206,b'3456'))
        self.assertEqual(requests.get(self.base+'/review-exports/%2e%2e/reviews.json',timeout=5).status_code,404)
        self.assertEqual(requests.get(self.base+'/review-assets/%2e%2e/webui.py',timeout=5).status_code,404)

    def test_catalog_prefers_available_vocal_mp4_without_approving_it(self):
        folder=self.root/'hardsub-samples';folder.mkdir()
        video=folder/'preview-v3.mp4';video.write_bytes(b'preview')
        (folder/'manifest.json').write_text(json.dumps([dict(project='demo',track='01_Song',
            variants=[dict(variant=3,file=str(video),duration=10)])]))
        catalog,_=build_catalog(self.root);track=catalog['tracks'][0]
        selected=next(f for f in track['files']if f['id']==track['default_file'])
        self.assertEqual((selected['kind'],selected['variant']),('hard',3))
        self.assertFalse(self.store.path.exists())

    def test_delivery_opens_isolated_matching_editor_without_changing_review(self):
        source=self.root/'source.mp4';source.write_bytes(b'clean source')
        align=self.root/'align.json'
        align.write_text(json.dumps(dict(media=str(source),lang='ja',audio_duration=10,
            options={},lines=[dict(raw='sample',start=2,end=4,tokens=[])])))
        links={'demo/01_Song':dict(source='C:/concert.mp4',source_start=120,versions={
            'baseline':dict(align='exports/align.json',media='exports/source.mp4',source_start=140,
                            source='C:/clean-concert.mp4',concert_id='clean-record')})}
        (self.root/'workflow-links.json').write_text(json.dumps(links))
        data=requests.get(self.base+'/api/review/library').json();track=data['tracks'][0];file=track['files'][0]
        self.assertTrue(file['workflow']['editable'])
        headers={'X-Library-Token':data['reveal_token']}
        payload=dict(track_id=track['id'],file_id=file['id'],file_signature=file['signature'])
        self.assertEqual(requests.post(self.base+'/api/review/edit',json=payload).status_code,403)
        with patch('pipeline.probe_media',return_value={'duration':10,'has_video':True}):
            first=requests.post(self.base+'/api/review/edit',headers=headers,json=payload)
            self.assertEqual(first.status_code,200,first.text)
            second=requests.post(self.base+'/api/review/edit',headers=headers,json=payload)
        self.assertEqual(first.json(),second.json())
        directory=self.native/first.json()['track_id'].split('/')[1]
        copied=json.loads((directory/'align.json').read_text(encoding='utf-8'))
        self.assertEqual((copied['lines'][0]['start'],copied['media']),(2,str(source.resolve())))
        self.assertEqual(self.file.read_bytes(),b'0123456789')
        self.assertFalse(self.store.path.exists())
        self.assertEqual(len(list(self.native.glob('review-*'))),1)
        metadata=json.loads((directory/'job.json').read_text(encoding='utf-8'))
        self.assertEqual(metadata['review_origin']['source_start'],140)
        self.assertEqual(metadata['review_origin']['source'],'C:/clean-concert.mp4')
        self.assertEqual(metadata['review_origin']['concert_id'],'clean-record')
        self.assertEqual(metadata['align_source'],str(source.resolve()))
        payload['file_signature']='stale'
        self.assertEqual(requests.post(self.base+'/api/review/edit',headers=headers,json=payload).status_code,409)

    def test_editor_rejects_outside_project_inputs(self):
        links={'demo/01_Song':{'versions':{'baseline':{'align':'../outside.json','media':'exports/track.mp4'}}}}
        (self.root/'workflow-links.json').write_text(json.dumps(links))
        data=requests.get(self.base+'/api/review/library').json();track=data['tracks'][0];file=track['files'][0]
        result=requests.post(self.base+'/api/review/edit',headers={'X-Library-Token':data['reveal_token']},
            json=dict(track_id=track['id'],file_id=file['id'],file_signature=file['signature']))
        self.assertEqual(result.status_code,400,result.text)
        self.assertEqual(list(self.native.iterdir()),[])

    def test_save_restart_conflict_and_origin(self):
        data = requests.get(self.base+'/api/review/library',timeout=5).json()
        file = data['tracks'][0]['files'][0]
        payload = dict(track_id='demo/01_Song',status='passed',revision=0,file_id=file['id'],file_signature=file['signature'],notes='已检查',issues=[])
        headers = {'Origin':self.base,'X-Library-Token':data['reveal_token']}
        path = self.base+'/api/review/reviews'
        self.assertEqual(requests.post(path,json=payload,timeout=5).status_code,403)
        self.assertEqual(requests.post(path,json=payload,headers={**headers,'Origin':'https://other.example'},timeout=5).status_code,403)
        self.assertEqual(requests.post(path,json=payload,headers=headers,timeout=5).status_code,200)
        self.assertEqual(ReviewStore(self.store.path).read()['reviews']['demo/01_Song']['notes'],'已检查')
        self.assertEqual(requests.post(path,json=payload,headers=headers,timeout=5).status_code,409)
        self.assertEqual(requests.get(self.base+'/api/review/reviews/export',timeout=5).json()['reviews']['demo/01_Song']['status'],'passed')
        updated={**payload,'revision':1,'notes':'再次检查'}
        self.assertEqual(requests.post(path,json=updated,headers=headers,timeout=5).status_code,200)
        history=requests.get(self.base+'/api/review/library',timeout=5).json()['tracks'][0]['review_history']
        self.assertEqual(history[0]['notes'],'已检查')
        self.assertEqual(history[0]['file_id'],file['id'])

    def test_old_approval_does_not_approve_new_release(self):
        catalog,_ = build_catalog(self.root)
        track=catalog['tracks'][0];file=track['files'][0]
        self.store.save(track,dict(status='passed',revision=0,file_id=file['id'],file_signature=file['signature']))
        newfile=self.root/'new.mp4';newfile.write_bytes(b'new')
        (self.root/'revisions').mkdir()
        (self.root/'revisions/manifest.json').write_text(json.dumps([dict(project='demo',track='01_Song',revision='r2',files=[dict(file=str(newfile),variant=1,duration=8)])]))
        catalog,_=build_catalog(self.root);self.store.attach(catalog)
        track=catalog['tracks'][0];record=track['human_review']
        self.assertEqual(record['status'],'passed')
        self.assertFalse(record['stale'])
        self.assertTrue(record['new_version'])
        self.assertNotEqual(track['default_file'],file['id'])

    def test_empty_library(self):
        (self.root/'track-manifest.json').unlink()
        data=requests.get(self.base+'/api/review/library',timeout=5).json()
        self.assertEqual(data['tracks'],[])

    def test_library_can_link_the_latest_local_revision_report(self):
        folder=self.root/'revisions/round-two';folder.mkdir(parents=True)
        (folder/'index.html').write_text('latest')
        metadata=self.root/'library-metadata.json'
        metadata.write_text(json.dumps(dict(revision_report='revisions/round-two/index.html')))
        catalog,_=build_catalog(self.root,url_prefix='/exports')
        self.assertEqual(catalog['revision_report'],'/exports/revisions/round-two/index.html')
        outside=self.root.parent/'private.html';outside.write_text('private')
        metadata.write_text(json.dumps(dict(revision_report='../private.html')))
        catalog,_=build_catalog(self.root)
        self.assertIsNone(catalog['revision_report'])
        self.assertTrue(catalog['diagnostics'])

    def test_archived_revision_keeps_review_and_lyrics_bound_to_old_file(self):
        folder=self.root/'revisions';folder.mkdir()
        old=folder/'old.mp4';old.write_bytes(b'old')
        new=folder/'new.mp4';new.write_bytes(b'new')
        previous=dict(project='demo',track='01_Song',revision='r1',
            files=[dict(file=str(old),variant=1,duration=10)],
            evidence=[dict(row=1,start=1,end=3,text='Old release lyric',score=.8,disagreement=.1)])
        current={**previous,'revision':'r2','files':[dict(file=str(new),variant=1,duration=8)]}
        (folder/'manifest.json').write_text(json.dumps([previous]))
        track=build_catalog(self.root)[0]['tracks'][0]
        file=next(f for f in track['files'] if f.get('revision')=='r1')
        self.store.save(track,dict(status='needs_changes',revision=0,file_id=file['id'],file_signature=file['signature']))
        (folder/'manifest.json').write_text(json.dumps([current]))
        (folder/'history.json').write_text(json.dumps([previous,previous]))
        catalog,allowed=build_catalog(self.root);self.store.attach(catalog);track=catalog['tracks'][0]
        self.assertEqual(sum(f.get('revision')=='r1' for f in track['files']),1)
        self.assertIn(file['id'],allowed)
        self.assertFalse(track['human_review']['stale'])
        self.assertTrue(track['human_review']['new_version'])
        self.assertEqual(track['revision_history'][0]['evidence'][0]['text'],'Old release lyric')

    def test_revision_acceptance_does_not_replace_historical_evidence(self):
        path=self.root/'track-manifest.json';rows=json.loads(path.read_text())
        rows[0]['acceptance']=dict(status='needs_fix',label='collapsed line',issues=[dict(rows=[1],message='too short')])
        rows[0]['health']=dict(ok=False);path.write_text(json.dumps(rows))
        folder=self.root/'revisions';folder.mkdir()
        video=folder/'fixed.mp4';video.write_bytes(b'fixed')
        revision=dict(project='demo',track='01_Song',revision='r2',files=[dict(file=str(video),variant=1,duration=10)],
            acceptance=dict(status='ok',label='structure passed',issues=[]),health=dict(ok=True),
            evidence=[dict(row=1,start=2,end=4,score=.9,disagreement=.1,text='hello')])
        (folder/'manifest.json').write_text(json.dumps([revision]))
        track=build_catalog(self.root)[0]['tracks'][0]
        self.assertEqual((track['grade'],track['baseline_quality']['grade']),('high','fix'))
        self.assertEqual(track['acceptance']['issues'],[])
        self.assertEqual(track['baseline_acceptance']['issues'][0]['rows'],[1])
        self.assertTrue(track['health']['ok']);self.assertFalse(track['baseline_health']['ok'])

    def test_repair_report_is_diagnostic_not_a_new_approval(self):
        folder=self.root/'repair-runs';folder.mkdir();page=folder/'index.html';page.write_text('report')
        (folder/'manifest.json').write_text(json.dumps([dict(project='demo',track='01_Song',changed=2,
            blocking=1,duration=0,render_gate=False,report=str(page),anchor='01_Song')]))
        data=requests.get(self.base+'/api/review/library').json();track=data['tracks'][0]
        self.assertEqual(track['repair']['changed'],2)
        self.assertEqual(track['human_review']['status'],'unchecked')
        self.assertEqual(len(track['files']),1)
        self.assertEqual(requests.get(self.base+track['repair']['asset']['url']).text,'report')

    def test_catalog_returns_lyrics_for_the_matching_release(self):
        import csv
        with (self.root/'confidence-details.csv').open('w',encoding='utf-8-sig',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=['project','track','row','clip_start','clip_end',
                'model_score','dual_disagreement_seconds','priority','reasons','lyrics'])
            writer.writeheader();writer.writerow(dict(project='demo',track='01_Song',row=1,clip_start=1,
                clip_end=3,model_score=.8,dual_disagreement_seconds=.1,priority='review',reasons='',lyrics='旧版歌词'))
        folder=self.root/'revisions';folder.mkdir()
        align=folder/'align.json';align.write_text(json.dumps(dict(lines=[dict(raw='新版歌词',start=2,end=4)])))
        video=folder/'new.mp4';video.write_bytes(b'new')
        revision=dict(project='demo',track='01_Song',revision='r2',supporting=[str(align)],
            files=[dict(file=str(video),variant=1,duration=10)],
            evidence=[dict(row=1,start=2,end=4,score=.8,disagreement=.1)])
        (folder/'manifest.json').write_text(json.dumps([revision]))
        track=build_catalog(self.root)[0]['tracks'][0]
        self.assertEqual(track['evidence'][0]['text'],'旧版歌词')
        self.assertEqual(track['revision']['evidence'][0]['text'],'新版歌词')
        revision['evidence'][0]['start']=5
        revision_lyrics(revision,self.root)
        self.assertEqual(revision['evidence'][0]['text'],'')
        outside=self.root.parent/'align.json';outside.write_text(align.read_text())
        revision['supporting']=[str(outside)];revision['evidence'][0]['start']=2
        revision_lyrics(revision,self.root)
        self.assertEqual(revision['evidence'][0]['text'],'')

    def test_native_generation_is_discovered_with_versioned_review(self):
        job=self.native/'sample-job';job.mkdir()
        align=dict(audio_duration=12,lines=[dict(raw='test',start=1,end=3,tokens=[])],health=dict(ok=True))
        (job/'align.json').write_text(json.dumps(align))
        (job/'job.json').write_text(json.dumps(dict(media='Song.wav')))
        (job/'Song_karaoke.mp4').write_bytes(b'0123456789')
        data=requests.get(self.base+'/api/review/library',timeout=5).json()
        track=next(t for t in data['tracks'] if t.get('native')) if any(t.get('native') for t in data['tracks']) else None
        self.assertIsNotNone(track)
        self.assertEqual(track['title'],'Song')
        self.assertIsNone(track['score'])
        self.assertEqual(track['grade'],'unknown')
        self.assertEqual(track['evidence'][0]['text'],'test')
        file=track['files'][0]
        response=requests.get(self.base+file['url'],headers={'Range':'bytes=1-3'},timeout=5)
        self.assertEqual((response.status_code,response.content),(206,b'123'))
        headers={'Origin':self.base,'X-Library-Token':data['reveal_token']}
        payload=dict(track_id=track['id'],status='passed',revision=0,file_id=file['id'],file_signature=file['signature'])
        self.assertEqual(requests.post(self.base+'/api/review/reviews',json=payload,headers=headers,timeout=5).status_code,200)
        align['lines'][0]['raw']='version one'
        (job/'align_v1.json').write_text(json.dumps(align))
        (job/'Song_karaoke_v1.mp4').write_bytes(b'revised')
        align['lines'][0]['raw']='version two'
        (job/'align_v2.json').write_text(json.dumps(align))
        (job/'Song_karaoke_v2.mp4').write_bytes(b'revised again')
        data=requests.get(self.base+'/api/review/library',timeout=5).json()
        track=next(t for t in data['tracks'] if t.get('native'))
        self.assertEqual(track['human_review']['status'],'passed')
        self.assertTrue(track['human_review']['new_version'])
        self.assertNotEqual(track['default_file'],file['id'])
        self.assertEqual([f['evidence'][0]['text'] for f in track['files']],['version two','version one','test'])
        self.assertEqual(requests.get(self.base+'/review-native/%2e%2e/reviews.json',timeout=5).status_code,404)


class RevisionPolicyTests(unittest.TestCase):
    def test_backup_metadata_does_not_enter_generation_history(self):
        from local_history import scan
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            backup=root/'revision-audit';backup.mkdir();(backup/'.history-ignore').touch()
            snapshot=backup/'snapshot';snapshot.mkdir();(snapshot/'align.json').write_text('{}')
            live=root/'actual-job';live.mkdir();(live/'align.json').write_text('{}')
            self.assertEqual([r['path'] for r in scan(root)['records']],['actual-job'])

    def test_accurate_trim_preserves_duration_and_refuses_cut_lyrics(self):
        original=dict(audio_duration=20,lines=[dict(raw='song',tokens=[dict(start=10,end=12)])])
        changed=shift_alignment(original,5,10)
        self.assertEqual(changed['lines'][0]['start'],5)
        self.assertEqual(changed['lines'][0]['end'],7)
        self.assertEqual(original['lines'][0]['tokens'][0]['start'],10)
        with self.assertRaises(ValueError):shift_alignment(original,11,5)

    def test_cache_key_includes_timestamp_and_trim(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'source';path.write_bytes(b'a')
            old=revision_fingerprint([path],dict(start=0))
            self.assertNotEqual(old,revision_fingerprint([path],dict(start=1)))
            import os
            stat=path.stat();os.utime(path,ns=(stat.st_atime_ns,stat.st_mtime_ns+1000000000))
            self.assertNotEqual(old,revision_fingerprint([path],dict(start=0)))


if __name__=='__main__':unittest.main()
