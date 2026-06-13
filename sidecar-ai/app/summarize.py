"""Phase 2 — summary + chapters from a transcript, via Qwen on llama-swap.

`enabled()` is False unless MEDIA_AI_QWEN_URL is set (degrade-dark). The caller
(main.py) checks the GPU broker gate first and defers when the card is busy;
this module just does the LLM call + parsing.
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request

from . import config
from .engine import Segment

logger = logging.getLogger("media-ai.summarize")


def enabled() -> bool:
    return bool(config.QWEN_URL)


def _format_transcript(segments: list[Segment]) -> str:
    """Compact, timestamped transcript the model can map chapters onto."""
    lines = []
    for s in segments:
        m, sec = divmod(int(s.start), 60)
        lines.append(f"[{m:02d}:{sec:02d}] {s.text}")
    return "\n".join(lines)


_SYSTEM = (
    "/no_think\n"  # Qwen3 soft switch: skip the reasoning block (prompt-level,
    # so it can't 500 the request the way an unsupported API param can).
    "You summarise media transcripts. You reply with ONLY a single JSON object, "
    "no prose, no markdown fences, no chain-of-thought. Schema:\n"
    '{"summary": str (2-4 sentences), '
    '"chapters": [{"start": "MM:SS", "title": str}], '
    '"topics": [str], "language": str}\n'
    "Chapters must use timestamps that actually appear in the transcript and be "
    "in order. If the clip is too short for multiple chapters, return one chapter "
    "at 00:00. Keep titles under 8 words."
)


def _extract_json(text: str) -> dict:
    """Pull the JSON object out of a model reply that may include <think> blocks,
    code fences, or leading prose."""
    # Drop reasoning blocks some Qwen builds emit.
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    # Strip code fences.
    text = re.sub(r"```(?:json)?", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object in model reply")
    return json.loads(text[start:end + 1])


def _chat(messages: list[dict]) -> str:
    payload = {
        "model": config.QWEN_MODEL,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": config.QWEN_MAX_TOKENS,
        "stream": False,
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{config.QWEN_URL}/v1/chat/completions",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=config.QWEN_TIMEOUT) as r:
            body = json.loads(r.read())
    except urllib.error.HTTPError as e:
        # Surface llama-server's error body — a bare "HTTP 500" is undiagnosable.
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:500]
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f"qwen HTTP {e.code}: {detail or e.reason}") from None
    return body["choices"][0]["message"]["content"]


def summarize(segments: list[Segment], *, detected_language: str | None = None) -> dict:
    """Return {summary, chapters, topics, language, model, summarize_seconds}.
    BLOCKING (LLM call) — run in a thread executor. Raises on Qwen/parse error;
    the caller surfaces it (or defers earlier on the broker gate)."""
    if not enabled():
        raise RuntimeError("summary not configured (MEDIA_AI_QWEN_URL unset)")
    if not any(s.text.strip() for s in segments):
        # Nothing was said (e.g. music-only / VAD dropped everything) — no point
        # invoking the LLM. Check actual text, not the formatted string (which
        # still carries [MM:SS] prefixes even when every segment is empty).
        return {"summary": "", "chapters": [], "topics": [],
                "language": detected_language or "", "model": config.QWEN_MODEL,
                "summarize_seconds": 0.0, "empty": True}
    transcript = _format_transcript(segments)

    user = ("Transcript (timestamps are MM:SS):\n\n" + transcript +
            "\n\nReturn the JSON object now.")
    t0 = time.time()
    raw = _chat([{"role": "system", "content": _SYSTEM},
                 {"role": "user", "content": user}])
    dt = time.time() - t0
    parsed = _extract_json(raw)

    # Normalise / harden the shape.
    chapters = []
    for ch in (parsed.get("chapters") or []):
        if isinstance(ch, dict) and ch.get("title"):
            chapters.append({"start": str(ch.get("start", "00:00")),
                             "title": str(ch["title"])[:80]})
    return {
        "summary": str(parsed.get("summary", "")).strip(),
        "chapters": chapters,
        "topics": [str(t) for t in (parsed.get("topics") or [])][:12],
        "language": str(parsed.get("language") or detected_language or ""),
        "model": config.QWEN_MODEL,
        "summarize_seconds": round(dt, 2),
    }
