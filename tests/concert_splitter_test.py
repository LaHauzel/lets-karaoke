"""Synthetic media and HTTP regression tests; no personal concert media required."""
import hashlib
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
import concert_splitter as splitter
import concert_web as concert
import webui
from http.server import ThreadingHTTPServer


class BoundaryTests(unittest.TestCase):
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

    def test_invalid_times(self):
        for start, end in [(float('nan'), 3), (0, float('inf')), (-1, 2), (3, 2), (0, 11)]:
            with self.assertRaises(ValueError):
                splitter.validate_segments([{'start': start, 'end': end}], 10)
        with self.assertRaises(ValueError):
            splitter.validate_segments([{'start':0, 'end':6}, {'start':5, 'end':10}], 10)
        self.assertEqual(len(splitter.validate_segments([{'start':0,'end':2},{'start':3,'end':4}],10)),2)


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

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
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
