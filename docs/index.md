# Sentinel SMDL — dev docs

`@azsmdl_bot` — Telegram bot wrapping yt-dlp + gallery-dl + Telethon
+ tailnet HTTP delivery + HMAC-signed share URLs. 1700+ sites, photo
carousels, livestream recording, sticker maker Mini App.

Aggregated into the central docs site. See the
[infra pillar](https://docs.az-sentinel.xyz/pillars/infra/#smdl--standalone-media-bot)
for why this lives under `infra` (not `ai`). This page is the
**dev-facing companion** to the [repo README](https://github.com/azfarh95/sentinel-smdl).

## Status — 2026-05-20 sticker maker shipped

- v1 — yt-dlp + Telegram delivery (2026-04 → 2026-05)
- v2 — per-user scoping + admin tab + OneDrive + /start handshake (2026-05-16)
- **Sticker maker Mini App** — video/GIF → MiniApp editor → personal pack (2026-05-20)

## Repo layout

```
sentinel-smdl/
├─ app/
│  ├─ bot.py             # python-telegram-bot v21 application
│  ├─ recorder/          # yt-dlp + gallery-dl + HLS livestream backends
│  ├─ delivery/          # Telegram Bot API / Telethon / tailnet HTTP / HMAC share
│  ├─ miniapp/           # WebView sticker-maker UI (Svelte + FFmpeg.wasm)
│  ├─ db.py              # SQLite — cache, watchlist, prefs
│  └─ config.py          # Per-user prefs + env loader
├─ cookies/              # Per-platform yt-dlp cookies (mounted volume)
├─ tests/                # pytest
├─ Dockerfile
├─ docker-compose.yml
└─ docs/                 # → aggregated into docs.az-sentinel.xyz
```

## Delivery decision tree

```
File size ──┐
            ├─ < 50 MB         → Telegram Bot API (inline)
            ├─ 50 MB – 2 GB    → Telethon user-account upload (if SMDL_USER_SESSION configured)
            └─ > 2 GB          → tailnet HTTP link + HMAC-signed public share URL
                                  • tailnet:  http://<host>:8096/m/<file>  (mesh-only, source-IP gated)
                                  • public:   https://<host>/share/<token>  (24h expiry, HS256-signed)
```

## Sticker maker Mini App

The headline feature. Video/GIF → cropped sticker → personal Telegram
sticker pack. Lives at the bot's Mini App URL, served from `/miniapp/`
on the bot's tailnet host.

**5 post-build fixes captured in memory worth remembering:**

1. **WebView cookie + preview** — WebViews don't share auth cookies with
   the parent browser. For auth-gated previews, fetch via the bot's
   cookie context then expose as blob URL to the WebView.
2. **Crop UI removed** — initial UX had explicit crop bounds; replaced
   with crop-to-fill VP9 (auto-center, auto-aspect to 512×512). Saved a
   whole UI iteration.
3. **Crop-to-fill VP9** — Telegram stickers require VP9 in WebM, square
   aspect, ≤ 256 KB. FFmpeg.wasm in the WebView does the encode.
4. **No-lock re-make** — earlier version locked the user out of the
   sticker maker while encoding. Now the bot remembers state and lets
   the user re-make the same sticker without re-uploading.
5. **`openTelegramLink`** — the "Add to pack" CTA opens `t.me/addstickers/...`
   which has to go through `WebApp.openTelegramLink()`, not
   `window.open()`. Plain `window.open` silently no-ops inside the WebView.

## Reusable Mini App patterns

These bit us first on SMDL and apply to any future Mini App:

- WebView cookie isolation (fetch + blob URL for auth-gated media)
- `t.me/*` needs `WebApp.openTelegramLink()`
- Aggressive HTML cache — bump `cache_version` on every UI change
- `_verify()` accepts cookie OR initData (dual-path auth)
- CF Access strips `#tgWebAppData=...` URL fragment — fallback to a
  server-side session endpoint trusting the CF Access cookie
- Server-side first paint — emit `<option selected>` directly; never
  depend on JS to apply initial state

See [`reference/miniapps`](https://docs.az-sentinel.xyz/reference/miniapps/)
for the cross-Mini-App catalog.

## Environment

```bash
SMDL_BOT_TOKEN=...                 # Required — invalid token = bot starts in degraded mode
SMDL_USER_SESSION=...              # Optional — Telethon string session for > 50 MB uploads
SMDL_TAILNET_HOST=smdl.tailnet.tld # For > 2 GB tailnet links
SMDL_PUBLIC_HOST=https://smdl.az-sentinel.xyz # For HMAC-signed share URLs
SMDL_HMAC_SECRET=...               # Share-URL signing
SMDL_DB_PATH=/data/smdl.db         # SQLite
SMDL_COOKIES_DIR=/cookies          # Per-platform yt-dlp cookies
SMDL_ADMIN_USERS=12345,67890       # Telegram user IDs with admin tab
```

## Bot commands

| Command | Purpose |
|---|---|
| `/start` | Initial handshake + onboarding |
| `<paste URL>` | Download (auto-detects yt-dlp vs gallery-dl) |
| `/sticker <video>` | Open sticker maker Mini App |
| `/watch <url>` | Subscribe to a streamer; bot DMs when live |
| `/cache_clear` | Wipe URL cache |
| `/prefs` | Per-user preferences (language, quality, etc.) |
| `/admin` | Admin tab (Telegram user IDs in `SMDL_ADMIN_USERS`) |

## Dev workflow

```bash
docker compose up                  # bot + nginx for tailnet HTTP
# or for hot-reload during development:
python -m app.bot                  # against your dev bot token
```

## Related

- [Infra pillar — SMDL section](https://docs.az-sentinel.xyz/pillars/infra/#smdl--standalone-media-bot)
- [Mini App catalog](https://docs.az-sentinel.xyz/reference/miniapps/)
- Memory entry: Telegram Mini App patterns (reusable across any future Mini App)
