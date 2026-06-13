"""Summary parsing/formatting + the empty-clip short-circuit (no network/GPU)."""
import pytest

from app import summarize
from app.engine import Segment


def test_extract_json_with_think_and_fences():
    raw = ('<think>reasoning here</think>\n```json\n'
           '{"summary":"S","chapters":[{"start":"00:00","title":"Intro"}],'
           '"topics":["a"],"language":"en"}\n```')
    d = summarize._extract_json(raw)
    assert d["summary"] == "S"
    assert d["chapters"][0]["title"] == "Intro"
    assert d["language"] == "en"


def test_extract_json_leading_prose():
    d = summarize._extract_json('Sure! {"summary": "x", "topics": []}')
    assert d["summary"] == "x"


def test_extract_json_bad_raises():
    with pytest.raises(ValueError):
        summarize._extract_json("not json at all")


def test_format_transcript_timestamps():
    out = summarize._format_transcript([Segment(0, 5, "hi"), Segment(125, 130, "later")])
    assert "[00:00] hi" in out
    assert "[02:05] later" in out


def test_empty_clip_short_circuits_without_chat(monkeypatch):
    # _chat must NOT be called for a music-only / empty-text clip.
    called = {"n": 0}
    monkeypatch.setattr(summarize, "_chat", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    monkeypatch.setattr(summarize.config, "QWEN_URL", "http://x:1")  # enabled()
    out = summarize.summarize([Segment(0, 0, "   ")], detected_language="en")
    assert out["empty"] is True
    assert called["n"] == 0
