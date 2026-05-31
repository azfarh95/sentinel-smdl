import json
import os
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

DB_PATH = os.environ.get("DB_PATH", "/data/jobs.db")


async def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS url_cache (
                url        TEXT PRIMARY KEY,
                files      TEXT NOT NULL,
                platform   TEXT,
                uploader   TEXT,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        # Per-user download history. url_cache stays a global content cache;
        # this table is the audit trail of who-downloaded-what.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS download_history (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id       INTEGER NOT NULL,
                url           TEXT NOT NULL,
                files         TEXT NOT NULL,
                platform      TEXT,
                uploader      TEXT,
                downloaded_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_dh_chat_time
            ON download_history (chat_id, downloaded_at DESC)
        """)
        # Users directory — populated implicitly on first interaction by
        # auth.record_interaction(). Status drives the gate. New users land
        # as 'pending' and need owner approval before they're 'active'.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id            INTEGER PRIMARY KEY,
                username           TEXT,
                first_name         TEXT,
                last_name          TEXT,
                status             TEXT NOT NULL DEFAULT 'pending',
                first_seen         TEXT NOT NULL,
                last_seen          TEXT NOT NULL,
                interaction_count  INTEGER NOT NULL DEFAULT 0,
                banned_at          TEXT,
                banned_reason      TEXT,
                pending_code       TEXT,
                pending_expires_at TEXT
            )
        """)
        # Idempotent column adds for upgraders (SQLite is permissive about
        # ALTER ADD if the column doesn't exist yet — but it errors on dup,
        # so we ask the schema first).
        async with db.execute("PRAGMA table_info(users)") as cur:
            cols = {row[1] async for row in cur}
        if "pending_code" not in cols:
            await db.execute("ALTER TABLE users ADD COLUMN pending_code TEXT")
        if "pending_expires_at" not in cols:
            await db.execute("ALTER TABLE users ADD COLUMN pending_expires_at TEXT")
        # Moderation audit trail — one row per owner action on a user
        # (approve / deny / ban / unban / approve_by_code). Gives the Server
        # tab a legible history instead of inferring state from the users row.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS auth_events (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id    INTEGER,
                action     TEXT NOT NULL,
                actor_id   INTEGER,
                detail     TEXT,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_ae_time
            ON auth_events (created_at DESC)
        """)
        # Notifications-feed read marker — one row per user storing the last
        # time they opened the consolidated feed. The feed itself is computed
        # on read by merging existing sources (downloads / recordings / auth
        # events); this table only tracks unread state, so we never have to
        # write a row per event.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS notification_reads (
                chat_id INTEGER PRIMARY KEY,
                seen_at TEXT NOT NULL
            )
        """)
        # Approved groups — chat_ids of Telegram groups the owner has trusted.
        # Members of these groups can use the bot WITHOUT per-user approval.
        # Trade-off: bot replies are visible to the whole group; download
        # history attributes to the group's chat_id (shared by all members).
        await db.execute("""
            CREATE TABLE IF NOT EXISTS approved_groups (
                chat_id     INTEGER PRIMARY KEY,
                label       TEXT,
                approved_by INTEGER NOT NULL,
                approved_at TEXT NOT NULL
            )
        """)
        # Profile scraper (V1) — owner-only IG/TikTok profile auto-monitor.
        # One row per profile being scraped. `last_post_ids` is the JSON list
        # of the most-recent N post IDs seen on the last successful probe;
        # next probe diffs against this to find new posts. `next_probe_at` is
        # the scheduler index — the burst-session scheduler picks profiles
        # whose `next_probe_at` is in the past and bundles them into a session.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS scraper_profiles (
                url            TEXT PRIMARY KEY,
                platform       TEXT NOT NULL,
                username       TEXT,
                label          TEXT,
                enabled        INTEGER NOT NULL DEFAULT 1,
                last_check_at  TEXT,
                next_probe_at  TEXT,
                last_post_ids  TEXT NOT NULL DEFAULT '[]',
                downloaded_count INTEGER NOT NULL DEFAULT 0,
                failure_count  INTEGER NOT NULL DEFAULT 0,
                last_error     TEXT,
                last_http_code INTEGER,
                added_by       INTEGER NOT NULL,
                added_at       TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_sp_next
            ON scraper_profiles (next_probe_at) WHERE enabled = 1
        """)
        # Per-cookie state — failure-cluster detection, daily ceiling, warmup
        # anchor, stable UA pinning. Cookie identity is the file basename
        # (e.g. 'instagram', 'tiktok') matching `_resolve_cookies()` in
        # downloader.py. Auto-populated on first probe.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS scraper_cookies (
                cookie_key        TEXT PRIMARY KEY,
                first_seen_at     TEXT NOT NULL,
                user_agent        TEXT,
                last_block_at     TEXT,
                cooldown_until    TEXT,
                probes_today      INTEGER NOT NULL DEFAULT 0,
                probes_today_date TEXT,
                consecutive_blocks INTEGER NOT NULL DEFAULT 0,
                alerted_at        TEXT
            )
        """)
        # ── Sticker maker ────────────────────────────────────────────────────
        # Drafts = user-uploaded videos pending edit. TTL 6h via cleanup loop.
        # Stickers = finalized rows committed to a user's pack.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sticker_drafts (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id           INTEGER NOT NULL,
                telegram_file_id  TEXT NOT NULL,
                file_path         TEXT NOT NULL,
                mime_type         TEXT,
                duration_s        REAL,
                width             INTEGER,
                height            INTEGER,
                uploaded_at       TEXT NOT NULL,
                expires_at        TEXT NOT NULL,
                status            TEXT NOT NULL DEFAULT 'awaiting_edit',
                error             TEXT
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS ix_sticker_drafts_user_status
            ON sticker_drafts(user_id, status)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS ix_sticker_drafts_expires
            ON sticker_drafts(expires_at)
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sticker_packs (
                user_id      INTEGER PRIMARY KEY,
                pack_name    TEXT NOT NULL UNIQUE,
                pack_title   TEXT NOT NULL,
                telegram_url TEXT,
                created_at   TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS stickers (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id           INTEGER NOT NULL,
                pack_name         TEXT NOT NULL,
                source_draft_id   INTEGER,
                emoji             TEXT NOT NULL,
                telegram_file_id  TEXT,
                webm_path         TEXT,
                created_at        TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS ix_stickers_user
            ON stickers(user_id)
        """)
        # ── License keys ─────────────────────────────────────────────────────
        # The operator (this private instance) is the issuing authority for
        # license keys that gate the distributed Community / Family APKs. The
        # key the owner hands out is `SMDL-<TIER>.<key_id>.<secret>`; we store
        # only an HMAC of the secret (so a DB leak doesn't yield usable keys).
        # Every key is time-limited (expires_at always set). Validation is
        # online: the APK calls /api/license/validate, which checks status +
        # expiry + seats here and returns a grant the APK caches for an offline
        # grace window. See app/licensing.py for the crypto/format.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS license_keys (
                key_id      TEXT PRIMARY KEY,
                tier        TEXT NOT NULL,
                issued_to   TEXT,
                seats       INTEGER NOT NULL DEFAULT 1,
                secret_hash TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'active',
                issued_at   TEXT NOT NULL,
                expires_at  TEXT NOT NULL,
                revoked_at  TEXT,
                note        TEXT
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS ix_license_status
            ON license_keys(status)
        """)
        # One row per (key, device). Seat enforcement counts distinct devices
        # per key; an already-seen device re-validating is free.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS license_activations (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                key_id       TEXT NOT NULL,
                device_id    TEXT NOT NULL,
                device_label TEXT,
                first_seen   TEXT NOT NULL,
                last_seen    TEXT NOT NULL,
                UNIQUE(key_id, device_id)
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS ix_license_activation_key
            ON license_activations(key_id)
        """)
        # ── Premium users (operator manifest) ────────────────────────────────
        # Maps an external identity (Telegram chat_id, Google sub, e-mail) to
        # a plan baked into grants without going through the license-key rail.
        # Used on the community/play deployments so the operator can mark a
        # specific identity as plus/family without minting+handing-out a key.
        # (identity_type, identity_value) is the lookup key; UNIQUE so a single
        # identity holds at most one row. expires_at is optional — NULL means
        # "no expiry until removed". Plan must be a key of entitlements.PLANS.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS premium_users (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                identity_type  TEXT NOT NULL,
                identity_value TEXT NOT NULL,
                plan           TEXT NOT NULL,
                notes          TEXT,
                expires_at     TEXT,
                created_at     TEXT NOT NULL,
                updated_at     TEXT NOT NULL,
                UNIQUE(identity_type, identity_value)
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS ix_premium_identity
            ON premium_users(identity_type, identity_value)
        """)
        # ── Beta keys (extra-scope issuance, NOT billing) ────────────────────
        # Owner-only mint of opaque keys that unlock NAMED extra scopes (e.g.
        # `smdl.tv.recorder.beta`) on top of whatever plan the redeemer already
        # has. Distinct from license_keys: a beta key does not change the user's
        # plan or appear in the billing rail — it only attaches extra scopes to
        # their session cookie via auth_v2 on redemption. Stored as HMAC of the
        # secret half so a DB leak doesn't yield usable keys (mirrors
        # license_keys.secret_hash). Each row is one mint; once redeemed,
        # redeemed_by_user_id pins it to a single user (no seat-sharing).
        await db.execute("""
            CREATE TABLE IF NOT EXISTS beta_keys (
                key_id              TEXT PRIMARY KEY,
                secret_hash         TEXT NOT NULL,
                label               TEXT,
                extra_scopes        TEXT NOT NULL,
                expires_at          TEXT,
                created_at          TEXT NOT NULL,
                created_by          INTEGER,
                redeemed_by_user_id TEXT,
                redeemed_at         TEXT,
                revoked_at          TEXT,
                note                TEXT
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS ix_beta_keys_redeemed
            ON beta_keys(redeemed_by_user_id)
        """)
        # ── OAuth identities (Google sign-in) ────────────────────────────────
        # One row per (provider, subject). Populated by /auth/google/callback
        # on first sign-in; updated on every subsequent sign-in. This is the
        # identity directory for non-Telegram users — Telegram users live in
        # the `users` table keyed by chat_id.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS oauth_identities (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                provider      TEXT NOT NULL,
                subject       TEXT NOT NULL,
                email         TEXT,
                name          TEXT,
                picture_url   TEXT,
                first_seen    TEXT NOT NULL,
                last_seen     TEXT NOT NULL,
                UNIQUE(provider, subject)
            )
        """)
        await db.commit()


async def is_group_approved(chat_id: int) -> bool:
    """Fast lookup used by the auth gate. Expects a negative chat_id (groups)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM approved_groups WHERE chat_id = ? LIMIT 1",
            (int(chat_id),),
        ) as cur:
            return (await cur.fetchone()) is not None


