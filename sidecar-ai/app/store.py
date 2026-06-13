"""Transcript + embedding store — sqlite + sqlite-vec (Phase 3).

One sqlite DB at MEDIA_AI_DB_PATH holds:
  * `segments`     — one row per transcript segment (path, timestamps, text, lang)
  * `vec_segments` — a sqlite-vec vec0 virtual table of the segment embeddings,
                     keyed by the same rowid as `segments`

Re-indexing a media path replaces its rows (idempotent). Search is a vec KNN
joined back to `segments` for the text + timestamps.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time

import sqlite_vec

from . import config
from .embed import DIM

logger = logging.getLogger("media-ai.store")

_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    db = sqlite3.connect(config.DB_PATH)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.row_factory = sqlite3.Row
    return db


def init() -> None:
    with _lock, _connect() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS segments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                media_path TEXT NOT NULL,
                seg_idx INTEGER NOT NULL,
                start REAL NOT NULL,
                end REAL NOT NULL,
                text TEXT NOT NULL,
                lang TEXT,
                indexed_at REAL NOT NULL
            )""")
        db.execute("CREATE INDEX IF NOT EXISTS ix_seg_path ON segments(media_path)")
        # cosine distance (bge vectors are normalized) so `score = 1 - distance`
        # is a true cosine similarity in [0,1]; vec0 defaults to L2 otherwise.
        db.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_segments "
            f"USING vec0(segment_id INTEGER PRIMARY KEY, "
            f"embedding FLOAT[{DIM}] distance_metric=cosine)")


def delete_media(db: sqlite3.Connection, media_path: str) -> None:
    rows = db.execute("SELECT id FROM segments WHERE media_path = ?", (media_path,)).fetchall()
    for r in rows:
        db.execute("DELETE FROM vec_segments WHERE segment_id = ?", (r["id"],))
    db.execute("DELETE FROM segments WHERE media_path = ?", (media_path,))


def index_segments(media_path: str, segments: list, vectors: list[list[float]],
                   lang: str | None) -> int:
    """Replace all rows for media_path with these segments + vectors. Returns the
    number of segments stored. `segments` are engine.Segment; len == len(vectors)."""
    now = time.time()
    with _lock, _connect() as db:
        delete_media(db, media_path)
        n = 0
        for i, (seg, vec) in enumerate(zip(segments, vectors)):
            cur = db.execute(
                "INSERT INTO segments(media_path, seg_idx, start, end, text, lang, indexed_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (media_path, i, seg.start, seg.end, seg.text, lang, now))
            db.execute("INSERT INTO vec_segments(segment_id, embedding) VALUES (?, ?)",
                       (cur.lastrowid, sqlite_vec.serialize_float32(vec)))
            n += 1
        return n


def search(query_vec: list[float], k: int = 10, media_path: str | None = None) -> list[dict]:
    """KNN over segment embeddings → timestamped hits. When media_path is given,
    over-fetch then filter to that file (vec0 KNN is global)."""
    fetch = k if media_path is None else max(k * 8, 64)
    with _lock, _connect() as db:
        rows = db.execute(
            "SELECT v.segment_id AS id, v.distance AS distance, "
            "  s.media_path, s.start, s.end, s.text, s.lang "
            "FROM vec_segments v JOIN segments s ON s.id = v.segment_id "
            "WHERE v.embedding MATCH ? AND k = ? ORDER BY v.distance",
            (sqlite_vec.serialize_float32(query_vec), fetch)).fetchall()
    hits = []
    for r in rows:
        if media_path and r["media_path"] != media_path:
            continue
        hits.append({"media_path": r["media_path"], "start": r["start"], "end": r["end"],
                     "text": r["text"], "lang": r["lang"],
                     "score": round(1.0 - r["distance"], 4), "distance": round(r["distance"], 4)})
        if len(hits) >= k:
            break
    return hits


def stats() -> dict:
    with _lock, _connect() as db:
        n_seg = db.execute("SELECT COUNT(*) c FROM segments").fetchone()["c"]
        n_media = db.execute("SELECT COUNT(DISTINCT media_path) c FROM segments").fetchone()["c"]
    return {"segments": n_seg, "media": n_media}
