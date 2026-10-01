"""Lightweight YAMNet speech-versus-music scoring for concert review."""
from __future__ import annotations

import csv
import importlib.util
import importlib.metadata
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODEL_DIR = ROOT / "models" / "concert"
MODEL_PATH = MODEL_DIR / "yamnet.onnx"
LABELS_PATH = MODEL_DIR / "yamnet_class_map.csv"

SPEECH_LABELS = {
    "speech", "conversation", "narration, monologue",
    "male speech, man speaking", "female speech, woman speaking",
    "child speech, kid speaking",
}
MUSIC_LABELS = {
    "music", "singing", "vocal music", "choir", "a capella",
    "rock music", "heavy metal", "pop music", "background music",
    "musical instrument",
}


def classifier_key():
    """Invalidate score caches when model files/runtime availability changes."""
    if importlib.util.find_spec('onnxruntime') is None:
        return None
    if not MODEL_PATH.is_file() or not LABELS_PATH.is_file():
        return None
    try:
        runtime = importlib.metadata.version('onnxruntime')
    except importlib.metadata.PackageNotFoundError:
        try:
            runtime = importlib.metadata.version('onnxruntime-gpu')
        except importlib.metadata.PackageNotFoundError:
            runtime = 'unknown'
    return {'runtime': runtime,
            'files': [[path.name, path.stat().st_size, path.stat().st_mtime_ns]
                      for path in (MODEL_PATH, LABELS_PATH)]}


def load_classifier(model_path: Path = MODEL_PATH, labels_path: Path = LABELS_PATH):
    """Load the optional local ONNX model; return None when it is not installed."""
    if not model_path.is_file() or not labels_path.is_file():
        return None
    try:
        import onnxruntime as ort
        import numpy as np
    except ImportError:
        return None

    try:
        with labels_path.open("r", encoding="utf-8-sig", newline="") as stream:
            labels = list(csv.DictReader(stream))
        by_index = {int(row["index"]): row["display_name"].strip().lower() for row in labels}
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError('YAMNet 类别文件损坏，请重新下载模型') from exc
    speech = [i for i, name in by_index.items() if name in SPEECH_LABELS]
    music = [i for i, name in by_index.items() if name in MUSIC_LABELS]
    if not speech or not music or min(by_index) < 0:
        raise ValueError("YAMNet 类别文件不完整，请重新下载演唱会音频分类模型")

    options = ort.SessionOptions()
    options.intra_op_num_threads = 2
    options.inter_op_num_threads = 1
    try:
        session = ort.InferenceSession(str(model_path), sess_options=options,
                                       providers=["CPUExecutionProvider"])
    except Exception as exc:
        raise ValueError(f"无法载入 YAMNet 演唱会音频分类模型：{exc}") from exc
    input_name = session.get_inputs()[0].name

    def score(waveform):
        values = np.asarray(waveform, dtype=np.float32).reshape(-1)
        if not values.size:
            return np.empty(0, np.float32), np.empty(0, np.float32)
        try:
            outputs = session.run(None, {input_name: values})[0]
        except Exception as exc:
            raise ValueError(f'YAMNet 分类失败：{exc}') from exc
        if (outputs.ndim != 2 or outputs.shape[1] <= max(speech + music) or
                not np.all(np.isfinite(outputs))):
            raise ValueError('YAMNet 分类结果无效，请重新下载模型')
        return outputs[:, speech].max(axis=1), outputs[:, music].max(axis=1)

    return score
