# Sentinel Media Downloader — handover

Pointer document for the next session. The session record is in Sentinel Docs;
this file holds only live state and work that remains.

## 2026-10-16 - the deploy line is `main` again

- PR #26 rebased the long-lived `feat/ig-camoufox-stealth` branch onto `main` (12 commits became 7; conflicts in the lane files and `iptv_youtube.py` kept `main`'s newer version) and merged it as `f998db56`. The live checkout `C:\Users\azfar\sentinel-smdl` is on `main`, and the container `smdl` was restarted onto it (healthy, `/health/bot` ready).
- The container bind-mounts `sentinel-smdl\app`, `static` and `data` from the live checkout. A code change goes live with `docker restart smdl`; check `GET :8096/health/bot` returns `{"ready":true}`. The image only changes with a rebuild from `metamcp-local`.
- Old tip saved as remote branch `backup/ig-camoufox-pre-rebase-20261016` (and the local `feat/ig-camoufox-stealth`). Delete both once nothing refers to them.
- The stale `sentinel-smdl-wt-cookie-fallback` worktree (it held `main` and blocked the switch) was removed; it was clean and fully on origin.

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
