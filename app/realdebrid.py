"""Real-Debrid REST client.

Turns torrent magnet URIs into direct HTTPS URLs that any HTML5 video
element (or aria2c / yt-dlp / wget) can stream and download from.

Auth model (v1):
  - Personal API token from https://real-debrid.com/apitoken, pasted by
    the owner into .env.local as RD_API_TOKEN. Single-user, simplest path.
  - Full OAuth device-code flow is on the roadmap (P6 polish) for when we
    want refresh-token rotation. For now the personal token is permanent
    until revoked, which fits the single-owner Sentinel Suite usage.

API doc: https://api.real-debrid.com/

The standard add-magnet → poll → unrestrict flow:
  1. POST /torrents/addMagnet            → {id, uri}
  2. GET  /torrents/info/{id}            → status: 'magnet_conversion' → 'waiting_files_selection' → 'downloaded'
  3. POST /torrents/selectFiles/{id}     → tell RD which files to fetch (usually 'all')
  4. POLL /torrents/info/{id}            → wait for status='downloaded' (RD's cache hit is ~1s; uncached can be 30s-5min)
  5. The info response contains `links: [...]` — RD-hosted URLs (one per file)
  6. POST /unrestrict/link  for each    → returns final direct HTTPS URLs

The whole flow is wrapped in `magnet_to_direct_urls(magnet)` below.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


_API_BASE = "https://api.real-debrid.com/rest/1.0"
_DEFAULT_TIMEOUT = 30                # per-request HTTP timeout
_POLL_INTERVAL = 1.5                 # seconds between info polls
_DEFAULT_RESOLVE_TIMEOUT = 180       # cap on the full magnet→URL flow

# Owner-editable token file. Bind-mounted (./smdl/config:/config) so a token
# saved from the Settings UI survives image rebuilds without an env change.
_TOKEN_FILE = os.environ.get("RD_TOKEN_FILE", "/config/rd_token")


class RealDebridError(Exception):
    """Raised when the RD API rejects a request or returns malformed data.

    Carries the HTTP status and RD's own `error_code` when the failure
    came from an API error body, so callers can branch on specific
    conditions (e.g. an infringing-file takedown) instead of string-matching."""

    def __init__(self, message: str, *, http_status: int | None = None,
                 error_code: int | None = None):
        super().__init__(message)
        self.http_status = http_status
        self.error_code = error_code

    @property
    def is_infringing(self) -> bool:
        """True when RD refused a file for legal/copyright reasons. RD signals
        this as HTTP 451 (Unavailable For Legal Reasons) and/or error_code 35
        (`infringing_file`). Permanent for that file — never worth retrying."""
        return self.http_status == 451 or self.error_code == 35


def _get_token() -> Optional[str]:
    """Read the API token. Env var first, then a /config file as a fallback
    (so the container can be reconfigured without an image rebuild)."""
    t = os.environ.get("RD_API_TOKEN", "").strip()
    if t:
        return t
    # Fallback: token file (single-line), owner-editable from the Settings UI.
    try:
        with open(_TOKEN_FILE, "r", encoding="utf-8") as f:
            return f.read().strip() or None
    except (FileNotFoundError, PermissionError):
        return None


def set_token(token: str) -> None:
    """Persist the owner's RD personal token to the token file. Atomic
    write so a crash mid-save can't leave a truncated token behind."""
    token = (token or "").strip()
    if not token:
        raise RealDebridError("Empty token.")
    d = os.path.dirname(_TOKEN_FILE) or "."
    os.makedirs(d, exist_ok=True)
    tmp = f"{_TOKEN_FILE}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(token + "\n")
    os.replace(tmp, _TOKEN_FILE)


def token_status() -> dict:
    """Whether a token is configured + a masked tail for display. Never
    returns the raw token. `source` tells the owner where it's coming
    from (an env var overrides the editable file)."""
    env_t = os.environ.get("RD_API_TOKEN", "").strip()
    tok = _get_token()
    source = "env" if env_t else ("file" if tok else None)
    masked = f"…{tok[-4:]}" if tok and len(tok) >= 4 else ("set" if tok else None)
    return {"set": bool(tok), "masked": masked, "source": source,
            "editable": not env_t}