async def list_approved_groups() -> list[dict]:
    out: list[dict] = []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM approved_groups ORDER BY approved_at DESC"
        ) as cur:
            async for row in cur:
                out.append(dict(row))
    return out


async def approve_group(chat_id: int, label: str | None,
                         approved_by: int) -> bool:
    """Insert (or refresh label for) an approved group. Refuses positive
    chat_ids — those are DMs and use the per-user flow."""
    if int(chat_id) >= 0:
        return False
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO approved_groups (chat_id, label, approved_by, approved_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                label = excluded.label
        """, (int(chat_id), label, int(approved_by), now))
        await db.commit()
        return True


async def unapprove_group(chat_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM approved_groups WHERE chat_id = ?",
            (int(chat_id),),
        )
        await db.commit()
        return (cur.rowcount or 0) > 0


import secrets as _secrets
from datetime import timedelta as _timedelta


PENDING_CODE_TTL = _timedelta(minutes=1)


def _generate_approval_code() -> str:
    """Cryptographically-random 9-digit code, hyphen-grouped for readability.
    Format: '123-456-789'. ~10^9 entropy — easily brute-forceable in pure
    isolation, but the gate is also chat_id-bound, single-use, and 24h-TTL."""
    n = _secrets.randbelow(10**9)
    s = f"{n:09d}"
    return f"{s[0:3]}-{s[3:6]}-{s[6:9]}"


def _norm_code(code: str) -> str:
    """Strip hyphens/spaces, accept either '123456789' or '123-456-789'."""
    return "".join(c for c in (code or "") if c.isdigit())


def _is_owner_chat(chat_id: int) -> bool:
    """Owner check that doesn't import auth.py (which imports us). Reads the
    config module's OWNER_CHAT_ID directly. Used for fast-pathing owner row
    creation to 'active' so it never appears in pending lists."""
    try:
        from .config import OWNER_CHAT_ID
        return OWNER_CHAT_ID is not None and int(chat_id) == int(OWNER_CHAT_ID)
    except Exception:
        return False


async def record_interaction(chat_id: int, username: str | None = None,
                              first_name: str | None = None,
                              last_name: str | None = None) -> dict:
    """UPSERT a user row on every bot interaction. Returns the post-update row.

    New users land as 'pending' with a fresh 9-digit code (1-min TTL).
    EXCEPT the owner — owner rows are always created as 'active' with no
    code, so they never surface in the Admin pending list.

    Existing-pending users get a NEW code if the old one expired; otherwise
    the existing one is preserved. Active/banned rows are never auto-flipped
    here — only the owner can promote/demote via admin endpoints."""
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    is_owner = _is_owner_chat(chat_id)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE chat_id = ?", (int(chat_id),)) as cur:
            row = await cur.fetchone()
        if row is None:
            if is_owner:
                await db.execute("""
                    INSERT INTO users
                        (chat_id, username, first_name, last_name,
                         status, first_seen, last_seen, interaction_count)
                    VALUES (?, ?, ?, ?, 'active', ?, ?, 1)
                """, (int(chat_id), username, first_name, last_name, now, now))
                await db.commit()
                return {"chat_id": int(chat_id), "username": username,
                        "first_name": first_name, "last_name": last_name,
                        "status": "active", "first_seen": now, "last_seen": now,
                        "interaction_count": 1, "banned_at": None,
                        "banned_reason": None, "pending_code": None,
                        "pending_expires_at": None}
            code = _generate_approval_code()
            expiry = (now_dt + PENDING_CODE_TTL).isoformat()
            await db.execute("""
                INSERT INTO users
                    (chat_id, username, first_name, last_name,
                     status, first_seen, last_seen, interaction_count,
                     pending_code, pending_expires_at)
                VALUES (?, ?, ?, ?, 'pending', ?, ?, 1, ?, ?)
            """, (int(chat_id), username, first_name, last_name, now, now, code, expiry))
            await db.commit()
            return {"chat_id": int(chat_id), "username": username,
                    "first_name": first_name, "last_name": last_name,
                    "status": "pending", "first_seen": now, "last_seen": now,
                    "interaction_count": 1, "banned_at": None, "banned_reason": None,
                    "pending_code": code, "pending_expires_at": expiry}
        # Existing row — refresh contact info, bump counters.
        await db.execute("""
            UPDATE users SET
                username          = COALESCE(?, username),
                first_name        = COALESCE(?, first_name),
                last_name         = COALESCE(?, last_name),
                last_seen         = ?,
                interaction_count = interaction_count + 1
            WHERE chat_id = ?
        """, (username, first_name, last_name, now, int(chat_id)))
        # If pending and code expired, rotate to a new code.
        if (row["status"] or "").lower() == "pending":
            expiry_str = row["pending_expires_at"] or ""
            try:
                expired = (not expiry_str) or datetime.fromisoformat(expiry_str) < now_dt
            except Exception:
                expired = True
            if expired:
                new_code = _generate_approval_code()
                new_expiry = (now_dt + PENDING_CODE_TTL).isoformat()
                await db.execute("""
                    UPDATE users SET pending_code = ?, pending_expires_at = ?
                    WHERE chat_id = ?
                """, (new_code, new_expiry, int(chat_id)))
        await db.commit()
        async with db.execute("SELECT * FROM users WHERE chat_id = ?", (int(chat_id),)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else {}


async def rotate_pending_code(chat_id: int) -> dict | None:
    """Force-generate a fresh approval code for a pending user. Returns the
    updated row, or None if the user isn't in 'pending' state (active users
    are already approved; banned users must stay banned). Used by the
    /regenerate_token bot command when an old code expired."""
    now_dt = datetime.now(timezone.utc)
    new_code = _generate_approval_code()
    new_expiry = (now_dt + PENDING_CODE_TTL).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            UPDATE users SET pending_code = ?, pending_expires_at = ?
            WHERE chat_id = ? AND status = 'pending'
        """, (new_code, new_expiry, int(chat_id)))
        await db.commit()
        if (cur.rowcount or 0) == 0:
            return None
        async with db.execute("SELECT * FROM users WHERE chat_id = ?", (int(chat_id),)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None


async def find_user_by_pending_code(code: str) -> dict | None:
    """Look up a user by their pending approval code. Returns None if not
    found, expired, or already approved."""
    normalized = _norm_code(code)
    if len(normalized) != 9:
        return None
    formatted = f"{normalized[0:3]}-{normalized[3:6]}-{normalized[6:9]}"
    now_dt = datetime.now(timezone.utc)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE status = 'pending' AND pending_code = ?",
            (formatted,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        # Expiry check (rotation happens on next /start, but a stale code
        # presented now must not silently approve).
        try:
            if row["pending_expires_at"] and \
               datetime.fromisoformat(row["pending_expires_at"]) < now_dt:
                return None
        except Exception:
            return None
        return dict(row)


async def approve_user(chat_id: int) -> bool:
    """Flip a user to 'active', clear the pending code. Idempotent on
    already-active rows. Refuses to operate on banned rows — admin must
    explicitly unban first."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            UPDATE users SET status = 'active',
                             pending_code = NULL,
                             pending_expires_at = NULL
            WHERE chat_id = ?
              AND status IN ('pending', 'active')
        """, (int(chat_id),))
        await db.commit()
        return (cur.rowcount or 0) > 0


async def get_user(chat_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE chat_id = ?", (int(chat_id),)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def list_users() -> list[dict]:
    out: list[dict] = []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT * FROM users
            ORDER BY (status='banned') DESC, last_seen DESC
        """) as cur:
            async for row in cur:
                out.append(dict(row))
    return out


async def set_user_status(chat_id: int, status: str, reason: str | None = None) -> bool:
    """Flip status to 'active' or 'banned'. Returns True if a row was updated."""
    if status not in ("active", "banned"):
        return False
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        if status == "banned":
            cur = await db.execute("""
                UPDATE users SET status = 'banned', banned_at = ?, banned_reason = ?
                WHERE chat_id = ?
            """, (now, reason, int(chat_id)))
        else:
            cur = await db.execute("""
                UPDATE users SET status = 'active', banned_at = NULL, banned_reason = NULL
                WHERE chat_id = ?
            """, (int(chat_id),))
        await db.commit()
        return (cur.rowcount or 0) > 0


async def log_auth_event(action: str, chat_id: int | None = None,
                         actor_id: int | None = None,
                         detail: str | None = None) -> None:
    """Append a moderation event. Never raises — an audit-log write must not
    block the action it records."""
    try:
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO auth_events (chat_id, action, actor_id, detail, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (None if chat_id is None else int(chat_id), action,
                  None if actor_id is None else int(actor_id), detail, now))
            await db.commit()
    except Exception:
        pass


async def list_auth_events(limit: int = 50) -> list[dict]:
    out: list[dict] = []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT id, chat_id, action, actor_id, detail, created_at
            FROM auth_events
            ORDER BY id DESC
            LIMIT ?
        """, (int(limit),)) as cur:
            async for row in cur:
                out.append(dict(row))
    return out


async def get_notifications_seen_at(chat_id: int) -> str | None:
    """Last time this user opened the notifications feed (ISO-8601) or None."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT seen_at FROM notification_reads WHERE chat_id = ?",
            (int(chat_id),),
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else None


async def mark_notifications_seen(chat_id: int, seen_at: str) -> None:
    """Upsert the per-user feed read marker. Never raises (best-effort)."""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO notification_reads (chat_id, seen_at)
                VALUES (?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET seen_at = excluded.seen_at
            """, (int(chat_id), seen_at))
            await db.commit()
    except Exception:
        pass


