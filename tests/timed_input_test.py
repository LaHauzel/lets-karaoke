"""Already-timed lyrics can render on CPU without separation or AI models."""
import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import wave
import numpy as np
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from pipeline import PipelineConfig, run, render_video, probe_media


class EncoderSelectionTests(unittest.TestCase):
    def test_unknown_encoder_is_rejected_without_touching_completed_video(self):
        with tempfile.TemporaryDirectory() as folder:
            output=Path(folder)/'completed.mp4'; output.write_bytes(b'completed')
            with patch('pipeline.run_process',side_effect=AssertionError('Must reject before FFmpeg')):
                with self.assertRaisesRegex(ValueError,'未知视频编码器'):
                    render_video('unused','unused.ass',Path(folder),output,None,{},encoder='unknown')
            self.assertEqual(output.read_bytes(),b'completed')


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'FFmpeg is required')
class TimedInputTests(unittest.TestCase):
    def test_silent_video_uses_supplied_audio_track_without_ai_models(self):
        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / 'silent.mp4'
            subprocess.run(['ffmpeg', '-y', '-nostdin', '-v', 'error', '-f', 'lavfi',
                            '-i', 'color=c=black:s=320x180:r=10:d=2', '-an', '-c:v', 'libx264', str(video)],
                           check=True, capture_output=True, timeout=30)
            self.assertFalse(probe_media(video)['has_audio'])
            audio = Path(folder) / 'independent.wav'
            with wave.open(str(audio), 'wb') as stream:
                stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(16000)
                stream.writeframes(np.zeros(16000, dtype=np.int16).tobytes())
            config = PipelineConfig(media=str(video), audio_track=str(audio),
                lyrics_text='[00:00.100]<00:00.100~00:00.800>test', lang='en',
                vocal_mode='keep', device='cpu', encoder='libx264', out_dir=folder, job_name='independent')
            with (patch('pipeline.separate_stems', side_effect=AssertionError('Use the supplied audio')),
                    patch('pipeline.ModelCache.aligner', side_effect=AssertionError('Use the supplied timestamps')),
                    patch('pipeline.extract_wav', side_effect=AssertionError('Do not create full PCM copies')),
                    patch('pipeline.load_audio_16k', side_effect=AssertionError('Do not load an unused waveform')),
                    contextlib.redirect_stdout(io.StringIO())):
                result = run(config)
            self.assertTrue(result.ok, result.error)
            rendered = probe_media(result.video)
            self.assertTrue(rendered['has_video'])
            self.assertTrue(rendered['has_audio'])
            self.assertFalse(result.stats['separated'])
            self.assertEqual(result.stats['audio_sec'], 1.0)
            self.assertAlmostEqual(rendered['duration'], 1.0, delta=.15)
            saved = json.loads((Path(result.job_dir)/'job.json').read_text(encoding='utf-8'))
            self.assertEqual(saved['out_audio'], str(audio.resolve()))

    def test_enhanced_lrc_keep_renders_without_ai_models(self):
        with tempfile.TemporaryDirectory() as folder:
            audio=Path(folder)/'input.wav'
            with wave.open(str(audio),'wb') as stream:
                stream.setnchannels(1);stream.setsampwidth(2);stream.setframerate(16000)
                stream.writeframes(np.zeros(16000,dtype=np.int16).tobytes())
            config=PipelineConfig(media=str(audio),lyrics_text='[00:00.100]<00:00.100~00:00.800>test',
                lang='en',separate=True,vocal_mode='keep',device='cpu',encoder='libx264',
                out_dir=folder,job_name='timed')
            with (patch('pipeline.separate_stems',side_effect=AssertionError('Demucs must not be needed')),
                    patch('pipeline.ModelCache.aligner',side_effect=AssertionError('An aligner must not be needed')),
                    patch('pipeline.extract_wav',side_effect=AssertionError('Do not create full PCM copies')),
                    patch('pipeline.load_audio_16k',side_effect=AssertionError('Do not load an unused waveform')),
                    contextlib.redirect_stdout(io.StringIO())):
                result=run(config)
            self.assertTrue(result.ok,result.error)
            self.assertTrue(Path(result.video).is_file())
            self.assertFalse(result.stats['separated'])
            directory = Path(result.job_dir)
            self.assertFalse((directory/'source_audio.wav').exists())
            self.assertFalse((directory/'align_input.wav').exists())
            saved = json.loads((directory/'job.json').read_text(encoding='utf-8'))
            self.assertEqual(saved['align_source'], str(audio.resolve()))
            from lyric_waveform import waveform
            self.assertTrue(waveform(directory)['peaks'])

    def test_ignoring_word_times_still_decodes_audio_for_the_aligner(self):
        from p0_common import TokenSpan
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as folder:
            audio = Path(folder)/'input.wav'
            with wave.open(str(audio), 'wb') as stream:
                stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(16000)
                stream.writeframes(np.zeros(16000, dtype=np.int16).tobytes())
            received = []
            def align(samples, tokens, lang):
                received.append(samples)
                return [TokenSpan('test', .2, .7)]
            config = PipelineConfig(media=str(audio), lyrics_text='[00:00.100]<00:00.100~00:00.800>test',
                lang='en', timed_mode='ignore', separate=False, vocal_mode='keep',
                device='cpu', encoder='x264', out_dir=folder, job_name='realign')
            with patch('pipeline.ModelCache.aligner', return_value=SimpleNamespace(align=align)), \
                    contextlib.redirect_stdout(io.StringIO()):
                result = run(config)
            self.assertTrue(result.ok, result.error)
            self.assertEqual(len(received[0]), 16000)
            self.assertTrue((Path(result.job_dir)/'align_input.wav').exists())


if __name__=='__main__':
    unittest.main()
