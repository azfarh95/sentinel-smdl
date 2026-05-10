# SMDL — Sentinel Media Downloader

> Standalone media downloader bot for Telegram. Wraps `yt-dlp` and `gallery-dl`, handles livestream recording, ships files via Telegram or HTTP / Tailscale-only links.

Standalone-deployable. Originally extracted from a larger AI-driven home-lab stack ([Project Sentinel](../docs/architecture/OVERVIEW.md)) so this component is debuggable in isolation. Same code can be re-wrapped as an MCP tool for LLM agent use ("SMDL MCP" sibling).

---

## Features

- **Multi-platform downloads** via `yt-dlp` (1700+ sites): YouTube, Twitch, Kick, TikTok, Instagram, X/Twitter, Facebook, Reddit, Bilibili, Pinterest, …
- **Photo/carousel fallback** via `gallery-dl` for sites where yt-dlp can't extract images
- **Livestream recording** with native HLS downloader (no orphaned ffmpeg processes), per-recording quality cap, throttled heartbeat status
- **Adaptive site support** — any URL with a yt-dlp extractor works; 3 consecutive "no extractor" failures triggers a friendly "site not supported" message
- **`/stop_livestream`** command halts in-progress recordings cleanly
- **Large-file delivery** — auto-fallback when files exceed Telegram's 50 MB bot limit:
  - Tailscale path-2: `http://<host>:8096/m/<file>` (mesh-only, source-IP gated)
  - Public signed-URL path-1: `https://<your-domain>/share/<token>/<file>` (HMAC-signed, 24h expiry)
  - Telethon user-account upload (2 GB cap)
- **SQLite URL cache** — repeat downloads served from cache
- **Cookie support** — per-platform cookie files for sub-only / age-gated content

---

## Quick start

```bash
# Clone
git clone https://github.com/<org>/sentinel-smdl
cd sentinel-smdl

# Configure
cp config/smdl.example.json config/smdl.json
# edit smdl.json: set owner_chat_id, allowed_chat_ids if you want a closed bot

# Set required env vars (or use a .env file)
export SMDL_BOT_TOKEN=<from BotFather>

# Optional: Tailscale + signed-URL features
export SMDL_PUBLIC_BASE_URL=https://media.your-domain.example.com
export SMDL_TAILNET_HOST=your-host.tail-XXXX.ts.net
export SMDL_SHARE_SECRET=$(python -c "import secrets; print(secrets.token_hex(32))")

# Optional: Telethon for >50 MB uploads
export TELETHON_API_ID=<from my.telegram.org>
export TELETHON_API_HASH=<from my.telegram.org>
export TELETHON_SESSION=<see scripts/generate_session.py>

# Build + run
docker build -t smdl .
docker run -d \
  --name smdl \
  -p 127.0.0.1:8096:8096 \
  -v ./config:/config \
  -v ./downloads:/downloads \
  -v ./cookies:/cookies \
  --env-file .env \
  smdl
```

---

## Configuration

`config/smdl.json` — operational settings:

| Key | Default | Purpose |
|---|---|---|
| `owner_chat_id` | `null` | Numeric Telegram chat ID of the bot's owner — files are saved to OneDrive-style paths only when sent by this chat |
| `allowed_chat_ids` | `[]` | If non-empty, bot ignores messages from chats not in this list |
| `default_quality` | `"1080p"` | Default video quality cap |
| `max_concurrent_downloads` | `2` | Semaphore for parallel jobs |
| `delete_after_send` | `false` | If true, deletes file after successful Telegram send (saves disk) |
| `temp_ttl_hours` | `24` | Cleanup interval for `/downloads/temp/` |
| `live_enabled` | `true` | Master switch for livestream recording |
| `live_max_concurrent` | `1` | Per-host cap on simultaneous live recordings |
| `live_heartbeat_seconds` | `300` | How often the bot edits the status message during a live recording |
| `live_min_free_disk_gb` | `10` | Refuse to start live recording if free disk is below this |
| `live_abort_on_session_fail` | `true` | Zero-retry on auth/cookie failures (recommended) |
| `live_platforms` | `["youtube","twitch","kick"]` | Advisory only; any URL with a yt-dlp extractor works |
| `live_max_height` | `720` | Resolution cap for live recordings (0 = unlimited; lower = smaller files) |

Environment variables (set in shell or `.env`):