async def delete_user_account(chat_id: int) -> dict:
    """Hard-delete a user and ALL their personal data (Play account-deletion
    requirement / GDPR erasure). Telegram chat_id == user_id, so this purges
    every per-user table keyed on either. Returns rows removed per table.

    Does NOT touch owner-managed/shared rows (approved_groups) or the license
    rail (license_keys/activations are keyed by key_id/device, not a user) —
    those are not the account holder's personal data and the owner revokes
    licenses separately.
    """
    cid = int(chat_id)
    removed: dict[str, int] = {}
    async with aiosqlite.connect(DB_PATH) as db:
        for table, col in (
            ("download_history", "chat_id"),
            ("notification_reads", "chat_id"),
            ("stickers", "user_id"),
            ("sticker_drafts", "user_id"),
            ("sticker_packs", "user_id"),
            ("users", "chat_id"),
        ):
            cur = await db.execute(f"DELETE FROM {table} WHERE {col} = ?", (cid,))
            removed[table] = cur.rowcount or 0
        await db.commit()
    return {"chat_id": cid, "removed": removed,
            "total": sum(removed.values())}


def _normalise_url(url: str) -> str:
    return url.strip().rstrip("/")


async def get_url_cache(url: str) -> dict | None:
    """Return cached entry only if URL was downloaded before AND all files still exist on disk."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM url_cache WHERE url = ?", (_normalise_url(url),)
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            d = dict(row)
    try:
        files = json.loads(d.get("files") or "[]")
    except Exception:
        return None
    if not files:
        return None
    if not all(Path(f).exists() for f in files):
        return None
    d["files"] = files
    return d


async def set_url_cache(url: str, files: list[str], platform: str | None, uploader: str | None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO url_cache (url, files, platform, uploader, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                files      = excluded.files,
                platform   = excluded.platform,
                uploader   = excluded.uploader,
                created_at = excluded.created_at
        """, (
            _normalise_url(url),
            json.dumps(files),
            platform,
            uploader,
            datetime.now(timezone.utc).isoformat(),
        ))
        await db.commit()


