# Sentinel Media Downloader — handover

Pointer document for the next session. The session record is in Sentinel Docs;
this file holds only live state and work that remains.

## Closed 2026-09-10 — optional Instagram cookie fallback

- Fixed the live failure `Permission denied: /cookies/instagram.txt` for public
  Instagram media. `_resolve_cookies()` now requires the optional cookie jar to
  open read/write before passing it to yt-dlp; an unreadable or read-only mount
  is omitted and the public request continues logged out.
- Regression tests cover inaccessible, read-only, and readable cookie files.
  `d8d871d` is merged to `main`; deployed branch commit is `00d04d1`.
- E2E evidence: public reel `DZvkSyijaIQ` was received, downloaded, delivered
  by Telegram, and recorded in `download_history` at
  `2026-09-10T13:42:20Z`.

## Outstanding

1. **Instagram image-only posts/carousels remain unsupported without an
   authenticated browser/session.** Direct yt-dlp enumerated the eight items of
   `DdG6iOXjUyn`, but every one yielded `No video formats found`; gallery-dl was
   redirected to Instagram login. Upstream yt-dlp issues #17077 and #16951
   record the same limitation, including with cookies.
2. **Choose a durable image route.** Camoufox is currently disabled
   (`SMDL_IG_CAMOUFOX=0`), consistent with ADR MED-012's superseded live
   resolution. The Planning card is `bfba08bc-847c-4409-82da-173f35589d54`:
   enable/configure the persistent browser route, or maintain a valid
   gallery-dl-authenticated session. `--write-thumbnail` is only a degraded
   fallback and needs resolution validation before adoption.
3. **OneDrive auto-mirror is enabled but has no valid token.** It does not
   affect Telegram delivery, but it logs a warning after successful sends.
4. **Production runs Uvicorn with `--reload`.** A Watchfiles memory-allocation
   error was observed while the service remained healthy. Use a production
   command without the development reloader when this is scheduled.

## References

- `docs/media/adrs/012-instagram-stealth-browser-camoufox.md` in
  `sentinel-docs` — accepted decision and its superseded on-demand resolution.
- `docs/Codex/journals/2026-09-10-codex-smdl-instagram-download-path.md` in
  `sentinel-docs` — evidence and session record.
- Jarl Planning card `bfba08bc-847c-4409-82da-173f35589d54`.
