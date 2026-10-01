"""Already-timed lyrics can render on CPU without separation or AI models."""
import contextlib
import io
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import wave
import numpy as np
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from pipeline import PipelineConfig, run


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'FFmpeg is required')
class TimedInputTests(unittest.TestCase):
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