async def cache_stats() -> dict:
    """Return {count, oldest, newest} for the URL cache."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*), MIN(created_at), MAX(created_at) FROM url_cache"
        ) as cur:
            row = await cur.fetchone()
    if not row or not row[0]:
        return {"count": 0, "oldest": None, "newest": None}
    return {"count": row[0], "oldest": row[1], "newest": row[2]}


async def clear_cache(url: str | None = None) -> int:
    """Clear cache. If url given, only that entry; otherwise everything.
    Returns the count of rows removed."""
    async with aiosqlite.connect(DB_PATH) as db:
        if url is None:
            cur = await db.execute("DELETE FROM url_cache")
        else:
            cur = await db.execute("DELETE FROM url_cache WHERE url = ?", (_normalise_url(url),))
        await db.commit()
        return cur.rowcount or 0


async def record_download(chat_id: int, url: str, files: list[str],
                          platform: str | None, uploader: str | None) -> None:
    """Append a row to download_history. Never raises — failures are swallowed
    upstream (this is audit/telemetry, not load-bearing for the user flow)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO download_history
                (chat_id, url, files, platform, uploader, downloaded_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            int(chat_id),
            _normalise_url(url),
            json.dumps(files),
            platform,
            uploader,
            datetime.now(timezone.utc).isoformat(),
        ))
        await db.commit()


