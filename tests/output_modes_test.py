"""Verify real subtitle rendering/muxing, duration, audio and saved versions."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import pipeline as pp
from ass_builder import AssOptions, KaraokeLine, KaraokeToken, build_ass, solve_windows
from local_history import describe


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg unavailable')
class OutputModesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.source = cls.root / 'source.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                        'color=black:s=320x180:r=25:d=2', '-f', 'lavfi', '-i',
                        'sine=frequency=440:duration=2', '-c:v', 'libx264', '-c:a', 'aac',
                        '-shortest', str(cls.source)], check=True)
        cls.audio = cls.root / 'other.wav'
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                        'sine=frequency=880:duration=2', str(cls.audio)], check=True)
        opt = AssOptions(font='Arial', font_size=80, next_line=False)
        lines = [KaraokeLine(raw='TEST', start=.4, end=1.1,
                            tokens=[KaraokeToken('TEST', 'TEST', .4, 1.1)])]
        solve_windows(lines, opt)
        (cls.root/'karaoke.ass').write_text(build_ass(lines, opt, 320, 180), encoding='utf-8-sig')
        cls.info = pp.probe_media(cls.source)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def probe(self, path):
        return json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_streams',
                                                   '-show_format', '-of', 'json', str(path)]))

    def test_soft_contains_ass_keeps_video_and_music_after_final_subtitle(self):
        target = self.root/'soft.mkv'
        used = pp.render_video(self.source, 'karaoke.ass', self.root, target, None,
                               self.info, 'x264', 21, True, output_mode='soft')
        self.assertEqual(used, 'copy')
        d = self.probe(target)
        self.assertEqual(sorted(s['codec_type'] for s in d['streams']), ['audio', 'subtitle', 'video'])
        sub = next(s for s in d['streams'] if s['codec_type'] == 'subtitle')
        self.assertEqual(sub['codec_name'], 'ass')
        self.assertEqual(sub['disposition']['default'], 1)
        self.assertGreater(float(d['format']['duration']), 1.9)
        def video_hash(path):
            return subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(path), '-map',
                                            '0:v:0', '-c', 'copy', '-f', 'hash', '-hash', 'sha256', '-'])
        self.assertEqual(video_hash(self.source), video_hash(target))

    def test_burn_has_visible_text_and_no_subtitle_track(self):
        target = self.root/'burn.mp4'
        pp.render_video(self.source, 'karaoke.ass', self.root, target, None, self.info, 'x264', 21, True)
        self.assertEqual(sorted(s['codec_type'] for s in self.probe(target)['streams']), ['audio', 'video'])
        frame = subprocess.check_output(['ffmpeg', '-v', 'error', '-ss', '0.7', '-i', str(target),
                                         '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'])
        self.assertGreater(sum(v > 100 for v in frame), 100, 'Burned lyric must be visible on a black frame')

    def test_soft_audio_only_is_finite_and_selects_replacement_audio(self):
        import numpy as np
        target = self.root/'audio.mkv'
        info = pp.probe_media(self.audio)
        pp.render_video(self.audio, 'karaoke.ass', self.root, target, None, info,
                        'x264', 21, output_mode='soft')
        d = self.probe(target)
        self.assertAlmostEqual(float(d['format']['duration']), 2., delta=.1)
        target = self.root/'replacement.mkv'
        pp.render_video(self.source, 'karaoke.ass', self.root, target, str(self.audio),
                        self.info, 'x264', 21, output_mode='soft')
        raw = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(target), '-map', '0:a:0',
                                       '-t', '1', '-ac', '1', '-ar', '16000', '-f', 'f32le', '-'])
        samples = np.frombuffer(raw, dtype='<f4')
        peak = np.argmax(abs(np.fft.rfft(samples))) * 16000 / len(samples)
        self.assertAlmostEqual(peak, 880, delta=5)

    def test_timed_pipeline_persists_mode_and_history_recovers_mkv(self):
        result = pp.run(pp.PipelineConfig(media=str(self.source), lang='en', separate=False,
            vocal_mode='keep', output_mode='soft', out_dir=str(self.root/'jobs'), job_name='mode',
            lyrics_text='[00:00.40]<00:00.40~00:01.10>TEST', device='cpu', encoder='x264'))
        self.assertTrue(result.ok, result.error)
        self.assertEqual(Path(result.video).suffix, '.mkv')
        directory = Path(result.job_dir)
        self.assertEqual(pp.load_job(directory)[0]['output_mode'], 'soft')
        self.assertEqual(describe(directory, self.root)['result']['stats']['output_mode'], 'soft')
        inherited = pp.restyle(directory, pp.RestyleRequest())
        self.assertEqual(Path(inherited['video']).suffix, '.mkv')
        self.assertEqual(inherited['encoder'], 'copy')
        self.assertEqual(pp.load_job(directory, inherited['version'])[0]['encoder_choice'], 'x264')
        self.assertEqual(describe(directory, self.root)['versions'][-1]['video'], Path(inherited['video']).name)
        changed = pp.restyle(directory, pp.RestyleRequest(base_version=inherited['version'], output_mode='burn'))
        self.assertEqual(Path(changed['video']).suffix, '.mp4')
        self.assertEqual(pp.load_job(directory, changed['version'])[0]['output_mode'], 'burn')
        self.assertTrue(Path(result.video).exists(), 'Switching mode must preserve the original output')

    def test_rejects_unknown_mode_and_wrong_container_before_rendering(self):
        with self.assertRaises(ValueError):
            pp.render_video(self.source, 'karaoke.ass', self.root, self.root/'bad.mp4',
                            None, self.info, output_mode='soft')
        with self.assertRaises(ValueError):
            pp.normalize_output_mode('anything')


if __name__ == '__main__':
    unittest.main()
