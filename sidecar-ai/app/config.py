"""Config — env, read once at import. Mirrors the SMDL convention (env with a
service prefix, sane defaults, degrade-dark for optional integrations)."""
from __future__ import annotations

import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


# ── Service ──────────────────────────────────────────────────────────────────
PORT = _int("MEDIA_AI_PORT", 8097)

# Root that download paths are resolved against. The compose mounts the same
# media volume here (read-only) as SMDL mounts at /downloads, so SMDL can hand
# this service a path relative to /downloads and it resolves to the same file.
DOWNLOADS_DIR = os.environ.get("MEDIA_AI_DOWNLOADS_DIR", "/downloads").rstrip("/") or "/"

# ── Whisper (CPU engine) ─────────────────────────────────────────────────────
# Model size: tiny|base|small|medium|large-v3 (+ .en variants). base int8 ran at
# ~10x realtime on this host's CPU; small ~3-4x with better accuracy. Override
# per-request too.
MODEL = os.environ.get("MEDIA_AI_MODEL", "base").strip() or "base"
MODELS_DIR = os.environ.get("MEDIA_AI_MODELS_DIR", "/models").rstrip("/") or "/models"
COMPUTE_TYPE = os.environ.get("MEDIA_AI_COMPUTE_TYPE", "int8").strip() or "int8"
# 0 => CTranslate2 picks (all cores). Cap it on a shared box so transcription
# doesn't starve the rest of the stack.
CPU_THREADS = _int("MEDIA_AI_CPU_THREADS", 0)
BEAM_SIZE = _int("MEDIA_AI_BEAM_SIZE", 1)
# VAD trims silence/music — big speedup and it returns no segments for
# music-only clips (correctly, instead of hallucinating lyrics).
VAD_FILTER = (os.environ.get("MEDIA_AI_VAD", "true").strip().lower() != "false")

# ── Search (Phase 3 — CPU embeddings + sqlite-vec) ───────────────────────────
# bge-small-en-v1.5: 384-dim, CPU via onnxruntime (no torch, no GPU). Embedding
# the library never contends for the card.
EMBED_MODEL = os.environ.get("MEDIA_AI_EMBED_MODEL", "BAAI/bge-small-en-v1.5").strip()
DB_PATH = os.environ.get("MEDIA_AI_DB_PATH", "/data/media_ai.db").strip()

# ── GPU broker (the "GPU when the broker says free" path; Phase 1 = off) ──────
# CPU is the default engine and needs no GPU. When a GPU engine is added
# (whisper.cpp/hipBLAS, since CTranslate2 has no ROCm), it will acquire a lease
# from the watchdog v2 broker first and fall back to CPU when denied. Off by
# default so Phase 1 never contends for the card.
GPU_BROKER_ENABLED = os.environ.get("MEDIA_AI_GPU_BROKER_ENABLED", "false").strip().lower() == "true"
GPU_BROKER_URL = os.environ.get("MEDIA_AI_GPU_BROKER_URL", "http://host.docker.internal:8200").rstrip("/")
# Reuses the shared 'gpu-broker-client' token (same one SMDL/Coinbox use,
# mirrored as COINBOX_GPU_BROKER_TOKEN in .env.local). No new secret.
GPU_BROKER_TOKEN = os.environ.get("MEDIA_AI_GPU_BROKER_TOKEN", "").strip()

# ── Qwen summary (Phase 2) ───────────────────────────────────────────────────
# llama-swap fronts the on-demand Qwen at :1234 (OpenAI-compatible, TTL evict).
# Summary/chapters check the broker GPU gate (broker.gpu_gate) before calling it
# — media-ai is an LLM-class consumer, it does NOT lease (only FLUX leases).
# Unset QWEN_URL ⇒ summary stays dark (enabled() is False).
QWEN_URL = os.environ.get("MEDIA_AI_QWEN_URL", "").rstrip("/")
QWEN_MODEL = os.environ.get("MEDIA_AI_QWEN_MODEL", "qwen/qwen3.6-27b").strip()
# Generous: a COLD 27B load through llama-swap (≈16 GB GGUF + mmproj into VRAM)
# measured >3 min before the first token on this box; warm calls are seconds.
# On-demand load is the price of being a good GPU citizen (TTL-evict frees the
# card for FLUX/gaming), so the timeout must cover a cold load + generation.
# Phase 4 invokes summary asynchronously so a user never blocks on a cold load.
QWEN_TIMEOUT = float(os.environ.get("MEDIA_AI_QWEN_TIMEOUT", "300"))
QWEN_MAX_TOKENS = _int("MEDIA_AI_QWEN_MAX_TOKENS", 1200)

# Physical VRAM-headroom preflight (app/gpu.py). The broker is blind to GPU users
# that don't lease (e.g. a resident ComfyUI/FLUX model), so before loading the
# 27B we also check actual free VRAM via ComfyUI /system_stats and defer when
# there isn't enough. Unset GPU_STATS_URL ⇒ check skipped (degrade-dark).
GPU_STATS_URL = os.environ.get("MEDIA_AI_GPU_STATS_URL", "").rstrip("/")
# 27B Q4_K_M ≈ 16 GB + KV/overhead. Defer if free VRAM is below this.
MIN_VRAM_GB = float(os.environ.get("MEDIA_AI_MIN_VRAM_GB", "17"))