async def list_download_history(chat_id: int, limit: int = 50) -> list[dict]:
    """Return the most recent `limit` downloads for a specific chat_id, newest first.
    Each row: {id, url, files (list), platform, uploader, downloaded_at}."""
    out: list[dict] = []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT id, url, files, platform, uploader, downloaded_at
            FROM download_history
            WHERE chat_id = ?
            ORDER BY downloaded_at DESC
            LIMIT ?
        """, (int(chat_id), int(limit))) as cur:
            async for row in cur:
                d = dict(row)
                try: d["files"] = json.loads(d.get("files") or "[]")
                except Exception: d["files"] = []
                out.append(d)
    return out


async def get_download(chat_id: int, hist_id: int) -> dict | None:
    """Fetch a single download_history row, scoped to chat_id so a user can
    only ever address their own history. Returns {id, url, files (list),
    platform, uploader, downloaded_at} or None."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT id, url, files, platform, uploader, downloaded_at
            FROM download_history
            WHERE id = ? AND chat_id = ?
        """, (int(hist_id), int(chat_id))) as cur:
            row = await cur.fetchone()
    if row is None:
        return None
    d = dict(row)
    try: d["files"] = json.loads(d.get("files") or "[]")
    except Exception: d["files"] = []
    return d


async def clear_download_history(chat_id: int) -> int:
    """Delete all download_history rows for this chat_id. Returns the number
    of rows deleted. The global url_cache is untouched — it's a content
    cache shared across users, not personal history."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "DELETE FROM download_history WHERE chat_id = ?",
            (int(chat_id),),
        ) as cur:
            n = cur.rowcount or 0
        await db.commit()
        return n


