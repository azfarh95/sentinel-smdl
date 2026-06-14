"""Optional translation to English via the in-stack libretranslate.

DEGRADE-DARK + FAIL-SOFT: a no-op unless MEDIA_AI_TRANSLATE_URL is set, and on
ANY error it returns the original text — so a libretranslate hiccup never breaks
transcription. Purpose: make non-English transcripts searchable by an English
query (the embedding model bge-small-en is English-only) and readable by the
owner.
"""
from __future__ import annotations

import json
import logging
import urllib.request

from . import config

logger = logging.getLogger("media-ai.translate")


def enabled() -> bool:
    return bool(config.TRANSLATE_URL)


def to_english(texts: list[str], source: str) -> list[str]:
    """Translate a batch of segment texts to English. Returns the originals
    unchanged when disabled, already English, empty, or on any failure."""
    if not texts or not enabled() or not source or source == "en":
        return texts
    payload = {"q": texts, "source": source, "target": "en", "format": "text"}
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(config.TRANSLATE_URL + "/translate", data=data,
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=config.TRANSLATE_TIMEOUT) as r:
            body = json.loads(r.read())
        tt = body.get("translatedText")
        if isinstance(tt, list) and len(tt) == len(texts):
            return [str(x) for x in tt]
        if isinstance(tt, str) and len(texts) == 1:
            return [tt]
        logger.warning("translate: unexpected response shape for source=%s", source)
    except Exception as e:  # noqa: BLE001
        logger.warning("translate failed (%s) — using originals", e)
    return texts
