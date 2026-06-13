"""Transcription engines — pluggable behind a common interface.

`CpuFasterWhisper` is the only live engine (CTranslate2, CPU int8). It is the
default and never touches the GPU, so it is safe to run while FLUX / Qwen / a
game hold the card.

`GpuWhisperCpp` is a declared seam, not yet implemented: CTranslate2 has no
AMD/ROCm support, so the eventual "GPU when the broker says free" path will be
whisper.cpp built with hipBLAS. The resolver below shows exactly where it slots
in (broker-gated, CPU fallback) so adding it is local to this file.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

from . import config

logger = logging.getLogger("media-ai.engine")


@dataclass
class Segment:
    start: float
    end: float
    text: str


@dataclass
class TranscriptResult:
    engine: str
    model: str
    language: str
    language_probability: float
    duration: float          # audio seconds
    transcribe_seconds: float
    segments: list[Segment] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(s.text for s in self.segments).strip()

    @property
    def realtime_factor(self) -> float:
        return round(self.duration / self.transcribe_seconds, 2) if self.transcribe_seconds else 0.0


class CpuFasterWhisper:
    """CTranslate2 Whisper on CPU. Models are loaded lazily and cached per
    model-size (loading is ~1-2 s; we don't want it on every request)."""

    name = "cpu-faster-whisper"

    def __init__(self) -> None:
        self._models: dict[str, object] = {}
        self._lock = threading.Lock()

    def _model(self, model_size: str):
        with self._lock:
            m = self._models.get(model_size)
            if m is None:
                from faster_whisper import WhisperModel  # imported here so the
                # module imports cheaply (and unit tests can skip the heavy dep)
                logger.info("loading whisper model=%s compute=%s threads=%s",
                            model_size, config.COMPUTE_TYPE, config.CPU_THREADS)
                m = WhisperModel(
                    model_size,
                    device="cpu",
                    compute_type=config.COMPUTE_TYPE,
                    download_root=config.MODELS_DIR,
                    cpu_threads=config.CPU_THREADS,
                )
                self._models[model_size] = m
            return m

    def transcribe(self, audio_path: str, *, model_size: str,
                   language: str | None = None) -> TranscriptResult:
        model = self._model(model_size)
        t0 = time.time()
        segments, info = model.transcribe(
            audio_path,
            beam_size=config.BEAM_SIZE,
            vad_filter=config.VAD_FILTER,
            language=language,
        )
        segs = [Segment(round(s.start, 2), round(s.end, 2), s.text.strip())
                for s in segments]  # generator — consuming it does the work
        dt = time.time() - t0
        return TranscriptResult(
            engine=self.name,
            model=model_size,
            language=info.language,
            language_probability=round(float(info.language_probability), 3),
            duration=round(float(info.duration), 2),
            transcribe_seconds=round(dt, 2),
            segments=segs,
        )


# Singleton CPU engine (thread-safe model cache inside).
_cpu = CpuFasterWhisper()


def resolve(engine: str):
    """Pick an engine. 'auto'/'cpu' -> CPU. 'gpu' is reserved for the future
    whisper.cpp/hipBLAS engine (broker-gated); it currently falls back to CPU
    with a log line rather than failing, so callers can already ask for it."""
    if engine in ("gpu", "auto-gpu"):
        # FUTURE: acquire broker lease (consumer='llm'); on grant use
        # GpuWhisperCpp, on deny fall through to CPU. Not yet implemented.
        logger.info("gpu engine not yet implemented — using CPU")
    return _cpu


def warmup(model_size: str | None = None) -> None:
    """Load the default model so the first real request is fast."""
    _cpu._model(model_size or config.MODEL)