async def get_setting(key: str, default: str = "") -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else default


async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (key, value))
        await db.commit()


# ── Profile scraper helpers ──────────────────────────────────────────────────


async def scraper_add_profile(url: str, platform: str, username: str | None,
                              label: str | None, added_by: int) -> bool:
    """Insert a profile to monitor. Idempotent — duplicate URL returns False.
    Caller is expected to have already validated platform + extracted username."""
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute("""
                INSERT INTO scraper_profiles
                    (url, platform, username, label, enabled, added_by, added_at,
                     next_probe_at)
                VALUES (?, ?, ?, ?, 1, ?, ?, ?)
            """, (_normalise_url(url), platform, username, label, int(added_by), now, now))
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def scraper_remove_profile(url: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM scraper_profiles WHERE url = ?",
            (_normalise_url(url),),
        )
        await db.commit()
        return (cur.rowcount or 0) > 0


async def scraper_list_profiles(platform: str | None = None,
                                 enabled_only: bool = False) -> list[dict]:
    out: list[dict] = []
    q = "SELECT * FROM scraper_profiles"
    args: tuple = ()
    where: list[str] = []
    if platform:
        where.append("platform = ?")
        args = args + (platform,)
    if enabled_only:
        where.append("enabled = 1")
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY platform, username, url"
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(q, args) as cur:
            async for row in cur:
                d = dict(row)
                try:    d["last_post_ids"] = json.loads(d.get("last_post_ids") or "[]")
                except: d["last_post_ids"] = []
                out.append(d)
    return out


async def scraper_get_profile(url: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM scraper_profiles WHERE url = ?",
            (_normalise_url(url),),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        d = dict(row)
        try:    d["last_post_ids"] = json.loads(d.get("last_post_ids") or "[]")
        except: d["last_post_ids"] = []
        return d


async def scraper_set_enabled(url: str, enabled: bool,
                               reset_failures: bool = False) -> bool:
    """Pause / resume polling for one profile."""
    async with aiosqlite.connect(DB_PATH) as db:
        if reset_failures:
            cur = await db.execute("""
                UPDATE scraper_profiles
                SET enabled = ?, failure_count = 0, last_error = NULL
                WHERE url = ?
            """, (1 if enabled else 0, _normalise_url(url)))
        else:
            cur = await db.execute("""
                UPDATE scraper_profiles SET enabled = ? WHERE url = ?
            """, (1 if enabled else 0, _normalise_url(url)))
        await db.commit()
        return (cur.rowcount or 0) > 0


async def scraper_due_profiles(now_iso: str, platform: str | None = None,
                                limit: int = 20) -> list[dict]:
    """Return enabled profiles whose `next_probe_at` is in the past (or null).
    Used by the burst-session scheduler to pick a session's batch."""
    out: list[dict] = []
    q = """
        SELECT * FROM scraper_profiles
        WHERE enabled = 1
          AND (next_probe_at IS NULL OR next_probe_at <= ?)
    """
    args: tuple = (now_iso,)
    if platform:
        q += " AND platform = ?"
        args = args + (platform,)
    q += " ORDER BY (next_probe_at IS NULL) DESC, next_probe_at ASC LIMIT ?"
    args = args + (int(limit),)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(q, args) as cur:
            async for row in cur:
                d = dict(row)
                try:    d["last_post_ids"] = json.loads(d.get("last_post_ids") or "[]")
                except: d["last_post_ids"] = []
                out.append(d)
    return out


async def scraper_update_probe_result(
    url: str,
    *,
    last_post_ids: list[str] | None = None,
    next_probe_at: str,
    http_code: int | None = None,
    error: str | None = None,
    failure_reset: bool = False,
    failure_increment: bool = False,
    new_downloads: int = 0,
) -> None:
    """Persist the outcome of a single probe. last_post_ids is overwritten
    (caller is responsible for merging if they want history beyond the last N)."""
    sets: list[str] = ["last_check_at = ?", "next_probe_at = ?",
                       "last_http_code = ?", "last_error = ?"]
    now = datetime.now(timezone.utc).isoformat()
    args: list = [now, next_probe_at, http_code, error]
    if last_post_ids is not None:
        sets.append("last_post_ids = ?")
        args.append(json.dumps(list(last_post_ids)))
    if failure_reset:
        sets.append("failure_count = 0")
    elif failure_increment:
        sets.append("failure_count = failure_count + 1")
    if new_downloads:
        sets.append("downloaded_count = downloaded_count + ?")
        args.append(int(new_downloads))
    args.append(_normalise_url(url))
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"UPDATE scraper_profiles SET {', '.join(sets)} WHERE url = ?",
            args,
        )
        await db.commit()


