"""CPU text embeddings via fastembed (ONNX) — Phase 3 semantic search.

bge-small-en-v1.5 is small, fast, and runs on CPU through onnxruntime (the same
runtime faster-whisper already pulls for VAD) — so NO torch, NO GPU. Embedding
the whole library never contends with FLUX / Qwen / a game for the card.

bge models distinguish documents from queries: passages use `.embed()`, the
search query uses `.query_embed()` (it prepends the retrieval instruction). Using
the right one on each side is what makes the cosine ranking meaningful.
"""
from __future__ import annotations

import logging
import threading

from . import config

logger = logging.getLogger("media-ai.embed")

DIM = 384  # bge-small-en-v1.5

_model = None
_lock = threading.Lock()


def _get():
    global _model
    if _model is None:
        with _lock:
            if _model is None:
                from fastembed import TextEmbedding
                logger.info("loading embedding model=%s", config.EMBED_MODEL)
                _model = TextEmbedding(model_name=config.EMBED_MODEL,
                                       cache_dir=config.MODELS_DIR)
    return _model


def embed_passages(texts: list[str]) -> list[list[float]]:
    """Embed document/segment texts. Returns one 384-float vector per text."""
    if not texts:
        return []
    return [v.tolist() for v in _get().embed(texts)]


def embed_query(text: str) -> list[float]:
    """Embed a search query (with bge's retrieval instruction)."""
    return list(_get().query_embed([text]))[0].tolist()


def warmup() -> None:
    _get()
