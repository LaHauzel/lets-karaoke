"""Already-timed lyrics can render on CPU without separation or AI models."""
import contextlib
import io
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
                            '-i', 'color=c=black:s=320x180:r=10:d=1', '-an', '-c:v', 'libx264', str(video)],
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
                    contextlib.redirect_stdout(io.StringIO())):
                result = run(config)
            self.assertTrue(result.ok, result.error)
            rendered = probe_media(result.video)
            self.assertTrue(rendered['has_video'])
            self.assertTrue(rendered['has_audio'])
            self.assertFalse(result.stats['separated'])

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
                    contextlib.redirect_stdout(io.StringIO())):
                result=run(config)
            self.assertTrue(result.ok,result.error)
            self.assertTrue(Path(result.video).is_file())
            self.assertFalse(result.stats['separated'])


if __name__=='__main__':
    unittest.main()