# ── scraper_cookies state ────────────────────────────────────────────────────


async def cookie_ensure(cookie_key: str, user_agent: str) -> dict:
    """Idempotently create a cookie state row. Sets the warmup anchor +
    pinned UA on first sight. Returns the persisted row."""
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("""
            INSERT INTO scraper_cookies (cookie_key, first_seen_at, user_agent)
            VALUES (?, ?, ?)
            ON CONFLICT(cookie_key) DO NOTHING
        """, (cookie_key, now, user_agent))
        await db.commit()
        async with db.execute(
            "SELECT * FROM scraper_cookies WHERE cookie_key = ?", (cookie_key,)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else {}


async def cookie_get(cookie_key: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM scraper_cookies WHERE cookie_key = ?", (cookie_key,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def cookie_record_probe(cookie_key: str, today: str) -> int:
    """Bump probes_today for a cookie. Resets when probes_today_date doesn't
    match the supplied YYYY-MM-DD. Returns the new count."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT probes_today, probes_today_date FROM scraper_cookies "
            "WHERE cookie_key = ?", (cookie_key,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return 0
        if (row["probes_today_date"] or "") != today:
            new_count = 1
            await db.execute("""
                UPDATE scraper_cookies
                SET probes_today = 1, probes_today_date = ?
                WHERE cookie_key = ?
            """, (today, cookie_key))
        else:
            new_count = int(row["probes_today"] or 0) + 1
            await db.execute("""
                UPDATE scraper_cookies
                SET probes_today = ?
                WHERE cookie_key = ?
            """, (new_count, cookie_key))
        await db.commit()
        return new_count


async def cookie_mark_block(cookie_key: str, cooldown_iso: str) -> None:
    """Record a 401/403/429 hit on this cookie + extend cooldown."""
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE scraper_cookies SET
                last_block_at      = ?,
                cooldown_until     = ?,
                consecutive_blocks = consecutive_blocks + 1
            WHERE cookie_key = ?
        """, (now, cooldown_iso, cookie_key))
        await db.commit()


async def cookie_mark_success(cookie_key: str) -> None:
    """A clean 200 — reset the consecutive-block counter."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE scraper_cookies SET consecutive_blocks = 0
            WHERE cookie_key = ?
        """, (cookie_key,))
        await db.commit()


async def cookie_mark_alerted(cookie_key: str) -> None:
    """Record that we sent the owner a cookie-expiry alert (so we don't spam)."""
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE scraper_cookies SET alerted_at = ? WHERE cookie_key = ?",
            (now, cookie_key),
        )
        await db.commit()


async def cookie_clear_alerted(cookie_key: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE scraper_cookies SET alerted_at = NULL WHERE cookie_key = ?",
            (cookie_key,),
        )
        await db.commit()


# ── Sticker maker helpers ────────────────────────────────────────────────────

STICKER_DRAFT_TTL_HOURS = 6


async def sticker_draft_insert(
    user_id: int, telegram_file_id: str, file_path: str,
    mime_type: str | None, duration_s: float | None,
    width: int | None, height: int | None,
) -> int:
    """Insert a new sticker draft. Returns the new row id."""
    now_dt = datetime.now(timezone.utc)
    expires = now_dt + _timedelta(hours=STICKER_DRAFT_TTL_HOURS)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            INSERT INTO sticker_drafts
                (user_id, telegram_file_id, file_path, mime_type, duration_s,
                 width, height, uploaded_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (int(user_id), telegram_file_id, file_path, mime_type, duration_s,
              width, height, now_dt.isoformat(), expires.isoformat()))
        await db.commit()
        return cur.lastrowid or 0


async def sticker_draft_get(draft_id: int, user_id: int) -> dict | None:
    """Fetch a draft by id, scoped to its owner. Returns None if not found
    or owned by someone else (the security boundary)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM sticker_drafts WHERE id = ? AND user_id = ?",
            (int(draft_id), int(user_id)),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None


async def sticker_draft_list(user_id: int) -> list[dict]:
    """List all live drafts for a user (newest first)."""
    out: list[dict] = []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM sticker_drafts WHERE user_id = ? "
            "ORDER BY uploaded_at DESC",
            (int(user_id),),
        ) as cur:
            async for row in cur:
                out.append(dict(row))
    return out


async def sticker_draft_set_status(draft_id: int, status: str,
                                    error: str | None = None) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE sticker_drafts SET status = ?, error = ? WHERE id = ?",
            (status, error, int(draft_id)),
        )
        await db.commit()


async def sticker_draft_delete(draft_id: int, user_id: int) -> str | None:
    """Delete one draft. Returns the file_path so the caller can unlink the
    file. Returns None if no row matched."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT file_path FROM sticker_drafts WHERE id = ? AND user_id = ?",
            (int(draft_id), int(user_id)),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        await db.execute(
            "DELETE FROM sticker_drafts WHERE id = ? AND user_id = ?",
            (int(draft_id), int(user_id)),
        )
        await db.commit()
        return row["file_path"]


