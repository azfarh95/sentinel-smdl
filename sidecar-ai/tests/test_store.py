"""sqlite-vec store round-trip with fake vectors (no embedding model needed)."""
import math

import pytest

from app import store
from app.embed import DIM
from app.engine import Segment


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store.config, "DB_PATH", str(tmp_path / "t.db"))
    store.init()
    return store


def _unit(seed: float) -> list[float]:
    # A deterministic normalized vector that leans on dimension int(seed).
    v = [0.0] * DIM
    v[int(seed) % DIM] = 1.0
    return v


def test_index_and_search_round_trip(db):
    segs = [Segment(0, 5, "alpha"), Segment(5, 10, "beta")]
    vecs = [_unit(1), _unit(2)]
    n = db.index_segments("a.mp4", segs, vecs, "en")
    assert n == 2
    assert db.stats()["segments"] == 2 and db.stats()["media"] == 1

    hits = db.search(_unit(1), k=1)
    assert hits[0]["text"] == "alpha"
    assert hits[0]["media_path"] == "a.mp4"
    assert 0.99 <= hits[0]["score"] <= 1.01  # cosine ~1 for the matching unit vec


def test_reindex_replaces_rows(db):
    db.index_segments("a.mp4", [Segment(0, 1, "old")], [_unit(1)], "en")
    db.index_segments("a.mp4", [Segment(0, 1, "new")], [_unit(1)], "en")
    assert db.stats()["segments"] == 1
    assert db.search(_unit(1), k=1)[0]["text"] == "new"


def test_path_scoped_search(db):
    db.index_segments("a.mp4", [Segment(0, 1, "alpha")], [_unit(1)], "en")
    db.index_segments("b.mp4", [Segment(0, 1, "alpha too")], [_unit(1)], "en")
    hits = db.search(_unit(1), k=5, media_path="b.mp4")
    assert hits and all(h["media_path"] == "b.mp4" for h in hits)
