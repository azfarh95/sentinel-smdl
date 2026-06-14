"""Transcript/summary cache + media_status (Phase A) and translate fail-soft (F)."""
import pytest

from app import store, translate


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store.config, "DB_PATH", str(tmp_path / "t.db"))
    store.init()
    return store


def test_transcript_cache_round_trip_and_invalidation(db):
    res = {"language": "en", "language_probability": 0.9, "duration": 10.0,
           "text": "hi there", "segments": [{"start": 0, "end": 5, "text": "hi there"}]}
    db.put_transcript("a.mp4", 1000.0, 500, res)
    hit = db.get_transcript("a.mp4", 1000.0, 500)
    assert hit and hit["text"] == "hi there" and hit["cached"] is True
    # changed size/mtime → cache miss (file changed)
    assert db.get_transcript("a.mp4", 1000.0, 999) is None
    assert db.get_transcript("a.mp4", 9999.0, 500) is None
    assert db.get_transcript("nope.mp4", 1000.0, 500) is None


def test_summary_cache_round_trip(db):
    db.put_summary("a.mp4", 1.0, 9, {"summary": "S", "chapters": [{"start": "00:00", "title": "x"}],
                                     "topics": ["t"], "language": "en", "model": "m"})
    hit = db.get_summary("a.mp4", 1.0, 9)
    assert hit and hit["summary"] == "S" and hit["chapters"][0]["title"] == "x"
    assert db.get_summary("a.mp4", 1.0, 10) is None  # size changed


def test_media_status_flags(db):
    from app.engine import Segment
    db.index_segments("a.mp4", [Segment(0, 1, "x")], [[0.0] * 384], "en")
    db.put_transcript("a.mp4", 1.0, 1, {"language": "en", "segments": [], "text": ""})
    st = db.media_status(["a.mp4", "b.mp4"])
    assert st["a.mp4"]["indexed"] and st["a.mp4"]["segments"] == 1 and st["a.mp4"]["transcribed"]
    assert not st["a.mp4"]["summarized"]
    assert st["b.mp4"] == {"indexed": False, "segments": 0, "transcribed": False, "summarized": False}


def test_translate_disabled_returns_originals(monkeypatch):
    monkeypatch.setattr(translate.config, "TRANSLATE_URL", "")
    assert translate.to_english(["你好"], "zh") == ["你好"]


def test_translate_english_source_is_noop(monkeypatch):
    monkeypatch.setattr(translate.config, "TRANSLATE_URL", "http://x:5000")
    assert translate.to_english(["hello"], "en") == ["hello"]
