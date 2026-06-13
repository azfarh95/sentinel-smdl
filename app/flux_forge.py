"""FLUX text-to-image via the host's ComfyUI (Q8 GGUF unet + Turbo LoRA, 8 steps).

Ported from coinbox-credits/app/forge.py (the FLUX "hero lane"; see memory
reference_flux_rocm_speed_levers — ~11x faster per-step than fp8 on RDNA3, no
dequant tax). Powers the Sticker Studio "✨ Generate" feature: a text prompt
becomes a sticker draft the user finishes in the existing editor (cutout /
outline / crop / make).

Talks to ComfyUI's HTTP API directly + read-only (queue a graph → poll /history
→ fetch /view). Never touches the owner-only Forge dashboard (:8830).

DEGRADE-DARK: this module is a no-op unless ``SMDL_COMFY_URL`` is set. The OSS
SMDL image has NO AI dependency and no GPU assumption — the feature simply stays
hidden when the env var is absent (the Sentinel deployment sets it; outside users
don't). The GPU broker lease (watchdog v2) keeps a render from fighting Qwen or a
game for the 24 GB card; it FAILS OPEN if the broker is unreachable.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

# ── Config (env, read once at import) ────────────────────────────────────────
# Empty COMFY_URL → feature disabled (enabled() is False). In-container the host
# ComfyUI is reached via host.docker.internal:8821; if host-gateway won't route
# to it (IPv6 ULA quirk on Docker Desktop, see coinbox), override SMDL_COMFY_URL
# with the host IPv4 (e.g. http://192.168.65.254:8821) WITHOUT touching the
# container's extra_hosts (so the :8200 broker/license path stays on host-gateway).
COMFY_URL = os.environ.get("SMDL_COMFY_URL", "").rstrip("/")
FORGE_POLL_TIMEOUT = float(os.environ.get("SMDL_FORGE_POLL_TIMEOUT", "300"))
FORGE_MAX_PROMPT = int(os.environ.get("SMDL_FORGE_MAX_PROMPT", "600"))

GPU_BROKER_ENABLED = os.environ.get("SMDL_GPU_BROKER_ENABLED", "true").lower() == "true"
GPU_BROKER_URL = os.environ.get("SMDL_GPU_BROKER_URL", "http://host.docker.internal:8200").rstrip("/")
# Reuses the shared 'gpu-broker-client' service token (mirrored into the compose
# env as COINBOX_GPU_BROKER_TOKEN). No new secret — SMDL is just another flux
# consumer on the same broker.
GPU_BROKER_TOKEN = os.environ.get("SMDL_GPU_BROKER_TOKEN", "").strip()

# FLUX model filenames — must exist in the host ComfyUI's models dirs.
FLUX_GGUF = "flux1-dev-Q8_0.gguf"
FLUX_T5 = "t5xxl_fp8_e4m3fn.safetensors"
FLUX_CLIP_L = "clip_l.safetensors"
FLUX_VAE = "ae.safetensors"
FLUX_TURBO_LORA = "flux1-turbo-alpha.safetensors"


def enabled() -> bool:
    """True iff image generation is wired (a ComfyUI URL is configured)."""
    return bool(COMFY_URL)


def _post_json(url: str, payload: dict, timeout: float = 30):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _get(url: str, timeout: float = 30) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def flux_graph(positive: str, seed: int, prefix: str = "smdl",
               steps: int = 8, guidance: float = 3.5, w: int = 1024, h: int = 1024) -> dict:
    return {
        "11": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": FLUX_GGUF}},
        "12": {"class_type": "DualCLIPLoader",
               "inputs": {"clip_name1": FLUX_T5, "clip_name2": FLUX_CLIP_L, "type": "flux"}},
        "13": {"class_type": "VAELoader", "inputs": {"vae_name": FLUX_VAE}},
        "14": {"class_type": "LoraLoaderModelOnly",
               "inputs": {"model": ["11", 0], "lora_name": FLUX_TURBO_LORA, "strength_model": 1.0}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": positive, "clip": ["12", 0]}},
        "26": {"class_type": "FluxGuidance", "inputs": {"guidance": guidance, "conditioning": ["6", 0]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["12", 0]}},
        "5": {"class_type": "EmptySD3LatentImage", "inputs": {"width": w, "height": h, "batch_size": 1}},
        "3": {"class_type": "KSampler",
              "inputs": {"seed": seed, "steps": steps, "cfg": 1.0, "sampler_name": "euler",
                         "scheduler": "simple", "denoise": 1.0, "model": ["14", 0],
                         "positive": ["26", 0], "negative": ["7", 0], "latent_image": ["5", 0]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["13", 0]}},
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": prefix, "images": ["8", 0]}},
    }


def generate(positive: str, seed: int, poll_timeout: float | None = None) -> bytes:
    """Queue a FLUX render on ComfyUI and return the PNG bytes. BLOCKING (~50s
    with a cold model load) — call from a thread executor. Raises on ComfyUI
    error or timeout (the caller surfaces the failure)."""
    if not COMFY_URL:
        raise RuntimeError("image generation not configured (SMDL_COMFY_URL unset)")
    poll_timeout = poll_timeout or FORGE_POLL_TIMEOUT
    graph = flux_graph(positive, seed)
    cid = uuid.uuid4().hex
    # Generous per-request timeouts: ComfyUI is single-process and its HTTP can
    # block while it loads the ~18 GB FLUX model into VRAM on a COLD render — a
    # single /history poll can stall ~30-50 s. 90 s per call tolerates that; the
    # overall budget is poll_timeout.
    submit = _post_json(f"{COMFY_URL}/prompt", {"prompt": graph, "client_id": cid}, timeout=90)
    pid = submit["prompt_id"]
    deadline = time.time() + poll_timeout
    while True:
        hist = json.loads(_get(f"{COMFY_URL}/history/{pid}", timeout=90))
        if pid in hist:
            entry = hist[pid]
            status = entry.get("status", {})
            if status.get("status_str") == "error":
                raise RuntimeError(f"ComfyUI reported error: {status}")
            imgs = entry.get("outputs", {}).get("9", {}).get("images", [])
            if imgs:
                im = imgs[0]
                q = urllib.parse.urlencode({"filename": im["filename"],
                                            "subfolder": im.get("subfolder", ""),
                                            "type": im.get("type", "output")})
                return _get(f"{COMFY_URL}/view?{q}", timeout=60)
        if time.time() >= deadline:
            raise TimeoutError(f"ComfyUI did not finish within {poll_timeout:.0f}s")
        time.sleep(2)


def broker_lease(action: str, holder: str = "smdl-forge") -> bool:
    """Acquire/release the GPU lease from the watchdog broker (action in
    {'acquire','release'}). Returns True on acquire iff the render may proceed.

    FAIL-OPEN: if the broker is disabled or unreachable, acquire returns True so
    SMDL keeps working when the watchdog is down. Release is best-effort. BLOCKING
    (short timeout) — call from a thread executor."""
    if not GPU_BROKER_ENABLED:
        return True
    url = f"{GPU_BROKER_URL}/api/v2/gpu-broker/lease/{action}"
    headers = {"Content-Type": "application/json"}
    if GPU_BROKER_TOKEN:
        headers["X-Sentinel-Service-Token"] = GPU_BROKER_TOKEN
    try:
        req = urllib.request.Request(
            url, data=json.dumps({"consumer": "flux", "holder": holder}).encode(),
            headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            d = json.loads(r.read())
        if action == "acquire":
            granted = bool(d.get("granted"))
            if not granted:
                logger.info("gpu broker denied FLUX lease: %s", d.get("reason", ""))
            return granted
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("gpu broker %s failed (%s) — fail-open", action, e)
        return True
