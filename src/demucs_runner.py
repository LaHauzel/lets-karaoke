"""Run Demucs with SoundFile output on torchaudio 2.9+ (no TorchCodec required)."""
from pathlib import Path


def save_audio(path, tensor, sample_rate, channels_first=True,
               encoding=None, bits_per_sample=16, **kwargs):
    import soundfile as sf
    data = tensor.detach().cpu().numpy()
    if channels_first:
        data = data.T
    subtype = 'FLOAT' if encoding == 'PCM_F' else f'PCM_{bits_per_sample or 16}'
    if Path(path).suffix.lower() not in ('.wav', '.flac'):
        raise ValueError('Demucs runner supports WAV/FLAC output only')
    sf.write(str(path), data, sample_rate, subtype=subtype)


if __name__ == '__main__':
    import model_paths  # Set project-local model cache before importing Demucs.
    import torchaudio
    torchaudio.save = save_audio  # Scoped to this dedicated child process.
    from demucs.separate import main
    main()
