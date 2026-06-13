# media-ai sidecar

Transcription / summary / search service for **Sentinel Media**. A **separate
image** from `smdl`: the heavy ML stack (CTranslate2 / faster-whisper, PyAV)
lives here so the OSS/community SMDL web image keeps its *no AI dependency*
promise. SMDL talks to it over HTTP and degrades dark when `SMDL_MEDIA_AI_URL`
is unset.

## Why CPU first

`faster-whisper` (CTranslate2) runs at **~10× realtime on this host's CPU**
(`base`, int8) — a 10-minute video transcribes in ~1 minute **without touching
the GPU**, so it never fights FLUX / Qwen / a game for the 24 GB card. CTranslate2
has **no AMD/ROCm** support, so the eventual *"GPU when the broker says free"*
path uses a separate **whisper.cpp/hipBLAS** engine (broker-gated) — a declared
seam in `app/engine.py`, not yet implemented.

## Phases

1. **Transcription** (this) — `POST /transcribe`, CPU faster-whisper.
2. **Summary + chapters** — Qwen `:1234` under a GPU broker `llm` lease.
3. **Embeddings + semantic search** — CPU embeddings → sqlite-vec → `/search`.
4. **SMDL wiring + UI** — auto-transcribe on download, Library transcript/search.

## API (Phase 1)

```
GET  /healthz      # liveness + active config (no model load)
POST /transcribe   # { path, model?, language?, engine? } -> transcript + segments + speed
```

`path` is resolved under the read-only downloads root
(`MEDIA_AI_DOWNLOADS_DIR`, the same media volume SMDL mounts), traversal
rejected.

```bash
curl -s localhost:8097/transcribe -H 'content-type: application/json' \
  -d '{"path":"Instagram/sherwx/3893755561468095168.mp4","model":"base"}' | jq
```

## Config (env)

| var | default | meaning |
|---|---|---|
| `MEDIA_AI_MODEL` | `base` | whisper size: tiny/base/small/medium/large-v3 |
| `MEDIA_AI_COMPUTE_TYPE` | `int8` | CTranslate2 compute type |
| `MEDIA_AI_CPU_THREADS` | `0` | 0 = all cores; cap on a shared box |
| `MEDIA_AI_MODELS_DIR` | `/models` | model-weights cache (named volume) |
| `MEDIA_AI_DOWNLOADS_DIR` | `/downloads` | media root (mounted read-only) |
| `MEDIA_AI_GPU_BROKER_*` | off | future GPU engine lease (watchdog v2) |
| `MEDIA_AI_QWEN_URL` | — | Phase 2 summary; dark when unset |

## Run

Part of the suite compose (`metamcp-local`), `media` profile:

```bash
docker compose --profile media build media-ai
docker compose --profile media up -d media-ai
```