async def sticker_drafts_delete_all(user_id: int) -> list[str]:
    """Wipe every draft for a user. Returns the file paths so the caller
    can unlink them."""
    paths: list[str] = []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT file_path FROM sticker_drafts WHERE user_id = ?",
            (int(user_id),),
        ) as cur:
            async for row in cur:
                paths.append(row["file_path"])
        await db.execute(
            "DELETE FROM sticker_drafts WHERE user_id = ?",
            (int(user_id),),
        )
        await db.commit()
    return paths


async def sticker_drafts_expired() -> list[dict]:
    """Return all drafts whose expires_at has passed (status != 'done')."""
    now = datetime.now(timezone.utc).isoformat()
    out: list[dict] = []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM sticker_drafts WHERE expires_at < ? AND status != 'done'",
            (now,),
        ) as cur:
            async for row in cur:
                out.append(dict(row))
    return out


async def sticker_drafts_purge(draft_ids: list[int]) -> None:
    if not draft_ids:
        return
    placeholders = ",".join("?" * len(draft_ids))
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"DELETE FROM sticker_drafts WHERE id IN ({placeholders})",
            tuple(int(i) for i in draft_ids),
        )
        await db.commit()


# ── Sticker packs ────────────────────────────────────────────────────────────


async def sticker_pack_get(user_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM sticker_packs WHERE user_id = ?",
            (int(user_id),),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None


async def sticker_pack_create(user_id: int, pack_name: str,
                               pack_title: str, telegram_url: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO sticker_packs
                (user_id, pack_name, pack_title, telegram_url, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                pack_name    = excluded.pack_name,
                pack_title   = excluded.pack_title,
                telegram_url = excluded.telegram_url
        """, (int(user_id), pack_name, pack_title, telegram_url, now))
        await db.commit()


async def sticker_record(user_id: int, pack_name: str, source_draft_id: int | None,
                          emoji: str, telegram_file_id: str | None,
                          webm_path: str | None) -> int:
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            INSERT INTO stickers
                (user_id, pack_name, source_draft_id, emoji, telegram_file_id,
                 webm_path, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (int(user_id), pack_name, source_draft_id, emoji,
              telegram_file_id, webm_path, now))
        await db.commit()
        return cur.lastrowid or 0


async def sticker_list_for_user(user_id: int) -> list[dict]:
    out: list[dict] = []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM stickers WHERE user_id = ? ORDER BY created_at DESC",
            (int(user_id),),
        ) as cur:
            async for row in cur:
                out.append(dict(row))
    return out


# ── License keys ─────────────────────────────────────────────────────────────


async def license_create(key_id: str, tier: str, secret_hash: str,
                         issued_to: str | None, seats: int,
                         issued_at: str, expires_at: str,
                         note: str | None = None) -> None:
    """Persist a freshly-issued key. key_id is unique; the plaintext secret is
    never stored — only its HMAC (secret_hash)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO license_keys
                (key_id, tier, issued_to, seats, secret_hash, status,
                 issued_at, expires_at, note)
            VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)
        """, (key_id, tier, issued_to, int(seats), secret_hash,
              issued_at, expires_at, note))
        await db.commit()


async def license_get(key_id: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM license_keys WHERE key_id = ?", (key_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def license_list() -> list[dict]:
    """All keys, newest first, each with its current activation count."""
    out: list[dict] = []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT k.*,
                   (SELECT COUNT(*) FROM license_activations a
                     WHERE a.key_id = k.key_id) AS activations
            FROM license_keys k
            ORDER BY k.issued_at DESC
        """) as cur:
            async for row in cur:
                out.append(dict(row))
    return out


async def license_revoke(key_id: str) -> bool:
    """Flip a key to 'revoked'. Idempotent; returns True if a row changed."""
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            UPDATE license_keys SET status = 'revoked', revoked_at = ?
            WHERE key_id = ? AND status != 'revoked'
        """, (now, key_id))
        await db.commit()
        return (cur.rowcount or 0) > 0


async def license_count_activations(key_id: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM license_activations WHERE key_id = ?",
            (key_id,),
        ) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else 0


async def license_activation_exists(key_id: str, device_id: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM license_activations WHERE key_id = ? AND device_id = ? LIMIT 1",
            (key_id, device_id),
        ) as cur:
            return (await cur.fetchone()) is not None


async def license_record_activation(key_id: str, device_id: str,
                                    device_label: str | None = None) -> None:
    """Upsert a (key, device) activation. An existing device just refreshes
    last_seen; a new device claims a seat (caller checks the limit first)."""
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO license_activations
                (key_id, device_id, device_label, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(key_id, device_id) DO UPDATE SET
                last_seen    = excluded.last_seen,
                device_label = COALESCE(excluded.device_label, device_label)
        """, (key_id, device_id, device_label, now, now))
        await db.commit()


async def license_list_activations(key_id: str) -> list[dict]:
    out: list[dict] = []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM license_activations WHERE key_id = ? "
            "ORDER BY last_seen DESC", (key_id,),
        ) as cur:
            async for row in cur:
                out.append(dict(row))
    return out
