"""P0 验证 · 对齐后端与人声分离封装

后端：
  Wav2Vec2Aligner  —— 用 transformers 加载语言相关 CTC 模型 + torchaudio 的
                      CTC forced_align 做逐音素对齐。这是 WhisperX 内部同款
                      原理（wav2vec2 声学对齐），但省掉了 whisperx/pyannote 的
                      重依赖，便于在 py3.13 上跑通。
  QwenAligner      —— Qwen3-ForcedAligner-0.6B（qwen-asr）。API 在首次运行时
                      自动探测，适配器对多种调用签名做兼容。

分离：
  DemucsSeparator  —— htdemucs / htdemucs_ft，输出干人声与伴奏。
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np

from p0_common import SR, TokenSpan


# ==========================================================================
# wav2vec2 CTC 强制对齐
# ==========================================================================


class Wav2Vec2Aligner:
    """语言相关 wav2vec2 CTC 模型 + torchaudio.functional.forced_align。

    模型词表粒度可能是「字符」或「子词」，因此这里做一层 unit 映射：
    把原 token 拆成模型单元，对齐后按 owner 聚合成原 token 的 span。
    """

    def __init__(self, model_name: str, device: str = "cuda", cache_dir: str | None = None):
        import torch
        from transformers import Wav2Vec2CTCTokenizer, Wav2Vec2FeatureExtractor, Wav2Vec2ForCTC

        self.name = f"wav2vec2:{model_name.split('/')[-1]}"
        self.device = device
        self.model_name = model_name
        # local_files_only：权重已在 cache_dir 里，禁止再去 huggingface.co 做 HEAD
        # 校验。联网校验在网络不通时会重试 5 次、每次 read timeout 10s，白白拖慢数分钟。
        kw = {"local_files_only": True}
        if cache_dir:
            kw["cache_dir"] = cache_dir
        # 显式加载 feature extractor + tokenizer，不用 AutoProcessor：
        # jonatasgrosman 系列的 processor_class 可能是 Wav2Vec2ProcessorWithLM，
        # 会引入 pyctcdecode 依赖。强制对齐只需要声学模型 + 词表，不需要语言模型。
        self.fe = Wav2Vec2FeatureExtractor.from_pretrained(model_name, **kw)
        self.tok = Wav2Vec2CTCTokenizer.from_pretrained(model_name, **kw)
        self.model = Wav2Vec2ForCTC.from_pretrained(model_name, **kw).to(device).eval()
        self.blank = int(getattr(self.model.config, "pad_token_id", 0) or 0)
        self.unk = self.tok.unk_token_id
        self.oov = 0
        self.total = 0

    # -- 词表映射 ---------------------------------------------------------
    def _one(self, s: str) -> int | None:
        for cand in (s, s.upper(), s.lower()):
            if not cand:
                continue
            try:
                i = self.tok.convert_tokens_to_ids(cand)
            except Exception:
                continue
            if isinstance(i, int) and i >= 0 and (self.unk is None or i != self.unk):
                return i
        return None

    def encode(self, tokens: list[str]) -> tuple[list[int], list[int]]:
        ids: list[int] = []
        owner: list[int] = []
        self.oov = 0
        self.total = len(tokens)
        for ti, t in enumerate(tokens):
            got = self._one(t)
            if got is None:
                got = None
                seq = []
                ok = True
                for ch in t:
                    j = self._one(ch)
                    if j is None:
                        ok = False
                        break
                    seq.append(j)
                got = seq if ok and seq else None
            if got is None:
                self.oov += 1
                continue
            if isinstance(got, int):
                got = [got]
            ids.extend(got)
            owner.extend([ti] * len(got))
        return ids, owner

    # -- 对齐 -------------------------------------------------------------
    def align(self, audio: np.ndarray, tokens: list[str], lang: str = "") -> list[TokenSpan]:
        import torch
        import torchaudio
        from torchaudio.functional import forced_align, merge_tokens

        ids, owner = self.encode(tokens)
        if not ids:
            raise RuntimeError("no token survived vocab mapping (all OOV)")

        inputs = self.fe(audio, sampling_rate=SR, return_tensors="pt", padding=False)
        wav_t = inputs.input_values.to(self.device)
        with torch.no_grad():
            logits = self.model(wav_t).logits
        log_probs = torch.log_softmax(logits.float(), dim=-1)

        targets = torch.tensor([ids], dtype=torch.int32, device=self.device)
        aligned, scores = forced_align(log_probs, targets, blank=self.blank)
        spans = merge_tokens(aligned[0].to("cpu"), scores[0].to("cpu"))

        if len(spans) != len(ids):
            raise RuntimeError(f"forced_align span/unit mismatch: {len(spans)} vs {len(ids)}")

        n_samples = wav_t.shape[-1]
        # logits 形状是 (batch, time, num_classes)：时间维在 axis=1，
        # axis=-1 是词表大小。用 axis=-1 会让帧率换算错上十几倍。
        n_frames = log_probs.shape[1]
        ratio = n_samples / n_frames / SR

        out: list[TokenSpan] = []
        for span, oi in zip(spans, owner):
            out.append(TokenSpan(text=tokens[oi], start=span.start * ratio, end=span.end * ratio))

        # 同 token 的多个 unit -> 合并为单一 span（保留首次出现顺序）
        merged: list[TokenSpan] = []
        for ts in out:
            if merged and merged[-1].text == ts.text:
                merged[-1].end = max(merged[-1].end, ts.end)
            else:
                merged.append(TokenSpan(text=ts.text, start=ts.start, end=ts.end))
        return merged


# ==========================================================================
# Qwen3-ForcedAligner
# ==========================================================================


class QwenAligner:
    """Qwen3-ForcedAligner-0.6B 适配器。

    首次实例化时探测 qwen-asr 暴露的类与调用签名，并缓存结果。
    """

    _api_cache: dict | None = None

    def __init__(self, model_dir: str | None = None, device: str = "cuda", model_id: str = "Qwen/Qwen3-ForcedAligner-0.6B"):
        import inspect

        import qwen_asr

        self.name = "qwen3-forcedaligner-0.6b"
        self.device = device
        self.model_id = model_dir or model_id
        self._qwen_asr = qwen_asr

        if QwenAligner._api_cache is None:
            QwenAligner._api_cache = self._probe_api()
        self.api = QwenAligner._api_cache

        cls = getattr(qwen_asr, self.api["class_name"])
        try:
            self.model = cls.from_pretrained(self.model_id, device_map=device)
        except TypeError:
            self.model = cls.from_pretrained(self.model_id)
            if hasattr(self.model, "to"):
                self.model.to(device)
        if hasattr(self.model, "eval"):
            self.model.eval()
        self._inspect = inspect

    def _probe_api(self) -> dict:
        """探测可用的对齐类与调用签名。"""
        import inspect

        q = self._qwen_asr
        names = [n for n in dir(q) if not n.startswith("_")]
        cand = None
        for n in ("ForcedAligner", "ForcedAlignModel", "Qwen3ForcedAligner", "Aligner"):
            if hasattr(q, n):
                cand = n
                break
        if cand is None:
            cand = next((n for n in names if "align" in n.lower()), None)
        if cand is None:
            raise RuntimeError(f"qwen_asr 未暴露对齐类，可用符号: {names}")

        cls = getattr(q, cand)
        meths = [m for m in dir(cls) if not m.startswith("_")]
        sig = ""
        for m in ("align", "forced_align", "predict", "forward"):
            if hasattr(cls, m):
                try:
                    sig = f"{m}{inspect.signature(getattr(cls, m))}"
                except Exception:
                    sig = m
                break
        return {"class_name": cand, "symbols": names, "methods": meths, "call_sig": sig}

    def align(self, audio: np.ndarray, tokens: list[str], lang: str = "") -> list[TokenSpan]:
        text = " ".join(tokens) if lang == "en" else "".join(tokens)
        lang_map = {"zh": "Chinese", "en": "English", "ja": "Japanese"}
        lang_arg = lang_map.get(lang, "Chinese")

        # qwen_asr 的 align() 只接受三种 audio 形态：
        #   str 路径 / (ndarray, sample_rate) 元组 / 上述两者的列表
        # 传裸 ndarray 会抛 "Unsupported audio input type"。
        audio_arg = (np.asarray(audio, dtype=np.float32), SR)

        m = self.model
        fn = None
        for cand in ("align", "forced_align", "predict"):
            if hasattr(m, cand):
                fn = getattr(m, cand)
                break
        if fn is None:
            raise RuntimeError(f"Qwen 对齐模型无可调用方法，可用: {self.api['methods']}")

        # 依次尝试若干调用签名
        attempts = [
            lambda: fn(audio_arg, text, language=lang_arg),
            lambda: fn(audio_arg, text, lang_arg),
            lambda: fn(audio=audio_arg, text=text, language=lang_arg),
            lambda: fn(audio_arg, text),
        ]
        last = None
        for k, a in enumerate(attempts):
            try:
                res = a()
                return self._normalize(res, tokens)
            except TypeError as e:
                last = e
                continue
            except Exception as e:
                last = e
                raise RuntimeError(f"Qwen 对齐调用失败（签名 #{k}）: {type(e).__name__}: {e}") from e
        raise RuntimeError(f"Qwen 对齐所有调用签名均失败，最后错误: {last}")

    def _normalize(self, res, tokens: list[str]) -> list[TokenSpan]:
        """把 qwen_asr 的返回结构归一成 TokenSpan 列表。

        实测结构（Qwen3-ForcedAligner-0.6B / qwen-asr）：
          align() -> List[ForcedAlignResult]
          ForcedAlignResult.items -> List[ForcedAlignItem]
          ForcedAlignItem(text: str, start_time: float(秒), end_time: float(秒))
        注意 start_time/end_time 的类型注解写的是 int，实际是秒为单位的 float。
        """
        raw = res if isinstance(res, (list, tuple)) else [res]

        items: list = []
        for r in raw:
            if isinstance(r, dict) and "items" in r:
                items.extend(list(r["items"]))
            elif hasattr(r, "items") and not isinstance(r, dict):
                items.extend(list(r.items))
            else:
                items.append(r)

        out: list[TokenSpan] = []
        for it in items:
            if isinstance(it, (list, tuple)) and len(it) >= 3:
                txt, st, en = it[0], float(it[1]), float(it[2])
            elif isinstance(it, dict):
                txt = it.get("text") or it.get("word") or it.get("token") or ""
                st = float(it.get("start_time", it.get("start", 0.0)))
                en = float(it.get("end_time", it.get("end", st)))
            else:
                txt = str(getattr(it, "text", ""))
                st = float(getattr(it, "start_time", getattr(it, "start", 0.0)))
                en = float(getattr(it, "end_time", getattr(it, "end", st)))
            out.append(TokenSpan(text=txt.strip(), start=st, end=en))
        return out


# ==========================================================================
# 人声分离
# ==========================================================================


class DemucsSeparator:
    """htdemucs / htdemucs_ft 人声分离。输入输出统一 16 kHz 单声道。"""

    def __init__(self, model_name: str = "htdemucs_ft", device: str = "cuda"):
        import torch
        from demucs.pretrained import get_model

        self.name = model_name
        self.device = device
        self.model = get_model(model_name)
        self.model.to(device).eval()
        self.sr = int(self.model.samplerate)
        self._torch = torch

    def separate_vocals(self, audio16k: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """返回 (vocals, accompaniment)，均为 16 kHz 单声道。"""
        import torch
        import torchaudio.functional as AF

        torch = self._torch
        wav = torch.from_numpy(audio16k).float()
        if self.sr != SR:
            wav = AF.resample(wav, SR, self.sr)

        wav = wav.unsqueeze(0).repeat(self.model.audio_channels, 1)
        ref = wav.mean(0)
        mean, std = ref.mean(), ref.std()
        wav_n = (wav - mean) / (std + 1e-8)

        from demucs.apply import apply_model

        with torch.no_grad():
            est = apply_model(
                self.model,
                wav_n[None].to(self.device),
                device=self.device,
                shifts=1,
                split=True,
                overlap=0.25,
                progress=False,
            )[0]
        est = est * std + mean
        est = est.to("cpu")

        idx = list(self.model.sources).index("vocals") if "vocals" in self.model.sources else 3
        vocals = est[idx]
        accomp = torch.stack([est[i] for i in range(est.shape[0]) if i != idx]).sum(0)

        v = vocals.mean(0)
        a = accomp.mean(0)
        if self.sr != SR:
            v = AF.resample(v, self.sr, SR)
            a = AF.resample(a, self.sr, SR)
        return v.numpy().astype(np.float32), a.numpy().astype(np.float32)


def timed(fn, *a, **kw):
    t0 = time.perf_counter()
    r = fn(*a, **kw)
    return r, time.perf_counter() - t0
