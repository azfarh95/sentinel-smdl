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

# ── Qwen summary (Phase 2; off until wired) ──────────────────────────────────
# llama-swap fronts the on-demand Qwen at :1234 (OpenAI-compatible). Summary/
# chapters acquire an 'llm' broker lease before calling it.
QWEN_URL = os.environ.get("MEDIA_AI_QWEN_URL", "").rstrip("/")
