"""
Deploy NVIDIA Parakeet ASR on Modal, following the structure of
https://github.com/modal-projects/modal-nvidia-asr with a simpler batch HTTP API.

Default model:
  - nvidia/parakeet-tdt-0.6b-v3

Usage:
  cd parakeet-modal
  uv sync
  uv run modal deploy deploy_parakeet_modal.py

  # print deployed URL
  uv run modal run deploy_parakeet_modal.py

  PARAKEET_API_URL=https://...--parakeet-transcription-web.modal.run \
    uv run python transcribe_client.py sample.wav

Environment:
  PARAKEET_MODEL              default: nvidia/parakeet-tdt-0.6b-v3
  MAX_AUDIO_SECONDS           default: 21600
  CHUNK_SECONDS               default: 300
  BATCH_SIZE                  default: 1
  HF_TOKEN                    optional HF token for gated / rate-limited downloads
  DIARIZATION_BACKEND         default: clustering
  DIARIZATION_SORTFORMER_MODEL default: nvidia/diar_sortformer_4spk-v1
  DIARIZATION_MODEL           legacy alias for DIARIZATION_SORTFORMER_MODEL
  DIARIZATION_VAD_MODEL       default: vad_multilingual_marblenet
  DIARIZATION_SPEAKER_MODEL   default: titanet_large
  MAX_SORTFORMER_AUDIO_SECONDS default: 120
  MAX_DIARIZATION_SECONDS     default: MAX_AUDIO_SECONDS
  WARMUP_ON_START             default: true

Endpoints:
  GET  /health
  POST /transcribe    multipart form-data with `file`

Request fields:
  file                    required audio file
  timestamp_level         optional: word | segment | char
  return_word_confidence  optional bool, default true
  diarize                 optional bool, default false (requires word timestamps)
  diarization_backend     optional: clustering | sortformer

Notes:
  - Audio is normalized to mono 16 kHz PCM with soundfile + ffmpeg fallback.
  - Long files are split into fixed-size chunks, transcribed in batches, then merged.
  - Optional speaker diarization uses NeMo ClusteringDiarizer or offline Sortformer, then aligns words to speakers.
  - Sortformer is limited to short-form audio; use clustering for long-form diarization.
  - This is a batch HTTP endpoint, not streaming WebSocket ASR.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import io
import json
import logging
import math
import os
import re
import subprocess
import tempfile
import threading
import wave
from dataclasses import dataclass
from typing import Any, Literal, Optional

import modal

APP_NAME = "parakeet-transcription"
MODEL = os.environ.get("PARAKEET_MODEL", "nvidia/parakeet-tdt-0.6b-v3")
PARAKEET_INFERENCE_DTYPE = os.environ.get("PARAKEET_INFERENCE_DTYPE", "float32").strip().lower()
DIARIZATION_BACKEND = os.environ.get("DIARIZATION_BACKEND", "clustering").strip().lower()
DIARIZATION_SORTFORMER_MODEL = os.environ.get(
    "DIARIZATION_SORTFORMER_MODEL",
    os.environ.get("DIARIZATION_MODEL", "nvidia/diar_sortformer_4spk-v1"),
)
DIARIZATION_STREAMING_SORTFORMER_MODEL = os.environ.get(
    "DIARIZATION_STREAMING_SORTFORMER_MODEL",
    "nvidia/diar_streaming_sortformer_4spk-v2.1",
)
DIARIZATION_VAD_MODEL = os.environ.get("DIARIZATION_VAD_MODEL", "vad_multilingual_marblenet")
DIARIZATION_SPEAKER_MODEL = os.environ.get("DIARIZATION_SPEAKER_MODEL", "titanet_large")
MAX_SORTFORMER_AUDIO_SECONDS = int(os.environ.get("MAX_SORTFORMER_AUDIO_SECONDS", "120"))
CACHE_DIR = "/cache"
SAMPLE_RATE = 16000
STREAMING_SORTFORMER_FRAME_LEN_SECONDS = 0.08
MINUTES = 60
SUPPORTED_DIARIZATION_BACKENDS = frozenset({"clustering", "sortformer"})

app = modal.App(APP_NAME)
model_cache = modal.Volume.from_name("parakeet-model-cache", create_if_missing=True)

image = (
    modal.Image.from_registry(
        "nvidia/cuda:12.8.0-cudnn-devel-ubuntu22.04",
        add_python="3.12",
    )
    .entrypoint([])
    .env(
        {
            "HF_HUB_ENABLE_HF_TRANSFER": "1",
            "HF_HOME": CACHE_DIR,
            "TORCH_HOME": CACHE_DIR,
            "DEBIAN_FRONTEND": "noninteractive",
            "CXX": "g++",
            "CC": "g++",
        }
    )
    .apt_install("ffmpeg", "git")
    .uv_pip_install(
        "Cython",
        "hf_transfer==0.1.9",
        "huggingface_hub[hf-xet]==0.31.2",
        "cuda-python==12.8.0",
        "numpy<2",
        "packaging",
        "soundfile",
        "resampy",
        "fastapi[standard]",
    )
    .run_commands(
        "python -m pip install --no-cache-dir 'git+https://github.com/NVIDIA/NeMo.git@main#egg=nemo_toolkit[asr]'",
        # Install torch/torchaudio LAST and force-reinstall from the CUDA 12.8 wheel index.
        # This avoids transitive deps replacing them with mismatched builds that look for
        # the wrong CUDA runtime (e.g. libcudart.so.13).
        "python -m pip install --no-cache-dir --force-reinstall --no-deps --index-url https://download.pytorch.org/whl/cu128 torch==2.8.0 torchaudio==2.8.0"
    )
)

with image.imports():
    import numpy as np
    import resampy
    import soundfile as sf
    import torch
    import nemo.collections.asr as nemo_asr
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import JSONResponse
    from nemo.collections.asr.models import ClusteringDiarizer
    from nemo.collections.asr.modules.sortformer_modules import StreamingSortformerState
    from nemo.collections.asr.parts.submodules.rnnt_decoding import RNNTDecodingConfig
    from nemo.collections.asr.parts.utils.asr_confidence_utils import ConfidenceConfig
    from omegaconf import OmegaConf


class NoStdStreams:
    def __init__(self):
        self.devnull = open(os.devnull, "w")

    def __enter__(self):
        import sys

        self._stdout, self._stderr = sys.stdout, sys.stderr
        self._stdout.flush(), self._stderr.flush()
        sys.stdout, sys.stderr = self.devnull, self.devnull

    def __exit__(self, exc_type, exc_value, traceback):
        import sys

        sys.stdout, sys.stderr = self._stdout, self._stderr
        self.devnull.close()


def preprocess_audio(audio: bytes | str, target_sample_rate: int = SAMPLE_RATE, return_tensor: bool = False):
    waveform = decode_audio(audio, target_sample_rate=target_sample_rate)
    if return_tensor:
        return torch.from_numpy(waveform).to("cuda")

    return waveform_to_pcm16_bytes(waveform)


def decode_audio(audio: bytes | str, target_sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    if isinstance(audio, str):
        if audio.startswith("http://") or audio.startswith("https://"):
            from urllib.request import urlopen

            audio = urlopen(audio).read()
        else:
            with open(audio, "rb") as f:
                audio = f.read()

    try:
        waveform, sample_rate = sf.read(io.BytesIO(audio), dtype="float32")
    except Exception:
        with tempfile.NamedTemporaryFile(suffix=".input", delete=False) as tmp_in:
            tmp_in.write(audio)
            tmp_in_path = tmp_in.name
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_out:
            tmp_out_path = tmp_out.name
        try:
            proc = subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-y",
                    "-i",
                    tmp_in_path,
                    "-ar",
                    str(target_sample_rate),
                    "-ac",
                    "1",
                    "-f",
                    "wav",
                    tmp_out_path,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            if proc.returncode != 0:
                raise ValueError(proc.stderr)
            waveform, sample_rate = sf.read(tmp_out_path, dtype="float32")
        except Exception as e:
            raise ValueError(f"Failed to decode audio: {e}") from e
        finally:
            try:
                os.unlink(tmp_in_path)
            except OSError:
                pass
            try:
                os.unlink(tmp_out_path)
            except OSError:
                pass

    if getattr(waveform, "ndim", 1) > 1:
        waveform = np.mean(waveform, axis=1)
    if sample_rate != target_sample_rate:
        waveform = resampy.resample(waveform, sample_rate, target_sample_rate)

    return np.asarray(waveform, dtype=np.float32).flatten()


def waveform_to_pcm16_bytes(waveform: np.ndarray) -> bytes:
    waveform_int16 = (waveform * 32767).astype(np.int16)
    return waveform_int16.tobytes()


def _write_pcm16_wav(path: str, data: bytes) -> None:
    with wave.open(path, "wb") as wav_out:
        wav_out.setnchannels(1)
        wav_out.setsampwidth(2)
        wav_out.setframerate(SAMPLE_RATE)
        wav_out.writeframes(data)


def write_pcm16_wav_temp(data: bytes, prefix: str = "parakeet-") -> str:
    temp_file = tempfile.NamedTemporaryFile(prefix=prefix, delete=False, suffix=".wav")
    _write_pcm16_wav(temp_file.name, data)
    temp_file.close()
    return temp_file.name


def write_wav_file(args: tuple[int, bytes]) -> str:
    idx, data = args
    return write_pcm16_wav_temp(data, prefix=f"parakeet-{idx}-")


def split_pcm16(audio_bytes: bytes, chunk_seconds: int) -> list[bytes]:
    bytes_per_second = SAMPLE_RATE * 2
    chunk_size = max(1, int(chunk_seconds)) * bytes_per_second
    return [audio_bytes[i : i + chunk_size] for i in range(0, len(audio_bytes), chunk_size)]


def _offset_word_level_info(word_level_info: list[Any], offset_seconds: float) -> list[list[Any]]:
    out: list[list[Any]] = []
    for item in word_level_info:
        if len(item) >= 3:
            shifted = [item[0], item[1] + offset_seconds, item[2] + offset_seconds]
            if len(item) > 3:
                shifted.extend(item[3:])
            out.append(shifted)
    return out


def _offset_words(words: list[dict[str, Any]], offset_seconds: float) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for word in words:
        out.append(
            {
                **word,
                "start": float(word["start"]) + offset_seconds,
                "end": float(word["end"]) + offset_seconds,
            }
        )
    return out


def _normalize_speaker_label(speaker: Any) -> str:
    if isinstance(speaker, str):
        match = re.search(r"(-?\d+)\s*$", speaker.strip())
        if match:
            return f"SPEAKER_{int(match.group(1)):02d}"
    try:
        return f"SPEAKER_{int(speaker):02d}"
    except (TypeError, ValueError):
        return str(speaker)


def _coerce_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_speaker_triplets(predicted_segments: Any) -> list[tuple[Any, Any, Any]]:
    if predicted_segments is None:
        return []

    if isinstance(predicted_segments, str):
        parts = predicted_segments.replace(",", " ").split()
        if len(parts) >= 3 and _coerce_float(parts[0]) is not None and _coerce_float(parts[1]) is not None:
            return [(parts[0], parts[1], parts[2])]
        return []

    if isinstance(predicted_segments, dict):
        start = predicted_segments.get("start")
        end = predicted_segments.get("end")
        speaker = predicted_segments.get("speaker", predicted_segments.get("speaker_id", predicted_segments.get("label")))
        if start is not None and end is not None and speaker is not None:
            return [(start, end, speaker)]
        return []

    if isinstance(predicted_segments, (list, tuple)):
        if (
            len(predicted_segments) >= 3
            and not isinstance(predicted_segments[0], (list, tuple, dict))
            and not isinstance(predicted_segments[1], (list, tuple, dict))
            and _coerce_float(predicted_segments[0]) is not None
            and _coerce_float(predicted_segments[1]) is not None
        ):
            return [(predicted_segments[0], predicted_segments[1], predicted_segments[2])]

        out: list[tuple[Any, Any, Any]] = []
        for item in predicted_segments:
            out.extend(_extract_speaker_triplets(item))
        return out

    return []


def _normalize_speaker_segments(predicted_segments: Any) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    for start, end, speaker in _extract_speaker_triplets(predicted_segments):
        start_float = _coerce_float(start)
        end_float = _coerce_float(end)
        if start_float is None or end_float is None:
            continue

        try:
            speaker_index: int | str = int(speaker)
        except (TypeError, ValueError):
            if isinstance(speaker, str):
                match = re.search(r"(-?\d+)\s*$", speaker.strip())
                speaker_index = int(match.group(1)) if match else str(speaker)
            else:
                speaker_index = str(speaker)
        segments.append(
            {
                "speaker": _normalize_speaker_label(speaker),
                "speaker_index": speaker_index,
                "start": start_float,
                "end": end_float,
            }
        )
    segments.sort(key=lambda segment: (segment["start"], segment["end"], str(segment["speaker"])))
    return segments


def _streaming_sortformer_probs_to_segments(
    speaker_probs: np.ndarray,
    *,
    frame_duration: float = STREAMING_SORTFORMER_FRAME_LEN_SECONDS,
    activation_threshold: float = 0.35,
) -> list[dict[str, Any]]:
    if speaker_probs.ndim != 2 or speaker_probs.shape[0] == 0:
        return []

    best_speaker = np.argmax(speaker_probs, axis=1)
    best_score = np.max(speaker_probs, axis=1)

    segments: list[dict[str, Any]] = []
    current_speaker: Optional[int] = None
    segment_start_idx: Optional[int] = None

    def flush(end_idx: int) -> None:
        nonlocal current_speaker, segment_start_idx
        if current_speaker is None or segment_start_idx is None or end_idx <= segment_start_idx:
            current_speaker = None
            segment_start_idx = None
            return
        segments.append(
            {
                "speaker": _normalize_speaker_label(current_speaker),
                "speaker_index": current_speaker,
                "start": segment_start_idx * frame_duration,
                "end": end_idx * frame_duration,
            }
        )
        current_speaker = None
        segment_start_idx = None

    for frame_idx, speaker_idx in enumerate(best_speaker.tolist()):
        active_speaker = int(speaker_idx) if float(best_score[frame_idx]) >= activation_threshold else None
        if active_speaker != current_speaker:
            flush(frame_idx)
            current_speaker = active_speaker
            segment_start_idx = frame_idx if active_speaker is not None else None

    flush(int(speaker_probs.shape[0]))
    return segments


def _load_rttm_speaker_segments(pred_rttm_dir: str) -> list[dict[str, Any]]:
    if not os.path.isdir(pred_rttm_dir):
        return []

    label_lines: list[str] = []
    for name in sorted(os.listdir(pred_rttm_dir)):
        if not name.endswith(".rttm"):
            continue
        rttm_path = os.path.join(pred_rttm_dir, name)
        with open(rttm_path, "r", encoding="utf-8") as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) < 8 or parts[0].upper() != "SPEAKER":
                    continue
                start = _coerce_float(parts[3])
                duration = _coerce_float(parts[4])
                speaker = parts[7]
                if start is None or duration is None:
                    continue
                label_lines.append(f"{start} {start + duration} {speaker}")

    return _normalize_speaker_segments(label_lines)


def _find_speaker_for_word(word: dict[str, Any], speaker_segments: list[dict[str, Any]]) -> Optional[str]:
    start = float(word["start"])
    end = float(word["end"])
    midpoint = (start + end) / 2.0

    for segment in speaker_segments:
        if float(segment["start"]) <= midpoint <= float(segment["end"]):
            return str(segment["speaker"])

    best_speaker: Optional[str] = None
    best_overlap = 0.0
    for segment in speaker_segments:
        overlap = min(end, float(segment["end"])) - max(start, float(segment["start"]))
        if overlap > best_overlap:
            best_overlap = overlap
            best_speaker = str(segment["speaker"])
    return best_speaker


def _assign_words_to_speakers(words: list[dict[str, Any]], speaker_segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for word in words:
        out.append({**word, "speaker": _find_speaker_for_word(word, speaker_segments)})
    return out


def _join_tokens(tokens: list[str]) -> str:
    text = ""
    for token in tokens:
        token = token.strip()
        if not token:
            continue
        if not text:
            text = token
        elif token[0] in ",.!?;:%)]}":
            text += token
        elif text[-1] in "([{":
            text += token
        else:
            text += f" {token}"
    return text


def _build_utterances(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    utterances: list[dict[str, Any]] = []
    current: Optional[dict[str, Any]] = None

    for word in words:
        text = str(word.get("text", "")).strip()
        if not text:
            continue

        speaker = word.get("speaker")
        start = float(word["start"])
        end = float(word["end"])

        if current is None or current["speaker"] != speaker:
            if current is not None:
                current["text"] = _join_tokens(current.pop("tokens"))
                utterances.append(current)
            current = {
                "speaker": speaker,
                "start": start,
                "end": end,
                "tokens": [text],
            }
            continue

        current["end"] = end
        current["tokens"].append(text)

    if current is not None:
        current["text"] = _join_tokens(current.pop("tokens"))
        utterances.append(current)

    return utterances


def _module_param_dtype(module: Any) -> Optional[torch.dtype]:
    try:
        return next(module.parameters()).dtype
    except (AttributeError, StopIteration, TypeError):
        nested = getattr(module, "diarizer", None)
        if nested is not None and nested is not module:
            return _module_param_dtype(nested)
        return None


def _module_autocast_context(module: Any):
    dtype = _module_param_dtype(module)
    if dtype in {torch.float16, torch.bfloat16}:
        return torch.autocast("cuda", enabled=True, dtype=dtype)
    return contextlib.nullcontext()


def _force_module_float32(module: Any) -> None:
    nested = getattr(module, "diarizer", None)
    if nested is not None and nested is not module:
        _force_module_float32(nested)
        return
    if hasattr(module, "float"):
        module.float()
    try:
        module.to(device="cuda", dtype=torch.float32)
    except TypeError:
        module.to("cuda")


def _parse_torch_dtype(name: str) -> torch.dtype:
    normalized = name.strip().lower()
    mapping = {
        "float32": torch.float32,
        "float": torch.float32,
        "fp32": torch.float32,
        "bfloat16": torch.bfloat16,
        "bf16": torch.bfloat16,
        "float16": torch.float16,
        "fp16": torch.float16,
        "half": torch.float16,
    }
    if normalized not in mapping:
        raise ValueError(
            f"Unsupported PARAKEET_INFERENCE_DTYPE={name!r}; use one of {sorted(mapping)}"
        )
    return mapping[normalized]


class _AudioBufferer:
    def __init__(self, sample_rate: int, buffer_size_in_secs: float):
        self.buffer_size = int(buffer_size_in_secs * sample_rate)
        self.sample_buffer = torch.zeros(self.buffer_size, dtype=torch.float32)

    def reset(self) -> None:
        self.sample_buffer.zero_()

    def update(self, audio: np.ndarray) -> None:
        if not isinstance(audio, torch.Tensor):
            audio = torch.from_numpy(audio)
        audio_size = audio.shape[0]
        if audio_size > self.buffer_size:
            raise ValueError(f"Frame size ({audio_size}) exceeds buffer size ({self.buffer_size})")
        shift = audio_size
        self.sample_buffer[:-shift] = self.sample_buffer[shift:].clone()
        self.sample_buffer[-shift:] = audio.clone()


class _CacheFeatureBufferer:
    def __init__(
        self,
        sample_rate: int,
        buffer_size_in_secs: float,
        chunk_size_in_secs: float,
        preprocessor_cfg: Any,
        device: torch.device,
        fill_value: float = -16.635,
    ):
        if buffer_size_in_secs < chunk_size_in_secs:
            raise ValueError(
                f"Buffer size ({buffer_size_in_secs}s) should be no less than chunk size ({chunk_size_in_secs}s)"
            )

        self.sample_rate = sample_rate
        self.buffer_size_in_secs = buffer_size_in_secs
        self.chunk_size_in_secs = chunk_size_in_secs
        self.device = device
        self.zero_level_spec_db_val = -16.635 if getattr(preprocessor_cfg, "log", False) else fill_value
        self.n_feat = preprocessor_cfg.features
        self.timestep_duration = preprocessor_cfg.window_stride
        self.n_chunk_look_back = int(self.timestep_duration * self.sample_rate)
        self.chunk_size = int(self.chunk_size_in_secs * self.sample_rate)
        self.sample_buffer = _AudioBufferer(sample_rate, buffer_size_in_secs)
        self.feature_buffer_len = int(buffer_size_in_secs / self.timestep_duration)
        self.feature_chunk_len = int(chunk_size_in_secs / self.timestep_duration)
        self.feature_buffer = torch.full(
            [self.n_feat, self.feature_buffer_len],
            self.zero_level_spec_db_val,
            dtype=torch.float32,
            device=self.device,
        )
        self.preprocessor = nemo_asr.models.ASRModel.from_config_dict(preprocessor_cfg)
        self.preprocessor.to(self.device)

    def reset(self) -> None:
        self.sample_buffer.reset()
        self.feature_buffer.fill_(self.zero_level_spec_db_val)

    def _update_feature_buffer(self, feat_chunk: torch.Tensor) -> None:
        self.feature_buffer[:, : -self.feature_chunk_len] = self.feature_buffer[:, self.feature_chunk_len :].clone()
        self.feature_buffer[:, -self.feature_chunk_len :] = feat_chunk.clone()

    def preprocess(self, audio_signal: torch.Tensor) -> torch.Tensor:
        audio_signal = audio_signal.unsqueeze_(0).to(self.device)
        audio_signal_len = torch.tensor([audio_signal.shape[1]], device=self.device)
        features, _ = self.preprocessor(input_signal=audio_signal, length=audio_signal_len)
        return features.squeeze()

    def update(self, audio: np.ndarray) -> None:
        self.sample_buffer.update(audio)
        if math.isclose(self.buffer_size_in_secs, self.chunk_size_in_secs):
            samples = self.sample_buffer.sample_buffer.clone()
        else:
            samples = self.sample_buffer.sample_buffer[-(self.n_chunk_look_back + self.chunk_size) :]
        features = self.preprocess(samples)
        if (diff := features.shape[1] - self.feature_chunk_len - 1) > 0:
            features = features[:, :-diff]
        self._update_feature_buffer(features[:, -self.feature_chunk_len :])

    def get_feature_buffer(self) -> torch.Tensor:
        return self.feature_buffer.clone()


@dataclass
class _StreamingDiarizationConfig:
    model_path: str = "nvidia/diar_streaming_sortformer_4spk-v2.1"
    device: str = "cuda"
    log: bool = False
    max_num_speakers: int = 4
    spkcache_len: int = 188
    spkcache_refresh_rate: int = 300
    fifo_len: int = 40
    chunk_len: int = 340
    chunk_left_context: int = 1
    chunk_right_context: int = 40


class _NeMoStreamingDiarizer:
    def __init__(
        self,
        cfg: _StreamingDiarizationConfig,
        model: str,
        frame_len_in_secs: float = STREAMING_SORTFORMER_FRAME_LEN_SECONDS,
        sample_rate: int = SAMPLE_RATE,
        left_offset: int = 8,
        right_offset: int = 8,
        use_amp: bool = False,
        compute_dtype: Any = None,
    ):
        self.model = model
        self.cfg = cfg
        self.cfg.model_path = model
        self.device = torch.device(cfg.device)
        self.use_amp = use_amp
        self.compute_dtype = compute_dtype or torch.float32
        self.frame_len_in_secs = frame_len_in_secs
        self.left_offset = left_offset
        self.right_offset = right_offset
        self.chunk_size = self.cfg.chunk_len
        self.max_num_speakers = self.cfg.max_num_speakers
        self.diarizer = self._build_diarizer()
        self.buffer_size_in_secs = (
            self.cfg.chunk_len * self.frame_len_in_secs
            + (self.left_offset + self.right_offset) * 0.01
        )
        self.feature_bufferer = _CacheFeatureBufferer(
            sample_rate=sample_rate,
            buffer_size_in_secs=self.buffer_size_in_secs,
            chunk_size_in_secs=self.cfg.chunk_len * self.frame_len_in_secs,
            preprocessor_cfg=self.diarizer.cfg.preprocessor,
            device=self.device,
        )
        self.streaming_state = self.init_streaming_state(batch_size=1)
        self.total_preds = torch.zeros((1, 0, self.max_num_speakers), device=self.diarizer.device)
        self.frame_counter = 0

    def _build_diarizer(self) -> Any:
        diar_model = nemo_asr.models.SortformerEncLabelModel.from_pretrained(
            model_name=self.cfg.model_path,
            map_location=self.cfg.device,
        )
        diar_model.sortformer_modules.chunk_len = self.cfg.chunk_len
        diar_model.sortformer_modules.spkcache_len = self.cfg.spkcache_len
        diar_model.sortformer_modules.chunk_left_context = self.cfg.chunk_left_context
        diar_model.sortformer_modules.chunk_right_context = self.cfg.chunk_right_context
        diar_model.sortformer_modules.fifo_len = self.cfg.fifo_len
        diar_model.sortformer_modules.log = self.cfg.log
        if hasattr(diar_model.sortformer_modules, "spkcache_refresh_rate"):
            diar_model.sortformer_modules.spkcache_refresh_rate = self.cfg.spkcache_refresh_rate
        if hasattr(diar_model.sortformer_modules, "spkcache_update_period"):
            diar_model.sortformer_modules.spkcache_update_period = self.cfg.spkcache_refresh_rate
        diar_model.eval()
        return diar_model

    def reset_state(self) -> None:
        self.feature_bufferer.reset()
        self.streaming_state = self.init_streaming_state(batch_size=1)
        self.total_preds = torch.zeros((1, 0, self.max_num_speakers), device=self.diarizer.device)
        self.frame_counter = 0

    def init_streaming_state(self, batch_size: int = 1) -> StreamingSortformerState:
        return self.diarizer.sortformer_modules.init_streaming_state(
            batch_size=batch_size,
            async_streaming=self.diarizer.async_streaming,
            device=self.device,
        )

    def stream_step(
        self,
        processed_signal: torch.Tensor,
        processed_signal_length: torch.Tensor,
        streaming_state: StreamingSortformerState,
        total_preds: torch.Tensor,
    ) -> tuple[StreamingSortformerState, torch.Tensor]:
        if processed_signal.device != self.device:
            processed_signal = processed_signal.to(self.device)
        if processed_signal_length.device != self.device:
            processed_signal_length = processed_signal_length.to(self.device)
        if total_preds.device != self.device:
            total_preds = total_preds.to(self.device)

        with (
            torch.amp.autocast(device_type=self.device.type, dtype=self.compute_dtype, enabled=self.use_amp),
            torch.inference_mode(),
            torch.no_grad(),
        ):
            result = self.diarizer.forward_streaming_step(
                processed_signal=processed_signal,
                processed_signal_length=processed_signal_length,
                streaming_state=streaming_state,
                total_preds=total_preds,
                left_offset=self.left_offset,
                right_offset=self.right_offset,
            )
        if self.device.type == "cuda":
            torch.cuda.synchronize(self.device)
        return result

    def push_frame(self, audio_frame: bytes) -> None:
        self.frame_counter += 1
        audio_array = np.frombuffer(audio_frame, dtype=np.int16).astype(np.float32) / 32768.0
        self.feature_bufferer.update(audio_array)
        if self.device.type == "cuda":
            torch.cuda.synchronize(self.device)
        features = self.feature_bufferer.get_feature_buffer().unsqueeze(0).transpose(1, 2)
        feature_lengths = torch.tensor([features.shape[1]], device=self.device)
        self.streaming_state, self.total_preds = self.stream_step(
            processed_signal=features,
            processed_signal_length=feature_lengths,
            streaming_state=self.streaming_state,
            total_preds=self.total_preds,
        )

    def diarize_pcm_bytes(self, pcm_bytes: bytes) -> np.ndarray:
        self.reset_state()
        frame_bytes = int(SAMPLE_RATE * self.frame_len_in_secs * 2)
        total_frame_count = (len(pcm_bytes) + frame_bytes - 1) // frame_bytes
        print(f"Streaming Sortformer processing {total_frame_count} frames on {self.device}", flush=True)
        if frame_bytes <= 0:
            raise ValueError(f"Invalid frame size computed for streaming diarizer: {frame_bytes}")
        for offset in range(0, len(pcm_bytes), frame_bytes):
            frame = pcm_bytes[offset : offset + frame_bytes]
            if len(frame) < frame_bytes:
                frame = frame + (b"\x00" * (frame_bytes - len(frame)))
            self.push_frame(frame)
            if self.frame_counter <= 3 or self.frame_counter % 25 == 0:
                print(f"Streaming Sortformer processed frame {self.frame_counter}/{total_frame_count}", flush=True)
        return self.total_preds[0].detach().float().cpu().numpy()


@app.cls(
    image=image,
    gpu="L40S",
    volumes={CACHE_DIR: model_cache},
    timeout=30 * MINUTES,
    scaledown_window=2 * MINUTES,
    max_containers=10,
)
class Parakeet:
    @modal.enter()
    async def load(self):
        logging.getLogger("nemo_logger").setLevel(logging.CRITICAL)
        model_name = os.environ.get("PARAKEET_MODEL", MODEL)
        self.diarizers: dict[str, Any] = {}
        self.diarizer_locks = {
            backend: threading.Lock() for backend in SUPPORTED_DIARIZATION_BACKENDS
        }
        self.default_diarization_backend = os.environ.get("DIARIZATION_BACKEND", DIARIZATION_BACKEND).strip().lower()
        self.sortformer_diarizer_model_name = os.environ.get(
            "DIARIZATION_SORTFORMER_MODEL", DIARIZATION_SORTFORMER_MODEL
        )
        self.streaming_sortformer_diarizer_model_name = os.environ.get(
            "DIARIZATION_STREAMING_SORTFORMER_MODEL",
            DIARIZATION_STREAMING_SORTFORMER_MODEL,
        )
        self.diarizer_vad_model_name = os.environ.get("DIARIZATION_VAD_MODEL", DIARIZATION_VAD_MODEL)
        self.diarizer_speaker_model_name = os.environ.get(
            "DIARIZATION_SPEAKER_MODEL", DIARIZATION_SPEAKER_MODEL
        )
        self.streaming_sortformer_chunk_len = int(os.environ.get("STREAMING_SORTFORMER_CHUNK_LEN", "6"))
        self.streaming_sortformer_device = os.environ.get("STREAMING_SORTFORMER_DEVICE", "cuda").strip().lower()
        self.streaming_sortformer_chunk_right_context = int(
            os.environ.get("STREAMING_SORTFORMER_CHUNK_RIGHT_CONTEXT", "7")
        )
        self.streaming_sortformer_chunk_left_context = int(
            os.environ.get("STREAMING_SORTFORMER_CHUNK_LEFT_CONTEXT", "1")
        )
        self.streaming_sortformer_fifo_len = int(os.environ.get("STREAMING_SORTFORMER_FIFO_LEN", "188"))
        self.streaming_sortformer_spkcache_len = int(os.environ.get("STREAMING_SORTFORMER_SPKCACHE_LEN", "188"))
        self.streaming_sortformer_spkcache_update_period = int(
            os.environ.get("STREAMING_SORTFORMER_SPKCACHE_UPDATE_PERIOD", "144")
        )
        self.streaming_sortformer_activation_threshold = float(
            os.environ.get("STREAMING_SORTFORMER_ACTIVATION_THRESHOLD", "0.35")
        )
        self.model_inference_dtype_name = os.environ.get("PARAKEET_INFERENCE_DTYPE", PARAKEET_INFERENCE_DTYPE)
        self.model_inference_dtype = _parse_torch_dtype(self.model_inference_dtype_name)
        if self.default_diarization_backend not in SUPPORTED_DIARIZATION_BACKENDS:
            raise ValueError(
                f"Unsupported DIARIZATION_BACKEND={self.default_diarization_backend!r}; "
                f"use one of {sorted(SUPPORTED_DIARIZATION_BACKENDS)}"
            )
        print("Runtime versions:")
        print(f"  torch={torch.__version__}")
        try:
            import torchaudio

            print(f"  torchaudio={torchaudio.__version__}")
        except Exception as e:
            print(f"  torchaudio import failed: {e}")
            raise
        print(f"  torch.cuda={torch.version.cuda}")
        print(f"  cuda_available={torch.cuda.is_available()}")
        print(f"Loading Parakeet model: {model_name}")
        self.model = nemo_asr.models.ASRModel.from_pretrained(model_name=model_name)
        self.model.to(device="cuda", dtype=self.model_inference_dtype)
        self.model.eval()
        print(
            f"Parakeet ASR ready with parameter dtype={_module_param_dtype(self.model)} "
            f"(requested={self.model_inference_dtype_name})"
        )

        decoding_cfg = RNNTDecodingConfig(
            strategy="greedy_batch",
            durations=self.model.cfg.decoding.durations,
            model_type=self.model.cfg.decoding.model_type,
            greedy=self.model.cfg.decoding.greedy,
            beam=self.model.cfg.decoding.beam,
            preserve_alignments=True,
            confidence_cfg=ConfidenceConfig(preserve_word_confidence=True),
        )
        self.model.change_decoding_strategy(decoding_cfg)

        if os.environ.get("WARMUP_ON_START", "true").strip().lower() in {"1", "true", "yes", "on"}:
            await self._warm_up_gpu()
        print("Parakeet ready")

    def _resolve_diarization_backend(self, diarization_backend: Optional[str]) -> str:
        backend = (diarization_backend or self.default_diarization_backend).strip().lower()
        if backend not in SUPPORTED_DIARIZATION_BACKENDS:
            raise ValueError(
                f"Unsupported diarization_backend={backend!r}; use one of {sorted(SUPPORTED_DIARIZATION_BACKENDS)}"
            )
        return backend

    def _configure_streaming_sortformer_diarizer(self, diarizer: Any) -> None:
        if isinstance(diarizer, _NeMoStreamingDiarizer):
            return
        modules = getattr(diarizer, "sortformer_modules", None)
        if modules is None:
            return

        for attr_name, value in (
            ("chunk_len", self.streaming_sortformer_chunk_len),
            ("chunk_left_context", self.streaming_sortformer_chunk_left_context),
            ("chunk_right_context", self.streaming_sortformer_chunk_right_context),
            ("fifo_len", self.streaming_sortformer_fifo_len),
            ("spkcache_len", self.streaming_sortformer_spkcache_len),
            ("spkcache_update_period", self.streaming_sortformer_spkcache_update_period),
        ):
            if hasattr(modules, attr_name):
                setattr(modules, attr_name, value)

    def _build_clustering_diarizer_cfg(self, manifest_path: str, out_dir: str) -> Any:
        max_num_speakers = int(os.environ.get("DIARIZATION_MAX_NUM_SPEAKERS", "8"))
        chunk_cluster_count = int(os.environ.get("DIARIZATION_CHUNK_CLUSTER_COUNT", "50"))
        embeddings_per_chunk = int(os.environ.get("DIARIZATION_EMBEDDINGS_PER_CHUNK", "10000"))

        return OmegaConf.create(
            {
                "name": "ClusterDiarizer",
                "num_workers": 0,
                "sample_rate": SAMPLE_RATE,
                "batch_size": 64,
                "device": "cuda",
                "verbose": False,
                "diarizer": {
                    "manifest_filepath": manifest_path,
                    "out_dir": out_dir,
                    "oracle_vad": False,
                    "collar": 0.25,
                    "ignore_overlap": True,
                    "vad": {
                        "model_path": self.diarizer_vad_model_name,
                        "external_vad_manifest": None,
                        "parameters": {
                            "window_length_in_sec": 0.15,
                            "shift_length_in_sec": 0.01,
                            "smoothing": "median",
                            "overlap": 0.5,
                            "onset": 0.1,
                            "offset": 0.1,
                            "pad_onset": 0.1,
                            "pad_offset": 0.0,
                            "min_duration_on": 0.0,
                            "min_duration_off": 0.2,
                            "filter_speech_first": True,
                        },
                    },
                    "speaker_embeddings": {
                        "model_path": self.diarizer_speaker_model_name,
                        "parameters": {
                            "window_length_in_sec": [1.5, 1.25, 1.0, 0.75, 0.5],
                            "shift_length_in_sec": [0.75, 0.625, 0.5, 0.375, 0.25],
                            "multiscale_weights": [1, 1, 1, 1, 1],
                            "save_embeddings": False,
                        },
                    },
                    "clustering": {
                        "parameters": {
                            "oracle_num_speakers": False,
                            "max_num_speakers": max_num_speakers,
                            "enhanced_count_thres": 80,
                            "max_rp_threshold": 0.25,
                            "sparse_search_volume": 30,
                            "maj_vote_spk_count": False,
                            "chunk_cluster_count": chunk_cluster_count,
                            "embeddings_per_chunk": embeddings_per_chunk,
                        },
                    },
                },
            }
        )

    def _ensure_diarizer_loaded(self, diarization_backend: str) -> Any:
        diarizer = self.diarizers.get(diarization_backend)
        if diarizer is not None:
            return diarizer

        with self.diarizer_locks[diarization_backend]:
            diarizer = self.diarizers.get(diarization_backend)
            if diarizer is not None:
                return diarizer

            try:
                if diarization_backend == "sortformer":
                    print(f"Loading diarization model: {self.sortformer_diarizer_model_name}")
                    diarizer = nemo_asr.models.SortformerEncLabelModel.from_pretrained(
                        model_name=self.sortformer_diarizer_model_name
                    )
                    diarizer.to("cuda")
                elif diarization_backend == "streaming_sortformer":
                    print(
                        "Loading streaming diarization model: "
                        f"{self.streaming_sortformer_diarizer_model_name} "
                        f"(device={self.streaming_sortformer_device}, chunk_len={self.streaming_sortformer_chunk_len}, "
                        f"left={self.streaming_sortformer_chunk_left_context}, right={self.streaming_sortformer_chunk_right_context}, "
                        f"fifo={self.streaming_sortformer_fifo_len}, spkcache_len={self.streaming_sortformer_spkcache_len}, "
                        f"spkcache_update={self.streaming_sortformer_spkcache_update_period})"
                    )
                    diarizer = _NeMoStreamingDiarizer(
                        cfg=_StreamingDiarizationConfig(
                            model_path=self.streaming_sortformer_diarizer_model_name,
                            device=self.streaming_sortformer_device,
                            max_num_speakers=4,
                            spkcache_len=self.streaming_sortformer_spkcache_len,
                            spkcache_refresh_rate=self.streaming_sortformer_spkcache_update_period,
                            fifo_len=self.streaming_sortformer_fifo_len,
                            chunk_len=self.streaming_sortformer_chunk_len,
                            chunk_left_context=self.streaming_sortformer_chunk_left_context,
                            chunk_right_context=self.streaming_sortformer_chunk_right_context,
                        ),
                        model=self.streaming_sortformer_diarizer_model_name,
                    )
                else:
                    print(
                        "Loading diarization pipeline: "
                        f"clustering (vad={self.diarizer_vad_model_name}, speaker={self.diarizer_speaker_model_name})"
                    )
                    diarizer_cfg = self._build_clustering_diarizer_cfg(
                        manifest_path=os.path.join(CACHE_DIR, "diarization-placeholder-manifest.json"),
                        out_dir=os.path.join(CACHE_DIR, "diarization-placeholder-output"),
                    )
                    diarizer = ClusteringDiarizer(cfg=diarizer_cfg).to(diarizer_cfg.device)
                if hasattr(diarizer, "eval"):
                    diarizer.eval()
                print(
                    f"Diarization backend {diarization_backend} ready "
                    f"with parameter dtype={_module_param_dtype(diarizer)}"
                )
            except Exception as e:
                raise RuntimeError(
                    "Failed to load the speaker diarization model. "
                    "If the model download requires Hugging Face auth, set HF_TOKEN before deploy. "
                    f"Original error: {e}"
                ) from e

            self.diarizers[diarization_backend] = diarizer
            return diarizer

    def _diarize_with_sortformer(self, diarizer: Any, pcm_bytes: bytes, diarization_backend: str) -> list[dict[str, Any]]:
        wav_path = write_pcm16_wav_temp(pcm_bytes, prefix="parakeet-diar-")
        try:
            with self.diarizer_locks[diarization_backend]:
                with NoStdStreams():
                    try:
                        with _module_autocast_context(diarizer), torch.inference_mode(), torch.no_grad():
                            predicted_segments = diarizer.diarize(audio=[wav_path], batch_size=1)
                    except RuntimeError as e:
                        if "must have the same dtype" not in str(e):
                            raise
                        print(
                            f"{diarization_backend} diarizer hit dtype mismatch with parameter "
                            f"dtype={_module_param_dtype(diarizer)}; retrying in float32"
                        )
                        _force_module_float32(diarizer)
                        diarizer.eval()
                        with torch.inference_mode(), torch.no_grad():
                            predicted_segments = diarizer.diarize(audio=[wav_path], batch_size=1)
            if isinstance(predicted_segments, tuple):
                predicted_segments = predicted_segments[0]
            if isinstance(predicted_segments, list) and predicted_segments and isinstance(predicted_segments[0], list):
                predicted_segments = predicted_segments[0]
            return _normalize_speaker_segments(predicted_segments)
        finally:
            try:
                os.unlink(wav_path)
            except OSError:
                pass

    def _diarize_with_streaming_sortformer(self, diarizer: _NeMoStreamingDiarizer, pcm_bytes: bytes) -> list[dict[str, Any]]:
        with self.diarizer_locks["streaming_sortformer"]:
            try:
                speaker_probs = diarizer.diarize_pcm_bytes(pcm_bytes)
            finally:
                diarizer.reset_state()

        speaker_segments = _streaming_sortformer_probs_to_segments(
            speaker_probs,
            activation_threshold=self.streaming_sortformer_activation_threshold,
        )
        if not speaker_segments:
            raise RuntimeError("Streaming Sortformer produced no speaker segments")
        return speaker_segments

    def _diarize_with_clustering(self, diarizer: Any, pcm_bytes: bytes) -> list[dict[str, Any]]:
        wav_path = write_pcm16_wav_temp(pcm_bytes, prefix="parakeet-diar-")
        try:
            with tempfile.TemporaryDirectory(prefix="parakeet-clustering-diar-") as work_dir:
                manifest_path = os.path.join(work_dir, "manifest.json")
                with open(manifest_path, "w", encoding="utf-8") as manifest_file:
                    json.dump(
                        {
                            "audio_filepath": wav_path,
                            "offset": 0,
                            "duration": None,
                            "label": "infer",
                            "text": "-",
                            "num_speakers": None,
                            "rttm_filepath": None,
                            "uem_filepath": None,
                        },
                        manifest_file,
                    )
                    manifest_file.write("\n")

                with self.diarizer_locks["clustering"]:
                    diarizer._cfg.diarizer.manifest_filepath = manifest_path
                    diarizer._cfg.diarizer.out_dir = work_dir
                    diarizer._diarizer_params.manifest_filepath = manifest_path
                    diarizer._diarizer_params.out_dir = work_dir
                    with NoStdStreams():
                        with torch.inference_mode(), torch.no_grad():
                            diarizer.diarize()

                speaker_segments = _load_rttm_speaker_segments(os.path.join(work_dir, "pred_rttms"))
                if not speaker_segments:
                    raise RuntimeError("Clustering diarizer produced no speaker segments")
                return speaker_segments
        finally:
            try:
                os.unlink(wav_path)
            except OSError:
                pass

    def _diarization_metadata(self, diarization_backend: str) -> dict[str, Any]:
        if diarization_backend == "sortformer":
            return {
                "diarization_backend": diarization_backend,
                "diarization_model": self.sortformer_diarizer_model_name,
            }
        if diarization_backend == "streaming_sortformer":
            return {
                "diarization_backend": diarization_backend,
                "diarization_model": self.streaming_sortformer_diarizer_model_name,
                "streaming_sortformer_device": self.streaming_sortformer_device,
                "streaming_sortformer_chunk_len": self.streaming_sortformer_chunk_len,
                "streaming_sortformer_chunk_left_context": self.streaming_sortformer_chunk_left_context,
                "streaming_sortformer_chunk_right_context": self.streaming_sortformer_chunk_right_context,
                "streaming_sortformer_fifo_len": self.streaming_sortformer_fifo_len,
                "streaming_sortformer_spkcache_len": self.streaming_sortformer_spkcache_len,
                "streaming_sortformer_spkcache_update_period": self.streaming_sortformer_spkcache_update_period,
                "streaming_sortformer_activation_threshold": self.streaming_sortformer_activation_threshold,
            }
        return {
            "diarization_backend": diarization_backend,
            "diarization_model": (
                f"clustering(vad={self.diarizer_vad_model_name},speaker={self.diarizer_speaker_model_name})"
            ),
            "diarization_vad_model": self.diarizer_vad_model_name,
            "diarization_speaker_model": self.diarizer_speaker_model_name,
        }

    def _diarize(self, pcm_bytes: bytes, diarization_backend: Optional[str] = None) -> tuple[str, list[dict[str, Any]]]:
        resolved_backend = self._resolve_diarization_backend(diarization_backend)
        diarizer = self._ensure_diarizer_loaded(resolved_backend)
        if resolved_backend == "sortformer":
            return resolved_backend, self._diarize_with_sortformer(diarizer, pcm_bytes, resolved_backend)
        if resolved_backend == "streaming_sortformer":
            return resolved_backend, self._diarize_with_streaming_sortformer(diarizer, pcm_bytes)
        return resolved_backend, self._diarize_with_clustering(diarizer, pcm_bytes)

    async def _warm_up_gpu(self) -> None:
        from urllib.request import urlopen

        audio_url = "https://github.com/voxserv/audio_quality_testing_samples/raw/refs/heads/master/mono_44100/156550__acclivity__a-dream-within-a-dream.wav"
        audio_bytes = urlopen(audio_url).read()
        try:
            await self.transcribe.local(
                audio_bytes,
                timestamp_level="word",
                return_word_confidence=False,
                diarize=False,
            )
        except Exception as e:
            print(f"Warmup failed: {e}")

    def _transcribe_asr(
        self,
        audio_data: bytes | bytearray | list[bytes | bytearray],
        timestamp_level: Optional[Literal["word", "segment", "char"]] = None,
        return_word_confidence: bool = True,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        is_batch = isinstance(audio_data, list)

        if is_batch:
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(audio_data) or 1) as executor:
                wav_paths = list(executor.map(write_wav_file, enumerate(audio_data)))
            try:
                with NoStdStreams():
                    try:
                        with _module_autocast_context(self.model), torch.inference_mode(), torch.no_grad():
                            output = self.model.transcribe(
                                wav_paths,
                                batch_size=len(wav_paths),
                                num_workers=1,
                                timestamps=timestamp_level is not None,
                                return_hypotheses=True,
                            )
                    except RuntimeError as e:
                        if "must have the same dtype" not in str(e):
                            raise
                        print(
                            "Parakeet ASR hit dtype mismatch with parameter "
                            f"dtype={_module_param_dtype(self.model)}; retrying in float32"
                        )
                        _force_module_float32(self.model)
                        self.model.eval()
                        with torch.inference_mode(), torch.no_grad():
                            output = self.model.transcribe(
                                wav_paths,
                                batch_size=len(wav_paths),
                                num_workers=1,
                                timestamps=timestamp_level is not None,
                                return_hypotheses=True,
                            )
            finally:
                for path in wav_paths:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
        else:
            tensor = preprocess_audio(audio_data, return_tensor=True)
            with NoStdStreams():
                try:
                    with _module_autocast_context(self.model), torch.inference_mode(), torch.no_grad():
                        output = self.model.transcribe(
                            tensor,
                            timestamps=timestamp_level is not None,
                            return_hypotheses=True,
                        )
                except RuntimeError as e:
                    if "must have the same dtype" not in str(e):
                        raise
                    print(
                        "Parakeet ASR hit dtype mismatch with parameter "
                        f"dtype={_module_param_dtype(self.model)}; retrying in float32"
                    )
                    _force_module_float32(self.model)
                    self.model.eval()
                    with torch.inference_mode(), torch.no_grad():
                        output = self.model.transcribe(
                            tensor,
                            timestamps=timestamp_level is not None,
                            return_hypotheses=True,
                        )

        def format_result(result: Any) -> dict[str, Any]:
            if timestamp_level is not None:
                timestamps_data = result.timestamp.get(timestamp_level, [])
                word_level_info = [
                    (
                        stamp.get(timestamp_level, stamp.get("segment", "")),
                        stamp["start"],
                        stamp["end"],
                    )
                    for stamp in timestamps_data
                ]
                if timestamp_level == "word" and return_word_confidence and getattr(result, "word_confidence", None):
                    word_level_info = [
                        data + (result.word_confidence[i].item(),)
                        for i, data in enumerate(word_level_info)
                    ]
            else:
                word_level_info = []
            words: list[dict[str, Any]] = []
            if timestamp_level == "word":
                words = [
                    {
                        "text": str(data[0]),
                        "start": float(data[1]),
                        "end": float(data[2]),
                        **({"confidence": float(data[3])} if len(data) > 3 else {}),
                    }
                    for data in word_level_info
                ]
            return {
                "transcript": result.text,
                "word_level_info": word_level_info,
                "words": words,
            }

        if is_batch:
            return [format_result(result) for result in output]
        return format_result(output[0])

    def _transcribe_pcm_bytes(
        self,
        pcm_bytes: bytes,
        timestamp_level: Optional[Literal["word", "segment", "char"]] = None,
        return_word_confidence: bool = True,
        diarize: bool = False,
        diarization_backend: Optional[str] = None,
    ) -> dict[str, Any]:
        if diarize and timestamp_level not in {None, "word"}:
            raise ValueError("diarize=true currently requires timestamp_level to be omitted or set to 'word'")
        resolved_diarization_backend = (
            self._resolve_diarization_backend(diarization_backend) if diarize else None
        )

        duration_seconds = len(pcm_bytes) / (SAMPLE_RATE * 2)
        max_audio_seconds = int(os.environ.get("MAX_AUDIO_SECONDS", "21600"))
        if duration_seconds > max_audio_seconds:
            raise ValueError(f"Audio too long: {duration_seconds:.1f}s (max {max_audio_seconds}s)")

        max_diarization_seconds = int(os.environ.get("MAX_DIARIZATION_SECONDS", str(max_audio_seconds)))
        if diarize and duration_seconds > max_diarization_seconds:
            raise ValueError(
                f"Audio too long for diarization: {duration_seconds:.1f}s (max {max_diarization_seconds}s)"
            )
        if diarize and resolved_diarization_backend == "sortformer" and duration_seconds > MAX_SORTFORMER_AUDIO_SECONDS:
            raise ValueError(
                "sortformer diarization is limited to audio up to "
                f"{MAX_SORTFORMER_AUDIO_SECONDS}s; got {duration_seconds:.1f}s. "
                "Use diarization_backend='clustering' for longer audio."
            )

        chunk_seconds = int(os.environ.get("CHUNK_SECONDS", "300"))
        batch_size = int(os.environ.get("BATCH_SIZE", "1"))
        chunks = split_pcm16(pcm_bytes, chunk_seconds=chunk_seconds)
        effective_timestamp_level = "word" if diarize else timestamp_level

        all_chunk_results: list[dict[str, Any]] = []
        merged_word_level_info: list[list[Any]] = []
        merged_words: list[dict[str, Any]] = []
        transcript_parts: list[str] = []

        for batch_idx in range(0, len(chunks), batch_size):
            batch = chunks[batch_idx : batch_idx + batch_size]
            batch_results = self._transcribe_asr(
                batch,
                timestamp_level=effective_timestamp_level,
                return_word_confidence=return_word_confidence,
            )
            assert isinstance(batch_results, list)
            for i, chunk_result in enumerate(batch_results):
                chunk_index = batch_idx + i
                chunk_offset_seconds = float(chunk_index * chunk_seconds)
                transcript = str(chunk_result.get("transcript", "")).strip()
                if transcript:
                    transcript_parts.append(transcript)
                chunk_word_info = _offset_word_level_info(
                    list(chunk_result.get("word_level_info", [])),
                    chunk_offset_seconds,
                )
                chunk_words = _offset_words(
                    list(chunk_result.get("words", [])),
                    chunk_offset_seconds,
                )
                merged_word_level_info.extend(chunk_word_info)
                merged_words.extend(chunk_words)
                all_chunk_results.append(
                    {
                        **chunk_result,
                        "chunk_index": chunk_index,
                        "offset_seconds": chunk_offset_seconds,
                        "duration_seconds": min(chunk_seconds, duration_seconds - chunk_offset_seconds),
                        "word_level_info": chunk_word_info,
                        "words": chunk_words,
                    }
                )

        response_payload: dict[str, Any] = {
            "transcript": "\n".join(part for part in transcript_parts if part),
            "word_level_info": merged_word_level_info,
            "chunks": all_chunk_results,
            "metadata": {
                "model": os.environ.get("PARAKEET_MODEL", MODEL),
                "sample_rate": SAMPLE_RATE,
                "duration_seconds": duration_seconds,
                "timestamp_level": timestamp_level,
                "effective_timestamp_level": effective_timestamp_level,
                "return_word_confidence": return_word_confidence,
                "chunk_seconds": chunk_seconds,
                "batch_size": batch_size,
                "chunk_count": len(chunks),
                "diarization_enabled": diarize,
            },
        }

        if diarize:
            assert resolved_diarization_backend is not None
            resolved_diarization_backend, speaker_segments = self._diarize(
                pcm_bytes,
                resolved_diarization_backend,
            )
            words_with_speakers = _assign_words_to_speakers(merged_words, speaker_segments)
            response_payload.update(
                {
                    "speaker_segments": speaker_segments,
                    "words": words_with_speakers,
                    "utterances": _build_utterances(words_with_speakers),
                    "speakers": sorted({segment["speaker"] for segment in speaker_segments}),
                }
            )
            response_payload["metadata"].update(self._diarization_metadata(resolved_diarization_backend))

        return response_payload

    @modal.method()
    async def transcribe(
        self,
        audio_data: bytes | bytearray | list[bytes | bytearray],
        timestamp_level: Optional[Literal["word", "segment", "char"]] = None,
        return_word_confidence: bool = True,
        diarize: bool = False,
        diarization_backend: Optional[str] = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        if diarize:
            if isinstance(audio_data, list):
                raise ValueError("diarize=True is only supported for a single audio input when calling transcribe().")
            pcm_bytes = preprocess_audio(audio_data, return_tensor=False)
            return self._transcribe_pcm_bytes(
                pcm_bytes,
                timestamp_level=timestamp_level,
                return_word_confidence=return_word_confidence,
                diarize=True,
                diarization_backend=diarization_backend,
            )

        return self._transcribe_asr(
            audio_data,
            timestamp_level=timestamp_level,
            return_word_confidence=return_word_confidence,
        )

    @modal.asgi_app()
    def web(self):
        web_app = FastAPI(title="NVIDIA Parakeet ASR", version="1.0.0")

        async def _form_file_bytes(form: Any, field: str = "file") -> bytes:
            fil = form.get(field)
            if fil is None:
                raise HTTPException(status_code=400, detail=f"Missing multipart field '{field}'.")
            if hasattr(fil, "read"):
                data = await fil.read()
                return data if isinstance(data, (bytes, bytearray)) else bytes(data)
            if isinstance(fil, (bytes, bytearray)):
                return bytes(fil)
            return bytes(fil)

        def _form_optional_str(form: Any, key: str) -> Optional[str]:
            v = form.get(key)
            if v is None:
                return None
            s = str(v).strip()
            return s or None

        def _form_bool(form: Any, key: str, default: bool) -> bool:
            v = form.get(key)
            if v is None:
                return default
            return str(v).strip().lower() in {"1", "true", "yes", "on"}

        @web_app.get("/health")
        async def health() -> dict[str, str]:
            return {"status": "ok"}

        @web_app.post("/transcribe")
        async def transcribe_endpoint(request: Request) -> JSONResponse:
            form = await request.form()
            raw = await _form_file_bytes(form, "file")
            timestamp_level = _form_optional_str(form, "timestamp_level")
            if timestamp_level not in {None, "word", "segment", "char"}:
                raise HTTPException(status_code=400, detail="timestamp_level must be one of: word, segment, char")
            return_word_confidence = _form_bool(form, "return_word_confidence", True)
            diarize = _form_bool(form, "diarize", False)
            diarization_backend = _form_optional_str(form, "diarization_backend")
            try:
                pcm_bytes = preprocess_audio(raw, return_tensor=False)
                response_payload = self._transcribe_pcm_bytes(
                    pcm_bytes,
                    timestamp_level=timestamp_level,
                    return_word_confidence=return_word_confidence,
                    diarize=diarize,
                    diarization_backend=diarization_backend,
                )
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
            except RuntimeError as e:
                raise HTTPException(status_code=500, detail=str(e)) from e

            return JSONResponse(response_payload)

        return web_app


@app.local_entrypoint()
async def main() -> None:
    url = await Parakeet().web.get_web_url.aio()
    print(url)
