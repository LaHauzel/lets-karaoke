"""Synthetic media and HTTP regression tests; no personal concert media required."""
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

import numpy as np
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
import concert_splitter as splitter
import concert_web as concert
import webui
from http.server import ThreadingHTTPServer


class BoundaryTests(unittest.TestCase):
    def test_sustained_timbre_transition_without_volume_dip(self):
        spectra = np.zeros((600, 20))
        spectra[:300, 0], spectra[300:, -1] = 1, 1
        for sensitivity in ('conservative', 'balanced', 'sensitive'):
            result = splitter.suggest(np.full(600, -15.), spectra, 600, 120, sensitivity)
            self.assertEqual(len(result['candidates']), 1)
            self.assertAlmostEqual(result['candidates'][0]['time'], 300, delta=3)
        spectra[:, :] = 0
        spectra[:, 0] = 1
        spectra[300, 0], spectra[300, -1] = 0, 1
        self.assertEqual(splitter.suggest(np.full(600, -15.), spectra, 600, 120)['candidates'], [])

    def test_valleys_and_full_coverage(self):
        energy = np.full(600, -15.)
        energy[195:207] = -55
        energy[395:407] = -55
        spectra = np.ones((600, 20))
        result = splitter.suggest(energy, spectra, 600, 120)
        self.assertEqual(len(result['candidates']), 2)
        self.assertAlmostEqual(result['candidates'][0]['time'], 201, delta=3)
        self.assertAlmostEqual(result['candidates'][1]['time'], 401, delta=3)
        self.assertEqual(result['segments'][0]['start'], 0)
        self.assertEqual(result['segments'][-1]['end'], 600)
        self.assertEqual(sum(s['end']-s['start'] for s in result['segments']), 600)

    def test_constant_audio_does_not_force_regular_cuts(self):
        r = splitter.suggest(np.full(1000, -15.), np.ones((1000, 20)), 1000)
        self.assertEqual(r['candidates'], [])
        self.assertEqual(len(r['segments']), 1)

    def test_yamnet_speech_vote_is_flagged_but_music_is_not(self):
        energy=np.full(90,-18.)
        spectra=np.ones((90,20))
        activity=np.zeros(90)
        activity[8:16]=.84
        activity[19:25]=.84
        speech=np.zeros(180)
        music=np.full(180,.20)
        speech[16:30]=.85
        speech[40:50]=.30  # Moderate classifier evidence is kept only with VAD support.
        speech[60:70]=.30  # The same evidence without VAD support is suppressed.
        music[100:140]=.85  # A singing/music-only region must not be called MC.
        base=splitter.suggest(energy,spectra,90,30)
        assisted=splitter.suggest(energy,spectra,90,30,speech_activity=activity,
                                  sound_scores={'speech':speech,'music':music,'hop':.48})
        self.assertEqual(assisted['segments'],base['segments'])
        self.assertEqual(len(assisted['speech_ranges']),2)
        self.assertAlmostEqual(assisted['speech_ranges'][0]['start'],7.68)
        self.assertAlmostEqual(assisted['speech_ranges'][0]['end'],14.88)
        self.assertGreaterEqual(assisted['speech_ranges'][0]['speech_score'],.80)
        self.assertAlmostEqual(assisted['speech_ranges'][1]['start'],19.2)
        self.assertAlmostEqual(assisted['speech_ranges'][1]['end'],24.48)

    def test_invalid_times(self):
        for start, end in [(float('nan'), 3), (0, float('inf')), (-1, 2), (3, 2), (0, 11)]:
            with self.assertRaises(ValueError):
                splitter.validate_segments([{'start': start, 'end': end}], 10)
        with self.assertRaises(ValueError):
            splitter.validate_segments([{'start':0, 'end':6}, {'start':5, 'end':10}], 10)
        self.assertEqual(len(splitter.validate_segments([{'start':0,'end':2},{'start':3,'end':4}],10)),2)
        for invalid in (None, {'start': True, 'end': 2}, {'start': 0, 'end': 2, 'selected': 'false'}):
            with self.assertRaises(ValueError):
                splitter.validate_segments([invalid], 10)

    def test_speech_uses_explicit_global_times_and_vad_alignment(self):
        sound = {'speech': np.full(5, .3), 'music': np.zeros(5),
                 'times': np.arange(5)*.48 + 70, 'hop': .48, 'window': .975}
        vad = np.zeros(90)
        vad[70:74] = .8
        ranges = splitter.detect_speech_ranges(sound, 90, vad)
        self.assertEqual(len(ranges), 1)
        self.assertAlmostEqual(ranges[0]['start'], 70.)
        self.assertAlmostEqual(ranges[0]['end'], 72.895)

    def test_streamed_classifier_keeps_global_hops_across_many_batches(self):
        # Fake model returns each window's absolute input sample value. It also
        # emits one padded EOF window, as the real YAMNet model does.
        import concert_sound_classifier
        sample_rate, duration = 16000, 180.
        audio = np.arange(int(duration*sample_rate), dtype=np.float32)
        def score(values):
            count = max(1, int(np.ceil((len(values)-15600)/7680))+1)
            starts = np.arange(count)*7680
            scores = values[np.minimum(starts, len(values)-1)] / sample_rate
            return scores, np.zeros(count)
        def chunks(command, chunk_bytes, duration, cancelled):
            size = chunk_bytes//4
            for start in range(0, len(audio), size):
                yield audio[start:start+size].tobytes()
        with patch.object(concert_sound_classifier, 'load_classifier', return_value=score), \
                patch.object(splitter, '_decoded_chunks', side_effect=chunks):
            result = splitter.extract_sound_scores('unused.mp4', duration)
        np.testing.assert_allclose(result['speech'], result['times'], atol=1e-5)
        np.testing.assert_allclose(np.diff(result['times']), .48, atol=1e-12)
        self.assertGreater(result['times'][-1], duration-1.5)

    def test_real_yamnet_stream_matches_whole_waveform_when_installed(self):
        from concert_sound_classifier import load_classifier
        classify = load_classifier()
        if classify is None:
            self.skipTest('optional YAMNet model/runtime not installed')
        samples = np.arange(60*16000, dtype=np.float32)/16000
        audio = (.1*np.sin(2*np.pi*(220*samples+3*samples**2))).astype(np.float32)
        speech, music = classify(audio)
        def chunks(command, chunk_bytes, duration, cancelled):
            for start in range(0, len(audio), chunk_bytes//4):
                yield audio[start:start+chunk_bytes//4].tobytes()
        with patch.object(splitter, '_decoded_chunks', side_effect=chunks):
            result = splitter.extract_sound_scores('unused.mp4', 60)
        np.testing.assert_allclose(result['speech'], speech, atol=1e-4)
        np.testing.assert_allclose(result['music'], music, atol=1e-4)
        np.testing.assert_allclose(result['times'], np.arange(len(speech))*.48, atol=1e-12)

    def test_source_fingerprint_detects_same_size_and_timestamp_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)/'source.mp4'
            source.write_bytes(b'original')
            identity = splitter.source_identity(source)
            stat = source.stat()
            source.write_bytes(b'modified')
            os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            with self.assertRaisesRegex(ValueError, '替换或修改'):
                splitter.verify_source(source, identity)

    def test_blocked_decoder_is_cancellable_and_has_idle_timeout(self):
        class BlockedProcess:
            def __init__(self):
                self.stopped = threading.Event()
                self.returncode = None
                self.stdout = self
            def read(self, size):
                self.stopped.wait(3)
                return b''
            def close(self):
                pass
            def poll(self):
                return self.returncode
            def kill(self):
                self.returncode = -9
                self.stopped.set()
            def wait(self, timeout=None):
                return self.returncode
        process = BlockedProcess()
        cancel = threading.Event()
        trigger = threading.Timer(.03, cancel.set)
        trigger.start()
        start = time.monotonic()
        with patch.object(splitter.subprocess, 'Popen', return_value=process):
            with self.assertRaises(splitter.Cancelled):
                list(splitter._decoded_chunks([], 32000, 60, cancel.is_set))
        trigger.join()
        self.assertLess(time.monotonic()-start, 1)
        self.assertTrue(process.stopped.is_set())
        process = BlockedProcess()
        with patch.object(splitter.subprocess, 'Popen', return_value=process), \
                patch.object(splitter, 'DECODE_IDLE_TIMEOUT', .01):
            with self.assertRaises(TimeoutError):
                list(splitter._decoded_chunks([], 32000, 60, lambda: False))
        self.assertTrue(process.stopped.is_set())

    def test_export_idle_timeout_keeps_manifest_and_reaps_process(self):
        class StalledProcess:
            returncode = None
            def poll(self):
                return self.returncode
            def kill(self):
                self.returncode = -9
            def wait(self, timeout=None):
                return self.returncode
        process = StalledProcess()
        with tempfile.TemporaryDirectory() as tmp:
            source, target = Path(tmp)/'source.mp4', Path(tmp)/'export'
            source.write_bytes(b'fixture')
            with patch.object(splitter.subprocess, 'Popen', return_value=process), \
                    patch.object(splitter, 'EXPORT_IDLE_TIMEOUT', .01):
                with self.assertRaises(TimeoutError):
                    splitter.export_segments(source, [{'title': 'clip', 'start': 0, 'end': 1, 'selected': True}],
                                             target, 'copy', expected_identity=splitter.source_identity(source))
            self.assertEqual(process.returncode, -9)
            manifest = json.loads((target/'manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(manifest['state'], 'error')
            self.assertEqual(manifest['segments'], [])
            self.assertEqual(len(manifest['planned_segments']), 1)
            self.assertEqual(list(target.glob('*.partial.*')), [])


class ConcertRecordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_root, self.old_jobs = concert.ROOT, concert.JOBS
        self.old_active, self.old_cancel = concert.ACTIVE, concert.CANCEL
        concert.ROOT, concert.JOBS = Path(self.temp.name), {}
        concert.ACTIVE, concert.CANCEL = set(), set()
        self.source = concert.ROOT/'source.mp4'
        self.source.write_bytes(b'fixture')

    def tearDown(self):
        concert.ROOT, concert.JOBS = self.old_root, self.old_jobs
        concert.ACTIVE, concert.CANCEL = self.old_active, self.old_cancel
        self.temp.cleanup()

    def seed(self, identifier, created=1, **extra):
        job = {'id': identifier, 'source': str(self.source), 'source_identity': splitter.source_identity(self.source),
               'name': self.source.name, 'duration': 120., 'created': created, 'state': 'ready',
               'segments': [{'title': 'original', 'start': 0., 'end': 120., 'selected': True}],
               'candidates': [], 'waveform': [], 'speech_ranges': [], 'exports': [],
               'min_length': 30, 'sensitivity': 'balanced', 'analysis_version': 1}
        job.update(extra)
        concert.directory(identifier).mkdir()
        concert.JOBS[identifier] = job
        concert.append_version(job)
        concert.persist(job)
        return job

    def post(self, route, data):
        encoded = json.dumps(data).encode()
        handler = SimpleNamespace(headers={'Content-Type': 'application/json', 'Content-Length': str(len(encoded)),
                                          'Host': '127.0.0.1:8099'},
                                  server=SimpleNamespace(server_address=('127.0.0.1', 8099), server_port=8099),
                                  _body=lambda: encoded, _json=lambda obj, code=200: (code, obj))
        return concert.dispatch_post(handler, '/api/concert/'+route)

    def test_version_save_retains_latest_and_rejects_stale_editor(self):
        job = self.seed('1111111111111111')
        job['analysis_version'] = 2
        job['segments'][0]['title'] = 'v2 original'
        concert.append_version(job)
        concert.persist(job)
        data = {'id': job['id'], 'version': 1, 'revision': 0,
                'segments': [{'title': 'v1 edited', 'start': 2, 'end': 110}]}
        code, saved = self.post('save', data)
        self.assertEqual(code, 200)
        self.assertEqual(saved['selected_version'], 1)
        self.assertEqual(saved['segments'][0]['title'], 'v1 edited')
        self.assertEqual(job['segments'][0]['title'], 'v2 original')
        self.assertEqual(concert.job_view(job, 1)['segments'][0]['start'], 2)
        self.assertEqual(self.post('save', data)[0], 400)
        concert.JOBS.clear()
        self.assertEqual(concert.job_view(concert.snapshot(job['id']), 1)['segments'][0]['title'], 'v1 edited')

    def test_legacy_version_cannot_inherit_new_fingerprint_or_revision(self):
        job = self.seed('1111111111111111')
        job['analysis_versions'][0].pop('source_identity')
        job['analysis_versions'][0].pop('revision')
        job['revision'] = 4
        view = concert.job_view(job, 1)
        self.assertIsNone(view['source_identity'])
        self.assertEqual(view['revision'], 0)
        self.assertEqual(self.post('export', {'id': job['id'], 'version': 1, 'segments': job['segments']})[0], 400)

    def test_legacy_root_edits_are_restored_into_their_version(self):
        job = self.seed('1111111111111111')
        job['segments'][0]['title'], job['edited'] = 'legacy saved edit', True
        concert.persist(job)
        concert.JOBS.clear()
        self.assertEqual(concert.job_view(concert.snapshot(job['id']), 1)['segments'][0]['title'], 'legacy saved edit')

    def test_migration_defers_active_groups_and_alias_operations_protect_worker(self):
        first = self.seed('1111111111111111')
        second = self.seed('2222222222222222', 2, state='analyzing')
        concert.ACTIVE.add(second['id'])
        concert.migrate_legacy_records()
        self.assertEqual(concert.get_job(second['id'])['id'], second['id'])
        self.assertNotIn('merged_into', concert.JOBS[second['id']])
        # Also protect records merged by earlier versions of the application.
        second['merged_into'] = first['id']
        concert.persist(second)
        self.assertEqual(self.post('cancel', {'id': first['id']})[0], 200)
        self.assertIn(second['id'], concert.CANCEL)
        self.assertEqual(self.post('save', {'id': first['id'], 'segments': first['segments']})[0], 409)
        self.assertEqual(self.post('delete', {'id': first['id']})[0], 409)

    def test_migration_preserves_export_owner_and_alias_downloads(self):
        first = self.seed('1111111111111111')
        second = self.seed('2222222222222222', 2)
        export_id = 'export-123abc'
        folder = concert.directory(second['id'])/export_id
        folder.mkdir()
        (folder/'clip.mkv').write_bytes(b'fixture export')
        second['exports'] = [{'id': export_id, 'files': [{'file': 'clip.mkv'}], 'path': str(folder), 'mode': 'copy', 'version': 1}]
        concert.persist(second)
        concert.migrate_legacy_records()
        merged = concert.snapshot(first['id'])
        self.assertEqual(merged['exports'][0]['owner_id'], second['id'])
        self.assertEqual(merged['exports'][0]['version'], 2)
        self.assertEqual(concert.export_directory(merged, merged['exports'][0]), folder)
        handler = SimpleNamespace(_json=lambda obj, code=200: (code, obj))
        with patch.object(concert, 'serve_file', side_effect=lambda handler, path: path) as serve:
            path = concert.dispatch_get(handler, f'/concert-files/{first["id"]}/{export_id}/clip.mkv', {})
            self.assertEqual(path, folder/'clip.mkv')
            path = concert.dispatch_get(handler, f'/concert-files/{second["id"]}/concert.json', {})
            self.assertEqual(path, concert.directory(first['id'])/'concert.json')

    def test_concurrent_history_migration_is_idempotent(self):
        first = self.seed('1111111111111111')
        second = self.seed('2222222222222222', 2)
        barrier, errors = threading.Barrier(6), []
        def migrate():
            try:
                barrier.wait(timeout=2)
                concert.migrate_legacy_records()
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=migrate) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(concert.snapshot(first['id'])['version_count'], 2)
        self.assertEqual(concert.get_job(second['id'])['id'], first['id'])
        self.assertEqual(list(concert.ROOT.glob('*/*.tmp')), [])

    def test_reanalysis_reuses_cache_and_rejects_changed_source(self):
        job = self.seed('1111111111111111', state='analyzing')
        values = (np.full(120, -15.), np.ones((120, 20)), None)
        with patch.object(concert, 'extract_features', return_value=values), \
                patch.object(concert, 'extract_sound_scores', return_value=None):
            concert.run_analysis(job['id'])
        self.assertEqual(job['state'], 'ready')
        job.update(state='analyzing', analysis_version=2, sensitivity='sensitive')
        with patch.object(concert, 'extract_features', side_effect=AssertionError('decoded cached audio')), \
                patch.object(concert, 'extract_sound_scores', side_effect=AssertionError('classified cached audio')):
            concert.run_analysis(job['id'])
        self.assertEqual(job['state'], 'ready', job['message'])
        self.assertTrue(job['cache_reused'])
        self.assertEqual(job['version_count'], 2)
        self.source.write_bytes(b'replaced')
        self.assertEqual(self.post('reanalyze', {'id': job['id']})[0], 400)
        self.assertEqual(self.post('export', {'id': job['id'], 'segments': job['segments']})[0], 400)

    def test_cancelled_reanalysis_restores_saved_edit_version(self):
        job = self.seed('1111111111111111')
        concert.save_version_segments(job, [{'title': 'saved edit', 'start': 2, 'end': 110, 'selected': True}], 1)
        job.update(state='analyzing', analysis_version=2, segments=[], candidates=[])
        with patch.object(concert, 'extract_features', side_effect=splitter.Cancelled()):
            concert.run_analysis(job['id'])
        self.assertEqual(job['state'], 'cancelled')
        self.assertEqual(job['analysis_version'], 1)
        self.assertEqual(job['segments'][0]['title'], 'saved edit')
        self.assertEqual(job['revision'], 1)

    def test_damaged_cache_regenerates_and_optional_classifier_failure_degrades(self):
        job = self.seed('1111111111111111', state='analyzing')
        (concert.directory(job['id'])/'features.npz').write_bytes(b'invalid cache')
        with patch.object(concert, 'extract_features', return_value=(np.full(120, -15.), np.ones((120, 20)), None)) as features, \
                patch.object(concert, 'extract_sound_scores', side_effect=ValueError('broken classifier')):
            concert.run_analysis(job['id'])
        features.assert_called_once()
        self.assertEqual(job['state'], 'ready', job['message'])
        self.assertIn('broken classifier', job['notice'])
        self.assertEqual(job['speech_ranges'], [])

    def test_shutdown_cancels_worker_and_waits_for_release(self):
        job = self.seed('1111111111111111', state='analyzing')
        concert.ACTIVE.add(job['id'])
        def worker():
            while job['id'] not in concert.CANCEL:
                time.sleep(.01)
            with concert.LOCK:
                concert.ACTIVE.discard(job['id'])
        thread = threading.Thread(target=worker)
        thread.start()
        with patch.object(concert, 'cancel_running_processes') as stop:
            self.assertTrue(concert.shutdown(timeout=1))
        thread.join(timeout=1)
        stop.assert_called_once()

    def test_worker_start_failure_releases_active_slot(self):
        job = self.seed('1111111111111111', state='analyzing')
        with patch.object(concert.threading.Thread, 'start', side_effect=RuntimeError('no thread available')):
            with self.assertRaises(RuntimeError):
                concert.start_worker(job['id'], lambda: None, ())
        self.assertEqual(concert.ACTIVE, set())
        self.assertEqual(job['state'], 'error')


class ConcertHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.source = cls.root/'synthetic video.mp4'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=blue:s=160x90:r=25:d=6',
                        '-f','lavfi','-i','sine=frequency=440:sample_rate=8000:duration=6',
                        '-c:v','libx264','-g','50','-c:a','aac','-shortest',str(cls.source)],check=True,
                       creationflags=splitter.CREATE_FLAGS)
        cls.original = hashlib.sha256(cls.source.read_bytes()).hexdigest()
        cls.old_root = concert.ROOT
        concert.ROOT = cls.root/'jobs'
        cls.server = ThreadingHTTPServer(('127.0.0.1',0),webui.Handler)
        cls.server.daemon_threads = True
        threading.Thread(target=cls.server.serve_forever,daemon=True).start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'
        cls.sound_scores_patch = patch.object(concert,'extract_sound_scores',return_value=None)
        cls.sound_scores_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.sound_scores_patch.stop()
        concert.ROOT = cls.old_root
        concert.JOBS.clear()
        cls.temp.cleanup()

    def post(self, route, data, **kw):
        return requests.post(self.base+'/api/concert/'+route,json=data,timeout=10,**kw)

    def wait(self, identifier):
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            j=requests.get(self.base+'/api/concert/job',params={'id':identifier},timeout=5).json()
            if j['state'] not in {'analyzing','exporting'} and identifier not in concert.ACTIVE:
                return j
            time.sleep(.05)
        self.fail('Background job did not finish')

    def test_static_pages_preserve_original(self):
        self.assertIn('panel-concert',requests.get(self.base,timeout=5).text)
        self.assertEqual(requests.get(self.base+'/karaoke',timeout=5).content,(webui.ASSETS/'index.html').read_bytes())
        for asset in ('/concert','/concert.js','/concert.css','/api/meta'):
            self.assertEqual(requests.get(self.base+asset,timeout=5).status_code,200)

    def test_analysis_export_range_and_recovery(self):
        r=self.post('analyze',{'source':str(self.source)})
        self.assertEqual(r.status_code,202,r.text)
        identifier=r.json()['id']
        j=self.wait(identifier)
        self.assertEqual(j['state'],'ready',j)
        self.assertEqual(len(j['segments']),1)
        self.assertEqual(j['method'],'energy-spectral-v1')
        self.assertEqual(j['speech_ranges'],[])
        with np.load(concert.directory(identifier)/'features.npz') as features:
            if importlib.util.find_spec('webrtcvad') is None:
                self.assertNotIn('speech_activity', features.files)
            else:
                self.assertIn('speech_activity',features.files)
        self.assertIn(len(j['waveform']), (6, 7))  # AAC encoder padding may add a partial second.
        r=self.post('reanalyze',{'id':identifier,'min_length':120,'sensitivity':'sensitive'})
        self.assertEqual(r.status_code,202,r.text)
        self.assertEqual(r.json()['id'],identifier)
        j=self.wait(identifier)
        self.assertEqual(j['version_count'],2)
        self.assertEqual(j['sensitivity'],'sensitive')
        url=self.base+f'/concert-files/{identifier}/source'
        data=self.source.read_bytes()
        for value,expected in [('bytes=0-99',data[:100]),('bytes=-37',data[-37:]),('bytes=20-49',data[20:50])]:
            r=requests.get(url,headers={'Range':value},timeout=5)
            self.assertEqual(r.status_code,206)
            self.assertEqual(r.content,expected)
        for value in ['bytes=999999999-','bytes=99-20','bytes=-0','bytes=0-1,5-6']:
            self.assertEqual(requests.get(url,headers={'Range':value},timeout=5).status_code,416)
        self.assertEqual(requests.get(self.base+f'/concert-files/{identifier}/features.npz',timeout=5).status_code,404)
        for mode in ('copy','precise'):
            segments=[{'title':'测试 / clip','start':1.24,'end':2.76,'selected':True}]
            r=self.post('export',{'id':identifier,'segments':segments,'mode':mode})
            self.assertEqual(r.status_code,202,r.text)
            j=self.wait(identifier)
            self.assertEqual(j['state'],'done',j)
            ex=j['exports'][-1]
            dest=Path(ex['path'])/ex['files'][0]['file']
            self.assertTrue(dest.exists())
            metadata=splitter.probe(dest)
            self.assertEqual(metadata['video_codec'],'h264')
            if mode=='precise':
                self.assertAlmostEqual(metadata['duration'],1.52,delta=.08)
            subprocess.run(['ffmpeg','-v','error','-i',str(dest),'-f','null','-'],check=True,capture_output=True,
                           creationflags=splitter.CREATE_FLAGS)
            file_url=self.base+f'/concert-files/{identifier}/{ex["id"]}/'+requests.utils.quote(dest.name)+'?download=1'
            r=requests.get(file_url,timeout=5)
            self.assertEqual(r.status_code,200)
            self.assertIn("filename*=UTF-8''",r.headers['Content-Disposition'])
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(),self.original)
        concert.JOBS.pop(identifier)
        self.assertEqual(concert.snapshot(identifier)['segments'][0]['title'],'测试 / clip')
        self.assertEqual(len(concert.snapshot(identifier)['exports']),2)

    def test_invalid_input_and_cross_origin(self):
        self.assertEqual(self.post('analyze',{'source':str(self.source),'min_length':'inf'}).status_code,400)
        self.assertEqual(self.post('analyze',{'source':'missing.mp4'}).status_code,400)
        self.assertEqual(self.post('analyze',{'source':str(self.source)},headers={'Origin':'https://foreign.test'}).status_code,403)
        self.assertEqual(self.post('save',{'id':'../../escape','segments':[]}).status_code,400)

    def test_feature_extraction_without_optional_vad(self):
        with patch.dict(sys.modules, {'webrtcvad': None}):
            energy, spectra, activity = splitter.extract_features(self.source, 6)
        self.assertIsNone(activity)
        self.assertGreaterEqual(len(energy), 6)
        self.assertEqual(len(energy), len(spectra))

    def test_precise_export_pads_odd_video_dimensions(self):
        source = self.root/'odd video.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                        'color=c=blue:s=160x90:r=25:d=2,format=yuv444p,pad=161:91',
                        '-f', 'lavfi', '-i', 'sine=sample_rate=8000:duration=2',
                        '-c:v', 'libx264', '-pix_fmt', 'yuv444p', '-c:a', 'aac', '-shortest', str(source)],
                       check=True, creationflags=splitter.CREATE_FLAGS)
        self.assertEqual((splitter.probe(source)['width'], splitter.probe(source)['height']), (161, 91))
        with tempfile.TemporaryDirectory() as tmp:
            files = splitter.export_segments(source, [{'title': 'odd', 'start': .2, 'end': 1.2, 'selected': True}],
                                             Path(tmp)/'export', 'precise', expected_identity=splitter.source_identity(source))
            info = splitter.probe(Path(tmp)/'export'/files[0]['file'])
            self.assertEqual((info['width'], info['height']), (162, 92))

    def test_partial_export_published_and_resumable_after_reload(self):
        identifier = self.post('analyze', {'source': str(self.source)}).json()['id']
        self.assertEqual(self.wait(identifier)['state'], 'ready')
        segments = [{'title': 'first', 'start': .5, 'end': 1.5, 'selected': True},
                    {'title': 'second', 'start': 3., 'end': 4., 'selected': True}]
        def cancel_after_first(*args, **kwargs):
            original = kwargs['on_complete']
            def completed(value):
                original(value)
                concert.CANCEL.add(identifier)
            kwargs['on_complete'] = completed
            return splitter.export_segments(*args, **kwargs)
        with patch.object(concert, 'export_segments', side_effect=cancel_after_first):
            response = self.post('export', {'id': identifier, 'segments': segments, 'mode': 'precise'})
            self.assertEqual(response.status_code, 202, response.text)
            job = self.wait(identifier)
        self.assertEqual(job['state'], 'cancelled', job)
        export = job['exports'][-1]
        self.assertEqual(len(export['files']), 1)
        first = Path(export['path'])/export['files'][0]['file']
        initial = (first.stat().st_mtime_ns, hashlib.sha256(first.read_bytes()).hexdigest())
        prefix = self.base+f'/concert-files/{identifier}/{export["id"]}/'
        self.assertEqual(requests.get(prefix+requests.utils.quote(first.name), timeout=5).status_code, 200)
        manifest = requests.get(prefix+'manifest.json', timeout=5).json()
        self.assertEqual(len(manifest['segments']), 1)
        self.assertEqual(manifest['state'], 'cancelled')
        # A reload after an interrupted worker also retains partial artifacts.
        stored = concert.get_job(identifier)
        stored['state'] = stored['exports'][-1]['state'] = 'exporting'
        concert.persist(stored)
        concert.JOBS.pop(identifier)
        self.assertEqual(concert.snapshot(identifier)['state'], 'interrupted')
        response = self.post('resume-export', {'id': identifier, 'export_id': export['id']})
        self.assertEqual(response.status_code, 202, response.text)
        job = self.wait(identifier)
        self.assertEqual(job['state'], 'done', job)
        self.assertEqual(len(job['exports'][-1]['files']), 2)
        self.assertEqual((first.stat().st_mtime_ns, hashlib.sha256(first.read_bytes()).hexdigest()), initial)
        self.assertEqual(requests.get(prefix+'manifest.json', timeout=5).json()['state'], 'done')

    def test_cancel_and_exclusion(self):
        started=threading.Event()
        def slow(source,duration,progress,cancelled):
            started.set()
            while not cancelled():
                time.sleep(.02)
            raise splitter.Cancelled()
        with patch.object(concert,'extract_features',side_effect=slow):
            r=self.post('analyze',{'source':str(self.source)})
            identifier=r.json()['id']
            self.assertTrue(started.wait(5))
            self.assertEqual(self.post('analyze',{'source':str(self.source)}).status_code,409)
            self.assertEqual(self.post('save',{'id':identifier,'segments':[{'start':0,'end':1}]}).status_code,409)
            self.assertEqual(self.post('cancel',{'id':identifier}).status_code,200)
            self.assertEqual(self.wait(identifier)['state'],'cancelled')


if __name__=='__main__':
    unittest.main()
