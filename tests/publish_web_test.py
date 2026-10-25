"""Publish workspace HTTP: approval gate, token, generation job and file serving."""
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import webui
import review_web
import publish_export
from review_catalog import build_catalog
from review_store import ReviewStore


class PublishWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temp.name)
        self.root = self.base_dir / 'exports'
        self.root.mkdir()
        self.media = self.root / 'song.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi',
                        '-i', 'color=c=purple:s=320x180:d=2', '-f', 'lavfi',
                        '-i', 'sine=frequency=440:duration=2', '-c:v', 'libx264',
                        '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest',
                        str(self.media)], check=True)
        rows = [dict(project='demo', track='01_Song', quality=dict(
                         total_rows=2, lower_confidence_rows=[], higher_confidence_rows=[1, 2],
                         translation_low_rows=[], alignment_median_model_score=.9),
                     acceptance=dict(status='ok', label='ok', issues=[]), health=dict(ok=True),
                     variants=[dict(file=str(self.media), variant=1, duration=2)])]
        (self.root / 'track-manifest.json').write_text(json.dumps(rows), encoding='utf8')
        self.store = ReviewStore(self.base_dir / 'human-reviews.json')
        self.native = self.base_dir / 'webui'
        self.native.mkdir()
        catalog, _ = build_catalog(self.root)
        track = catalog['tracks'][0]
        media = track['files'][0]
        self.store.save(track, dict(status='passed', revision=0, file_id=media['id'],
                                    file_signature=media['signature'], notes='已听看', issues=[]))
        self.publish_root = self.base_dir / 'publish'
        self.patches = [
            patch.object(review_web, 'ROOT', self.root),
            patch.object(review_web, 'REVIEWS', self.store),
            patch.object(review_web, 'NATIVE_ROOT', self.native),
            patch.object(publish_export, 'PUBLISH_ROOT', self.publish_root),
        ]
        for item in self.patches:
            item.start()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), webui.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def library(self):
        response = requests.get(self.base + '/api/publish/library', timeout=10)
        self.assertEqual(response.status_code, 200)
        return response.json()

    def post(self, path, payload, token=None):
        headers = {'X-Publish-Token': token} if token else {}
        return requests.post(self.base + path, json=payload, headers=headers, timeout=20)

    def settings(self, data, platform='bilibili'):
        item = data['tracks'][0]['settings']
        item['platforms']['bilibili'].update(tags='现场,测试', partition='30')
        item['platforms']['douyin'].update(topics='#现场')
        item['common'].update(subtitle='none', cover_time=0.4)
        return item

    def test_page_token_and_workspace_tab(self):
        page = requests.get(self.base + '/publish', timeout=10)
        self.assertEqual(page.status_code, 200)
        self.assertIn('publish.js', page.text)
        self.assertIn('发布准备', page.text)
        self.assertEqual(page.headers['X-Frame-Options'], 'SAMEORIGIN')
        workspace = requests.get(self.base, timeout=10).text
        self.assertIn('tab-publish', workspace)
        data = self.library()
        self.assertEqual(data['platform_order'], ['bilibili', 'douyin'])
        self.assertEqual(len(data['tracks']), 1)
        self.assertTrue(data['token'])
        self.assertIn('bilibili', data['platforms'])

    def test_unapproved_track_is_skipped(self):
        rows = json.loads((self.root / 'track-manifest.json').read_text(encoding='utf8'))
        rows.append(dict(project='demo', track='02_Other', quality=dict(
                             total_rows=1, lower_confidence_rows=[], higher_confidence_rows=[],
                             translation_low_rows=[], alignment_median_model_score=.5),
                         acceptance=dict(status='ok', label='ok', issues=[]), health=dict(ok=True),
                         variants=[dict(file=str(self.media), variant=1, duration=2)]))
        (self.root / 'track-manifest.json').write_text(json.dumps(rows), encoding='utf8')
        data = self.library()
        self.assertEqual(len(data['tracks']), 1)
        self.assertEqual(data['skipped'], 1)

    def test_settings_require_token_and_roundtrip(self):
        data = self.library()
        settings = self.settings(data)
        self.assertEqual(self.post('/api/publish/settings',
                                   {'track_id': data['tracks'][0]['id'], 'settings': settings}).status_code, 403)
        response = self.post('/api/publish/settings',
                             {'track_id': data['tracks'][0]['id'], 'settings': settings}, data['token'])
        self.assertEqual(response.status_code, 200, response.text)
        saved = json.loads((self.publish_root / 'settings.json').read_text(encoding='utf8'))
        self.assertEqual(saved['tracks'][data['tracks'][0]['id']]['common']['audio'], 'original')

    def test_preflight_and_generation_job(self):
        data = self.library()
        track_id = data['tracks'][0]['id']
        settings = self.settings(data)
        plan = self.post('/api/publish/preflight',
                         {'track_id': track_id, 'platforms': ['bilibili'], 'settings': settings},
                         data['token'])
        self.assertEqual(plan.status_code, 200, plan.text)
        body = plan.json()
        self.assertTrue(body['ok'], body)
        started = self.post('/api/publish/start',
                            {'track_id': track_id, 'platforms': ['bilibili'], 'settings': settings},
                            data['token'])
        self.assertEqual(started.status_code, 200, started.text)
        job_id = started.json()['job']
        job = None
        for _ in range(150):
            response = requests.get(self.base + '/api/publish/job?job=' + job_id, timeout=10)
            self.assertEqual(response.status_code, 200)
            job = response.json()
            if job['state'] != 'running':
                break
            time.sleep(0.1)
        self.assertEqual(job['state'], 'done', job)
        result = job['results'][0]
        self.assertEqual(result['platform'], 'bilibili')
        for name in ('video.mp4', 'cover.jpg', 'publish.json', 'upload-info.txt'):
            self.assertIn(name, result['files'])
        video_url = result['files']['video.mp4']
        response = requests.get(self.base + video_url, headers={'Range': 'bytes=0-99'}, timeout=10)
        self.assertEqual(response.status_code, 206)
        self.assertEqual(len(response.content), 100)
        after = self.library()
        state = after['tracks'][0]['state']
        self.assertEqual(state['packages']['bilibili']['status'], 'generated')
        self.assertIn('/publish-files/', state['packages']['bilibili']['files']['video.mp4'])

    def test_mark_submitted_and_asset_containment(self):
        data = self.library()
        track_id = data['tracks'][0]['id']
        settings = self.settings(data)
        started = self.post('/api/publish/start',
                            {'track_id': track_id, 'platforms': ['bilibili'], 'settings': settings},
                            data['token'])
        job_id = started.json()['job']
        for _ in range(150):
            job = requests.get(self.base + '/api/publish/job?job=' + job_id, timeout=10).json()
            if job['state'] != 'running':
                break
            time.sleep(0.1)
        self.assertEqual(job['state'], 'done', job)
        marked = self.post('/api/publish/mark',
                           {'track_id': track_id, 'platform': 'bilibili',
                            'url': 'https://www.bilibili.com/video/BV1test', 'bvid': 'BV1test',
                            'note': '本机记录'}, data['token'])
        self.assertEqual(marked.status_code, 200, marked.text)
        self.assertTrue(marked.json()['record']['submitted'])
        state = json.loads((self.publish_root / 'demo/01_Song/state.json').read_text(encoding='utf8'))
        self.assertEqual(state['packages']['bilibili']['submitted']['bvid'], 'BV1test')
        self.assertEqual(requests.get(self.base + '/publish-assets/%2e%2e/publish_web.py',
                                      timeout=10).status_code, 404)
        self.assertEqual(requests.get(self.base + '/publish-files/%2e%2e/%2e%2e/bilibili/video.mp4',
                                      timeout=10).status_code, 404)


if __name__ == '__main__':
    unittest.main()
