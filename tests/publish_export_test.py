"""Publish packages: approval gates, platform metadata and real FFmpeg output."""
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import publish_export as publisher


ASS_TEXT = '''[Script Info]
ScriptType: v4.00+
PlayResX: 320
PlayResY: 180

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Microsoft YaHei,22,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,12,12,12,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.30,0:00:01.60,Default,,0,0,0,,テスト字幕
'''


class PublishExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.publish = self.root / 'out/publish'
        self.media = self.root / 'source.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi',
                        '-i', 'color=c=teal:s=320x180:d=2', '-f', 'lavfi',
                        '-i', 'sine=frequency=440:duration=2', '-c:v', 'libx264',
                        '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest',
                        str(self.media)], check=True)
        self.ass = self.root / 'normal.ass'
        self.ass.write_text(ASS_TEXT, encoding='utf8')
        self.candidate = self.make_candidate()

    def tearDown(self):
        self.temp.cleanup()

    def make_candidate(self, track='01_Song', approved=None):
        media = approved or self.media
        stat = media.stat()
        return dict(id=f'demo/{track}', project='demo', track=track, title='サンプル曲',
                    concert='测试场次', number='01', duration=2.0, grade='high', revision=None,
                    approved=dict(file_id='f1', path=str(media),
                                  signature=f'{stat.st_size}:{stat.st_mtime_ns}',
                                  kind='soft', variant=1, duration=2.0, bytes=stat.st_size,
                                  multitrack=None),
                    subtitles=[dict(name='normal.ass', path=str(self.ass), bilingual=False)],
                    review=dict(notes='', updated_at='2026-10-25T00:00:00+08:00'))

    def settings(self, candidate=None, **common):
        candidate = candidate or self.candidate
        result = publisher.default_settings(candidate)
        result['platforms']['bilibili'].update(tags='现场,测试', partition='30')
        result['platforms']['douyin'].update(topics='#现场')
        result['common'].update({'subtitle': 'none', 'cover_time': 0.5})
        result['common'].update(common)
        return result

    def test_candidates_only_include_passed_signature_matched_tracks(self):
        good = self.make_candidate()
        stale = self.make_candidate('02_Stale')
        unapproved = self.make_candidate('03_None')
        stale['approved']['signature'] = '9:9'
        catalog = dict(tracks=[
            dict(id=good['id'], project='demo', track='01_Song', title='A', duration=2,
                 files=[dict(id='f1', path=good['approved']['path'],
                             signature=good['approved']['signature'], exists=True,
                             kind='soft', variant=1, duration=2)],
                 supporting=[dict(path=str(self.ass), exists=True)],
                 human_review=dict(status='passed', stale=False, new_version=False,
                                   file_id='f1', file_signature=good['approved']['signature'])),
            dict(id=stale['id'], project='demo', track='02_Stale', title='B', duration=2,
                 files=[dict(id='f1', path=stale['approved']['path'],
                             signature=stale['approved']['signature'], exists=True,
                             kind='soft', variant=1, duration=2)],
                 supporting=[],
                 human_review=dict(status='passed', stale=False, new_version=False,
                                   file_id='f1', file_signature='0:0')),
            dict(id=unapproved['id'], project='demo', track='03_None', title='C', duration=2,
                 files=[], supporting=[], human_review=dict(status='unchecked')),
        ])
        items, skipped = publisher.candidates(catalog)
        self.assertEqual([item['id'] for item in items], ['demo/01_Song'])
        self.assertEqual(skipped, 2)
        self.assertEqual(items[0]['subtitles'][0]['name'], 'normal.ass')

    def test_preflight_validates_metadata_and_options(self):
        plan = publisher.preflight(self.candidate, self.settings(), ['bilibili', 'douyin'],
                                   root=self.publish)
        self.assertTrue(plan['ok'], plan)
        broken = self.settings()
        broken['platforms']['bilibili']['title'] = 'x' * 81
        plan = publisher.preflight(self.candidate, broken, ['bilibili'], root=self.publish)
        self.assertFalse(plan['ok'])
        self.assertTrue(any('标题' in error for error in plan['platforms']['bilibili']['errors']))
        missing = self.settings()
        missing['platforms']['douyin']['title'] = ''
        plan = publisher.preflight(self.candidate, missing, ['douyin'], root=self.publish)
        self.assertFalse(plan['ok'])
        accomp = self.settings(audio='accompaniment')
        plan = publisher.preflight(self.candidate, accomp, ['bilibili'], root=self.publish)
        self.assertFalse(plan['ok'])
        self.assertTrue(any('伴奏' in error for error in plan['errors']))
        burn = self.settings(subtitle='burn')
        no_subtitles = self.make_candidate()
        no_subtitles['subtitles'] = []
        plan = publisher.preflight(no_subtitles, burn, ['bilibili'], root=self.publish)
        self.assertFalse(plan['ok'])
        self.assertTrue(any('ASS' in error for error in plan['errors']))
        copy = self.settings(video_mode='copy', subtitle='none')
        plan = publisher.preflight(self.candidate, copy, ['bilibili'], root=self.publish)
        self.assertTrue(plan['ok'], plan)
        copy_burn = self.settings(video_mode='copy', subtitle='burn')
        plan = publisher.preflight(self.candidate, copy_burn, ['bilibili'], root=self.publish)
        self.assertFalse(plan['ok'])

    def test_generation_writes_verified_package_and_state(self):
        settings = self.settings()
        plans = publisher.preflight(self.candidate, settings, ['bilibili'], root=self.publish)
        result = publisher.build_package(self.candidate, plans, 'bilibili', self.publish)
        package = Path(result['package'])
        self.assertEqual({path.name for path in package.iterdir()},
                         {'video.mp4', 'cover.jpg', 'publish.json', 'upload-info.txt', 'sha256.txt'})
        data = json.loads((package / 'publish.json').read_text(encoding='utf8'))
        self.assertEqual(data['platform'], 'bilibili')
        self.assertEqual(data['metadata']['title'], 'サンプル曲')
        self.assertTrue(data['human_approved'])
        self.assertFalse(data['automatically_uploaded'])
        self.assertEqual(data['source']['signature'], self.candidate['approved']['signature'])
        digest = hashlib.sha256((package / 'video.mp4').read_bytes()).hexdigest()
        self.assertEqual(data['video']['sha256'], digest)
        line = [row for row in (package / 'sha256.txt').read_text(encoding='utf8').splitlines()
                if row.endswith('video.mp4')][0]
        self.assertEqual(line.split()[0], digest)
        info = (package / 'upload-info.txt').read_text(encoding='utf8')
        self.assertIn('サンプル曲', info)
        self.assertIn('member.bilibili.com', info)
        probe = json.loads(subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'stream=codec_name,width,height',
             '-of', 'json', str(package / 'video.mp4')],
            capture_output=True, text=True, check=True).stdout)
        streams = {stream['codec_name'] for stream in probe['streams']}
        self.assertTrue({'h264', 'aac'} <= streams)
        cover_probe = json.loads(subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'stream=width,height',
             '-of', 'json', str(package / 'cover.jpg')],
            capture_output=True, text=True, check=True).stdout)
        cover_image = next(stream for stream in cover_probe['streams'] if stream.get('width'))
        self.assertEqual((cover_image['width'], cover_image['height']), (1146, 717))
        publisher.record_generation(self.candidate['id'], 'bilibili', result, self.publish)
        state = publisher.load_state(self.candidate['id'], self.publish)
        self.assertEqual(state['packages']['bilibili']['status'], 'generated')
        self.assertEqual(state['history'][0]['event'], 'generated')

    def test_douyin_vertical_package_and_copy_mode(self):
        settings = self.settings(framing='vertical_blur', subtitle='none')
        settings['platforms']['douyin'].update(topics='#测试')
        plan = publisher.preflight(self.candidate, settings, ['douyin'], root=self.publish)
        self.assertTrue(plan['ok'], plan)
        result = publisher.build_package(self.candidate, plan, 'douyin', self.publish)
        probe = json.loads(subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'stream=width,height',
             '-of', 'json', str(Path(result['package']) / 'video.mp4')],
            capture_output=True, text=True, check=True).stdout)
        video = next(stream for stream in probe['streams'] if stream.get('width'))
        self.assertEqual((video['width'], video['height']), (1080, 1920))
        copy = self.settings(video_mode='copy', subtitle='none', framing='source')
        copy_plan = publisher.preflight(self.candidate, copy, ['bilibili'], root=self.publish)
        self.assertTrue(copy_plan['ok'], copy_plan)
        copied = publisher.build_package(self.candidate, copy_plan, 'bilibili', self.publish)
        self.assertTrue((Path(copied['package']) / 'video.mp4').is_file())

    def test_changed_approval_blocks_preflight_and_generation(self):
        self.media.write_bytes(self.media.read_bytes() + b'changed')
        plan = publisher.preflight(self.candidate, self.settings(), ['bilibili'], root=self.publish)
        self.assertFalse(plan['ok'])
        self.assertTrue(any('变化' in error for error in plan['errors']))
        with self.assertRaises(ValueError):
            publisher.generate(self.candidate, self.settings(), ['bilibili'], root=self.publish)

    def test_settings_roundtrip_and_reject_broken_file(self):
        publisher.save_settings(self.candidate['id'], {'common': {'audio': 'original'}}, self.publish)
        data = publisher.load_settings(self.publish)
        self.assertEqual(data['tracks'][self.candidate['id']]['common']['audio'], 'original')
        path = publisher.settings_path(self.publish)
        path.write_text('{"schema_version": 2}', encoding='utf8')
        with self.assertRaises(ValueError):
            publisher.load_settings(self.publish)

    def test_mark_submitted_requires_a_generated_package(self):
        with self.assertRaises(ValueError):
            publisher.mark_submitted(self.candidate['id'], 'bilibili', root=self.publish)
        plans = publisher.preflight(self.candidate, self.settings(), ['bilibili'], root=self.publish)
        result = publisher.build_package(self.candidate, plans, 'bilibili', self.publish)
        publisher.record_generation(self.candidate['id'], 'bilibili', result, self.publish)
        record = publisher.mark_submitted(self.candidate['id'], 'bilibili',
                                          url='https://www.bilibili.com/video/BV1xx',
                                          bvid='BV1xx', note='测试', root=self.publish)
        self.assertTrue(record['submitted'])
        state = publisher.load_state(self.candidate['id'], self.publish)
        self.assertEqual(state['packages']['bilibili']['submitted']['bvid'], 'BV1xx')


if __name__ == '__main__':
    unittest.main()