# ── HTTP helpers ────────────────────────────────────────────────────────────
def _request(method: str, path: str, *, data: dict | None = None,
              timeout: int = _DEFAULT_TIMEOUT,
              token: Optional[str] = None) -> dict | list:
    """Single-endpoint HTTP wrapper. Raises RealDebridError on non-2xx OR
    when the body isn't JSON. Returns parsed JSON (dict or list)."""
    tok = token or _get_token()
    if not tok:
        raise RealDebridError(
            "No RD_API_TOKEN set. Get a personal token from "
            "https://real-debrid.com/apitoken and add to .env.local."
        )
    url = f"{_API_BASE}{path}"
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(
        url, data=body, method=method,
        headers={
            "Authorization": f"Bearer {tok}",
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded" if data else "application/json",
            "User-Agent": "SMDL/RD-Client",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        # RD returns JSON error bodies like {"error": "...", "error_code": N}
        try:
            err_body = json.loads(e.read().decode())
            msg = err_body.get("error") or str(e)
            code = err_body.get("error_code")
            raise RealDebridError(
                f"HTTP {e.code} — {msg} (code {code})",
                http_status=e.code, error_code=code,
            ) from e
        except json.JSONDecodeError:
            raise RealDebridError(
                f"HTTP {e.code} — {e.reason}", http_status=e.code,
            ) from e
    except urllib.error.URLError as e:
        raise RealDebridError(f"Network error: {e}") from e

    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as e:
        raise RealDebridError(f"Bad JSON response from RD: {e}")


# ── Token sanity / account ──────────────────────────────────────────────────
@dataclass
class RDAccount:
    username: str
    email: str
    type: str                       # 'premium' | 'free' (RD's own classification)
    premium_seconds_left: int       # seconds remaining on the current premium term
    expiration_iso: Optional[str]   # absolute expiry date, ISO-8601 (from RD's /user.expiration)
    points: int                     # fidelity points

    @property
    def is_premium(self) -> bool:
        # RD's `type` field is the authoritative signal. We belt-and-brace with
        # the seconds-remaining check in case `type` is ever missing.
        return self.type == "premium" or self.premium_seconds_left > 0


def get_account() -> RDAccount:
    """Validate the token by fetching account state. Useful as a health
    check on container boot and from the Mini App settings page.

    RD's /user response shape (relevant fields):
      - username : str
      - email    : str
      - type     : 'premium' | 'free'
      - premium  : int — REMAINING premium time IN SECONDS (NOT an epoch — easy to misread)
      - expiration : str — absolute expiry as ISO date
      - points   : int — fidelity points
    """
    j = _request("GET", "/user")
    if not isinstance(j, dict):
        raise RealDebridError("Unexpected /user response shape")
    return RDAccount(
        username=j.get("username", ""),
        email=j.get("email", ""),
        type=(j.get("type") or "").strip().lower(),
        premium_seconds_left=int(j.get("premium") or 0),
        expiration_iso=j.get("expiration"),
        points=int(j.get("points", 0) or 0),
    )


# ── Magnet resolution ───────────────────────────────────────────────────────
@dataclass
class RDDirectFile:
    """One playable/downloadable file after RD has resolved the magnet."""
    filename: str
    filesize: int
    direct_url: str       # the unrestricted https URL — feed to <video>/aria2
    mime_type: Optional[str]


def add_magnet(magnet: str) -> str:
    """Submit a magnet to RD. Returns the torrent id used for subsequent
    polls. RD caches popular torrents so this often resolves instantly."""
    if not magnet.startswith("magnet:"):
        raise RealDebridError("Not a magnet URI")
    j = _request("POST", "/torrents/addMagnet", data={"magnet": magnet})
    if not isinstance(j, dict) or not j.get("id"):
        raise RealDebridError(f"addMagnet returned unexpected: {j}")
    return j["id"]


def torrent_info(torrent_id: str) -> dict:
    """Current state of one RD torrent job — status, progress, file list,
    intermediate RD links."""
    j = _request("GET", f"/torrents/info/{torrent_id}")
    if not isinstance(j, dict):
        raise RealDebridError(f"info returned non-dict: {j}")
    return j


def select_files(torrent_id: str, file_ids: str = "all") -> None:
    """After addMagnet, RD requires us to declare which files we want from
    the torrent. 'all' is the simplest; for big season packs the caller can
    pass a comma-separated subset of file ids from torrent_info()['files']."""
    _request("POST", f"/torrents/selectFiles/{torrent_id}",
              data={"files": file_ids})


def unrestrict_link(rd_link: str) -> dict:
    """Turn an RD-hosted link (from torrent_info['links'][i]) into the
    final HTTPS URL that can be streamed/downloaded. Returns the full
    {download, filename, filesize, mimeType, ...} dict from RD."""
    j = _request("POST", "/unrestrict/link", data={"link": rd_link})
    if not isinstance(j, dict) or not j.get("download"):
        raise RealDebridError(f"unrestrict returned unexpected: {j}")
    return j


def magnet_to_direct_urls(magnet: str, *,
                            timeout: int = _DEFAULT_RESOLVE_TIMEOUT,
                            min_size_bytes: int = 50 * 1024 * 1024,
                            ) -> list[RDDirectFile]:
    """End-to-end: magnet URI → list of direct HTTPS URLs.

    Common case (RD has the torrent cached): returns in 2-5s.
    Uncached case: RD downloads to its own cache first, can take 30s-5min;
    we poll until status='downloaded' or `timeout` elapses.

    `min_size_bytes` filters out the .nfo / .srt / sample.mkv noise common
    in torrent releases. Default 50 MB keeps full-length video, drops fluff.
    """
    tid = add_magnet(magnet)
    logger.info("RD: magnet added → torrent_id=%s", tid)

    deadline = time.time() + timeout
    selected = False

    while True:
        info = torrent_info(tid)
        status = info.get("status", "")
        progress = info.get("progress", 0)
        logger.debug("RD torrent_id=%s status=%s progress=%s",
                      tid, status, progress)

        # Select files once RD enumerated them
        if not selected and status == "waiting_files_selection":
            select_files(tid, "all")
            selected = True
            time.sleep(0.5)
            continue

        if status == "downloaded":
            break
        if status in {"error", "magnet_error", "virus", "dead"}:
            raise RealDebridError(f"RD job failed: status={status}")

        if time.time() > deadline:
            raise RealDebridError(
                f"RD job didn't finish in {timeout}s "
                f"(status={status}, progress={progress})"
            )

        time.sleep(_POLL_INTERVAL)

    # Pair the torrent's file list with the RD links
    files = info.get("files") or []
    links = info.get("links") or []
    # RD returns only the selected files in links, in the same order as
    # files filtered by `selected==1`. Sanity-check.
    selected_files = [f for f in files if f.get("selected") == 1]
    out: list[RDDirectFile] = []
    infringing_hit = False
    for f, rd_link in zip(selected_files, links):
        if (f.get("bytes") or 0) < min_size_bytes:
            continue
        try:
            unr = unrestrict_link(rd_link)
        except RealDebridError as e:
            # A per-file takedown shouldn't be silently treated as "too small".
            # Remember it so we can raise an infringing-flavoured error below
            # if it turns out no file was playable.
            if e.is_infringing:
                infringing_hit = True
            logger.warning("RD unrestrict failed for %s: %s", f.get("path"), e)
            continue
        out.append(RDDirectFile(
            filename=unr.get("filename") or os.path.basename(f.get("path", "")),
            filesize=int(unr.get("filesize") or f.get("bytes") or 0),
            direct_url=unr["download"],
            mime_type=unr.get("mimeType"),
        ))
    if not out:
        if infringing_hit:
            raise RealDebridError(
                "Real-Debrid blocked this release (copyright takedown)",
                http_status=451, error_code=35,
            )
        raise RealDebridError(
            "No playable files emerged from RD (all under size threshold "
            f"of {min_size_bytes} bytes)"
        )
    return out