| Var | Required | Purpose |
|---|---|---|
| `SMDL_BOT_TOKEN` | yes | Telegram bot token from `@BotFather` |
| `DOWNLOADS_DIR` | no | Default `/downloads`. Inside container path. |
| `COOKIES_DIR` | no | Default `/cookies`. Per-platform cookie files (e.g., `youtube.txt`, `twitch.txt`). |
| `CONFIG_FILE` | no | Default `/config/smdl.json` |
| `SMDL_PUBLIC_BASE_URL` | optional | If set, enables signed-URL public sharing. Should be the HTTPS domain that fronts this service via reverse proxy / Cloudflare Tunnel. |
| `SMDL_TAILNET_HOST` | optional | Tailscale MagicDNS hostname for path-2 mesh-only delivery |
| `SMDL_SHARE_SECRET` | optional | HMAC key for signed URLs (64 hex chars). Required if `SMDL_PUBLIC_BASE_URL` is set. |
| `TELETHON_API_ID` / `TELETHON_API_HASH` / `TELETHON_SESSION` | optional | Enables 2 GB Telethon user-account upload fallback for files exceeding the 50 MB bot API limit |

---

## Bot commands

| Command | Effect |
|---|---|
| (paste any URL) | Auto-detect platform, identify (live/video/photo/carousel), download, send |
| `/live_status` | Show current livestream recording (if any) |
| `/stop_livestream` | Halt active livestream cleanly (alias: `/stop_livestream_download`) |

---

## Architecture

```
                 ┌─────────────────┐
   Telegram ────►│   bot.py        │ Message handler
                 │   (PTB + asyncio│ Concurrent updates ON
                 │     concurrent) │
                 └────────┬────────┘
                          │
       ┌──────────────────┼──────────────────┬──────────────────┐
       ▼                  ▼                  ▼                  ▼
┌────────────┐   ┌────────────────┐   ┌──────────────┐   ┌───────────────┐
│interceptor │   │  identify_post │   │  download    │   │live_downloader│
│ regex URL  │──►│  yt-dlp probe  │──►│  yt-dlp/     │   │ HLS native    │
│ detection  │   │  (no fetch)    │   │  gallery-dl  │   │ + retry budget│
└────────────┘   └────────────────┘   └──────┬───────┘   └───────┬───────┘
                                             │                    │
                                             ▼                    ▼
                                     ┌──────────────────────────────────┐
                                     │  Delivery decision (size-based)  │
                                     │   < 50 MB    → bot inline send   │
                                     │  50 MB-2 GB  → Telethon (if cfg) │
                                     │   > 50 MB    → tailnet + signed  │
                                     └──────────────────────────────────┘
```

`file_serve.py` exposes `GET /m/<file>` (tailnet-only via source-IP gate) and `GET /share/<token>/<file>` (HMAC-signed public).

---

## Why standalone (not just an MCP)

Sibling project: SMDL MCP — same engine wrapped as a Model Context Protocol server, used by an LLM agent to download media on user request.

Standalone exists because:
- Debugging yt-dlp behavior through an MCP server adds 3-4 layers of indirection
- The bot is useful on its own — Telegram-only users don't need the AI part
- Carved-out repo can deploy on a VPS without dragging the rest of the AI stack
- Standalone-first development surfaces bugs the MCP layer would mask (we caught a `concurrent_updates=False` deadlock and a yt-dlp ffmpeg-orphan issue this way)

When a feature stabilises in standalone, it's promoted to the MCP version. See [`feedback_smdl_dev_workflow.md`](https://example.com/) (in the parent stack's memory layer).

---

## License

TBD — leaning MIT for community-friendly distribution. Do not redistribute the maintainer's `smdl.json` (contains personal chat IDs).

---

## Status

Active development. Carve-out candidate from the [Project Sentinel](../docs/architecture/OVERVIEW.md) monorepo.

### Roadmap

| Stage | Scope | State |
|---|---|---|
| **V1 — Discovery + Docker** | Telegram bot + yt-dlp/gallery-dl + livestream recording + dual-path delivery + RecorderBridge stopgap | 🟢 Active — debugging the long tail of platform-specific edge cases (Twitch ffmpeg force, etc.) |
| **V2 — UX + mini-app** | Dedicated SMDL mini-app (TOTP-gated web UI for managing recordings, viewing history, downloading files); UX trimming on the Telegram side | ⚪ Scoped, not started |
| **V3 — PyInstaller native binary** | `smdl-windows-x64.exe` / `smdl-linux-x64` / `smdl-macos-arm64` published in GitHub Releases. No Docker required. | ⚪ |
| **V4 — MSI packaging** | Inno Setup wrapper around V3 binary: Windows wizard, service registration, Start Menu entry, uninstaller | ⚪ |

V1 must stabilise (1-2 months without major regressions) before V2 starts. Each stage is independently shippable; users can pick whichever level of polish suits them.

Pending for V1 carve-out: own LICENSE, example `smdl.example.json`, fresh-VPS install test, GHCR-published image (`ghcr.io/azfarh95/smdl:tag`).
