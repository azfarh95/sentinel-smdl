# SMDL Roadmap

**Sentinel Media Downloader Lab** — the media pillar of the Sentinel
Suite. Started life as a yt-dlp Telegram bot; growing into a full
media-management platform (downloader → IPTV → apps distribution →
wireless display → smart home).

This document is **the single source of truth for what SMDL is, what
it has, and what it will be**. It supersedes scattered status comments
in the codebase. Updated as each version ships.

---

## Versioning convention

`vMAJOR.MINOR.PATCH` — loosely semver:

- **MAJOR** — a new pillar / a fundamentally new capability category.
  v1 = downloader, v2 = stream tracker, v3 = IPTV, v4 = apps store, v5
  = projector, v9 = monetization/licensing. Existing pillars keep working;
  new MAJORs ride alongside.
- **MINOR** — a phase or sub-feature within a pillar. v3.0 = initial
  IPTV browser, v3.1 = EPG, v3.2 = recording, v3.3 = aggregator
  refactor, etc.
- **PATCH** — bugfixes, polish, no new capability. Tagged but rarely
  documented at this level in the roadmap.

A "version" in this file is a **conceptual milestone**, not a git
tag. The git tags align with PATCH-level releases when we actually
cut them.

---

## Past — what shipped (backfilled)

### v1.0 · Foundation (2024 → mid-2025)
Telegram bot wrapping yt-dlp + a few hundred extractor sites.

- Single-message `/dl <url>` → file delivered back
- URL cache (idempotent re-downloads)
- yt-dlp + gallery-dl side-by-side
- Owner-only auth via Telegram chat-id allowlist
- `python-telegram-bot` long-poll architecture
- Background `start_cleanup_loop` to age out cached files

### v1.5 · Watchlist + live recording (mid-2025)
- `/watchlist add <url>` for streamer monitoring
- `stream_monitor` background task — Twitch / YouTube Live / Kick
- Live recording via ffmpeg (`live_downloader.record_live`)
- Notification on "went live" via Telegram message
- Stripchat / Chaturbate / other adult-platform support gated behind
  `auth.is_platform_blocked` (default-blocked, owner-toggleable)

### v1.6 · Torrent / Real-Debrid fallback + progressive streaming (late May 2026)
- Download fallback chain: Real-Debrid (RD-451) → qBittorrent-behind-PIA → cache
- Default-on; **progressive streaming** (watch-while-downloading)
- qB auth via subnet whitelist (volume conf, not git); SMDL joins
  `metamcp-network` for container DNS
- Commit `a3247a7`

### v2.0 · Mini App + multi-user scaffolding (early 2026)
- Telegram WebApp Mini App at `/app`
- Telegram initData validation (HMAC against bot token)
- Allowed-user check (owner + pending + active states)
- `/app/admin` tab for site blocklist editing
- OneDrive integration for cloud copy of downloaded files

### v2.5 · Sticker Maker (May 2026)
- Send video/GIF to `@azsmdl_bot` → Mini App editor at `/stickers`
- Editor: trim, crop, choose emoji, encode as WebM sticker
- Auto-create + manage personal Telegram sticker pack
- `sticker_routes.py` + `sticker_processor.py` (ffmpeg encoder)
- Background TTL cleanup of expired drafts

### v2.6 · Sticker Studio + transparent video + pack model + cutout (early June 2026)
Major expansion of v2.5 — the sticker maker went from "trim/crop/emoji" to a
real studio with transparent video, multi-pack management, and one-tap import.

- **Sticker Studio** (Fabric.js compositor on `/stickers/{id}/edit`) — text /
  emoji / image overlay layers, in-canvas background cutout, server-side
  **die-cut white outline**, undo/redo. Tiers 1 + 2.
- **Shaped video stickers** — circle / heart / star / diamond / freehand on a
  blurred or solid fill (opaque), WYSIWYG shape preview on the crop overlay.
- **True transparent video** — `app/webm_alpha.py`, a dependency-free Matroska
  muxer that pairs vpxenc colour + alpha VP9 IVF into one track (AlphaMode +
  per-frame BlockAdditional). ffmpeg's libvpx can't write WebM alpha; this does.
  `vpx-tools` baked into the image. Telegram-verified.
- **Per-frame video cutout** — background removal that tracks a moving subject →
  animated transparent sticker. CPU `rembg`/`u2netp` with frame-subsampling
  (segment every 2nd frame @24fps, interpolate + feather), session pre-warmed at
  startup. GPU/DirectML evaluated and rejected — see
  [ADR MED-003](https://docs.az-sentinel.xyz/adrs/media/003-video-cutout-cpu-matting/).
- **Editor UI revamp** — Simple / Standard / Pro skins that gate *complexity*
  (not just looks): sticky preview + bottom tool tabs, Pro left-rail, Clip/Crop/
  Shape/Emoji + Studio tools.
- **Unified pack model** — a "pack" is just your stickers, **video + static
  mixed** (Telegram allows it; the kind tabs were dropped). Multiple **named
  packs** with an active one where new stickers land; front-page (Mini App
  `/app?tab=stickers`) revamped into Stickers / Add / Pack sections + pack picker.
- **Send-a-sticker import** — send any sticker to the bot → clone it into your
  pack; per-user `single` / `all` preference; a deep-link button imports the
  **whole** source set into a new pack named after it
  (`/api/sticker_pack/import_set`, `/api/sticker_import_pref`).
- Commits `395cc1c` (Studio T2), `2c72cfc`/`b251a7d` (alpha video), `61225bd`
  (skins), `b70f118` (pack picker), `d37e394` (import), `cff9b05` (video cutout),
  `fdd3580` (cutout-static fix). Branch `feat/trakt-sync`.

### v3.0 · IPTV browser (early May 2026 — late May 2026)
- New `/iptv` Mini App page — Netflix-style channel grid
- Initial sources: `iptv-org/iptv` global catalogue
- Channel cards with logo, country flag, category chips
- HLS stream playback via `tg.openLink` → VLC handoff
- DASH stream playback via `dash.js` lazy-load
- HLS inline fallback via `hls.js`
- Geo-block detection + warning banner
- Per-channel "Probe stream health" + recording stub

### v3.1 · IPTV — multi-source catalogue (mid-May 2026)
- Free-TV / IPTV community catalogue (~1,894 channels)
- `i.mjh.nz` FAST aggregator (split into mjh-radio / mjh-au / mjh-nz /
  mjh-sky-fast / mjh-other sub-sources)
- China-maintained sources: `fanmingming/live` + `YueChan/Live`
- `openiptvitaly` (Italian curated)
- Per-country iptv-org slices (SG/MY/ID quick-refresh buttons)
- YouTube Live source (~31 24/7 news streams, yt-dlp-resolved on play)

### v3.2 · IPTV — EPG + recording + reliability (mid-May 2026)
- XMLTV ingestion (mjh + epgshare01 SG/MY/ID)
- Now/Next programme display on play page
- ffmpeg-based recording (`/api/iptv/channels/<id>/record`)
- Probe-all background sweep with smart 6h fresh-skip
- Per-channel reliability badges (probe_count / alive_count)
- Auto-probe loop ticks every 12 h

### v3.3 · IPTV aggregator v2 — Stremio-style logical channels (late May 2026)
- New `logical_channels` table + `channel_id` FK on existing rows
- Dedup pipeline (tvg-family + slug+country bucketing + curated YAML)
- `GET /api/iptv/v2/channels` + `/play` + `/sources` endpoints
- Source-picker dropdown on play page
- Client-side auto-failover (hls.js / dash.js error → next alternate)
- Favorites migration from source-prefixed → logical channel IDs
- 56 curated channels with proper source pinning
- Spec: `metamcp-local/docs/iptv-aggregator-v2.md`

### v3.4 · IPTV curation tools + recordings page (late May 2026)
- `/iptv/recordings` first-class management page
- "★ Curate this channel" in-app YAML editor (owner-only)
- Bind-mount `data/` so YAML mutations persist to host repo
- Per-source filter restored on v2 channel listing

### v3.5 · IPTV in-app playback + Family TV (late May 2026)
- **Server-side HLS relay** so YouTube-live plays inline in-app (no VLC
  handoff) — `_hls_relay` in `iptv_routes.py`
- HLS relay 404 fix for logical channel ids
- **Family TV** — parent-friendly MY/SG/ID channel picker
- Expanded + fixed Malaysia YouTube-live channel list
- Gentler auto-probe so health sweeps don't starve live playback
- Commits `8bfa21e`, `e55e08c`, `0cd312d`, `3d0a11a`, `d26a8f7`

### v4.0 · Apps distribution + Auth-perms v2 Phase 1 (late May 2026)
- Sentinel Suite "📦 Apps" tile → self-hosted Play Store for sideload
  APKs (replaces TG-bot delivery)
- WebView APK wrapper (`sentinel-smdl-android/`) — Kotlin + Tauri-style
- Committed-in-repo debug keystore (stable cert across rebuilds)
- Android intent dispatcher with MIME-typed VLC/MX-Player preference
- Per-app `auth_v2` cookie format (v1 owner / v2 scoped beta users)
- `scopes.yaml` catalogue (16 initial scopes across all pillars)
- `require_scope("smdl.iptv")` enforcement on `/iptv/*` routes
- Spec: `metamcp-local/docs/auth-perms-v2.md`

### v9.0 · Editions + license authority — Monetization pillar Phase 1 (late May 2026)
**New pillar: commercialization.** Rides alongside the feature pillars
(v1–v8); this is the "make it shareable / sellable" track.

- **Edition split** — `SENTINEL_MEDIA_EDITION` = `community` (default,
  safe: ships no content/grey plumbing) / `private` (operator's full box)
- **Middleware edition gate** (`app/main.py`) — community 404s the
  legal-boundary surfaces: torrent/RD pipeline, same-origin HLS relay,
  aggregator country slices
- Community YouTube via the **official IFrame Player** + first-run legal
  acknowledgement
- **SMDL-local license-key authority** (`app/licensing.py`) — `SMDL-FAM` /
  `SMDL-COM` keys, HMAC-stored secrets (plaintext shown once), online
  `/api/license/validate` + 7-day offline grace, seat limits
- Owner console at `/app/licenses`; best-effort metadata mirror to the
  central Sentinel License Registry (watchdog `:8200`)
- Commits `c1bdc11`, `be47546`, `3e1f327`, `517cbab`, `eff044c`
- ⚠️ Today the FAM/COM tier is an **unenforced label** — features are
  gated by the deployment `EDITION` flag, not the key. v9.1 wires them.

**Current state of SMDL**: downloader **v1.6** · IPTV **v3.5** ·
stickers **v2.6** · distribution **v4.0** · monetization **v9.0**.

---

## Planned — what's next

### v2.7 · Sticker Studio depth (next focused session)
**Status**: planned · scope agreed 2026-06-05 (anchor order **B → A → C → D**)
**Depends on**: v2.6 (shipped)
**Spec**: [sentinel-docs/planning/smdl-stickers-v27.md](https://docs.az-sentinel.xyz/planning/smdl-stickers-v27/)

Take the sticker maker from "feature-complete editor" to "studio with depth":
- **B · Background job queue + progress** *(foundational)* — `sticker_jobs` table +
  asyncio worker; `/make` enqueues + returns `{job_id}`; Mini App progress poll/SSE
  + Telegram push on done. Removes the 7–20 s blocking encode.
- **A · Animated overlays on video** *(flagship)* — per-frame compositing so text /
  emoji / image / outline layers ride an *animated* sticker (Studio is static-only
  today). Reuses the cutout frame→encode pipeline. Depends on B.
- **C · Matting & encoder tuning** — stronger matte (RMBG/BiRefNet vs u2netp),
  temporal de-flicker, 2-pass VP9, parallel CPU matting.
- **D · Library depth (more DB)** — `stickers` gains tags/format/dims/use_count/
  sort_order/soft-delete; `sticker_presets`; cross-pack search; reorder / re-edit /
  trash+restore.

### v3.6 · Live TV depth — self-healing + actionable + personal + catch-up
**Status**: planned · scope agreed 2026-06-05 (full plan, **B → A → C → D**)
**Depends on**: v3.5 (shipped)
**Spec**: [sentinel-docs/planning/smdl-livetv-v36.md](https://docs.az-sentinel.xyz/planning/smdl-livetv-v36/)

Take Live TV from "big catalogue that mostly plays" to "TV that just works and
knows me":
- **B · Self-healing playback** *(foundational)* — reliability-ranked source pick +
  auto-failover on mid-stream stall via the HLS relay (reuses `v_channels_with_status`
  health + `report_failure`); auto-prune dead sources.
- **A · Actionable EPG** — cross-channel "what's on" search, programme reminders
  (Telegram push), record-this-program / record-every-episode (series-link) from
  EPG (today's `iptv_scheduled` is time-window only).
- **C · Personal TV** — favorites + custom lineups, continue/recently-watched rows,
  For-You rows from `iptv_play_history` (the live analogue of Trakt continue-watch).
- **D · Catch-up / time-shift** *(heaviest)* — pause/rewind/restart-program via
  provider catch-up URLs + a rolling DVR segment-ring buffer for favorites (job
  worker; shares the sticker v2.7-B pattern).

### v4.1 · Recording lifecycle controls (~1 day work)
**Status**: planned
**Depends on**: nothing
**Spec**: TBD (small, no formal doc)

Finish the recording-feature loop:
- `POST /api/iptv/recordings/<id>/cancel` — kill in-flight ffmpeg
- "Delete file" + confirm button on each finished recording card
- "Re-record" button — re-runs the same channel for the same duration

### v4.2 · Auth-perms v2 Phase 2 (~3 hr work)
**Status**: planned
**Depends on**: v4.0 (already shipped)
**Spec**: `metamcp-local/docs/auth-perms-v2.md` §7 + §11

Beta-user admin UI on the Suite:
- SQLite tables: `users`, `invites`, `revocations`, `auth_events`
- `/admin/users` CRUD page (owner-only)
- `/admin/invite` flow — generate one-time redemption URL
- `/auth/redeem?token=…` mints v2 scoped cookie
- Audit log surfaced on `/admin/audit`

Triggers building this: first beta user lined up.

### v4.3 · Auth-perms v2 Phase 3 — cross-pillar rollout (~30 min × N)
**Status**: planned
**Depends on**: v4.2

Decorate remaining pillars with `require_scope`:
- SMDL streamtracker / downloader / stickers / admin
- Sentinel Finance (view + write)
- Sentinel AI (chat + admin)
- Watchdog (view + restart)
- Apps store (install)

Each pillar: copy the `auth_v2.py` snippet, decorate routes, ship.

### v5.0 · SMDL Projector v1 Phase 1 — Sentinel-native WebRTC casting (~1 week)
**Status**: planned (highest-priority new pillar)
**Depends on**: dongle or RPi hardware procured; `sentinel-shared-brain` running
**Spec**: `metamcp-local/docs/smdl-projector-v1.md` §11

Wireless display for SMDL ecosystem content:
- New `sentinel-projector` container/service on dongle or RPi
- WebRTC receiver — peer connection, H.264/AAC decode, HDMI output
  via GStreamer
- Discovery: receivers register with the Suite at startup
- Android sender app — `MediaProjection` → `MediaCodec` → WebRTC
- Browser sender — `getDisplayMedia()` for tab/window capture, page
  at `/cast`
- Signaling via `sentinel-shared-brain` WebSocket bridge
- Auth: `projector.cast` scope (new, added to scopes.yaml)
- Admin UI: `/projector` on the Suite (sessions, trusted devices,
  toggle hotspot — toggle is a no-op until Phase 3)

**What this enables**: cast SMDL IPTV / Finance / AI / a browser tab
from any device to the projector. Doesn't help with Win+K or
MagicOS — those are Phase 2.

### v5.1 · SMDL Projector v1 Phase 2 — Miracast OS-sender compat (~2-3 weeks)
**Status**: planned (high effort, defer until v5.0 proves daily use)
**Depends on**: v5.0
**Spec**: `metamcp-local/docs/smdl-projector-v1.md` §12

Compatibility with stock OS senders:
- mDNS + SSDP advertisement so receivers show up in Win+K menu
- MS-MICE handshake (TCP/7236) — capability exchange + RTSP setup
- RTP/UDP H.264 + AAC packet receive + decode
- Optional PIN pairing (8-digit, displayed on HDMI output)
- Trusted-devices store (no PIN re-prompt after first pair)

**What this enables**: Windows 10/11 Wireless Display, Honor MagicOS
Easy Projection, Samsung Smart View, Xiaomi ScreenCast all cast to
the Sentinel Projector receiver without installing anything on the
sender.

**Honest risk**: Microsoft moves protocol details across Windows
versions; 80-90% reliability ceiling. Fork MiracleCast (C/GPL) to
save weeks vs. clean-room implementation.

### v5.2 · SMDL Projector v1 Phase 3 — Wi-Fi hotspot mode (~1 week)
**Status**: planned (after v5.1 stabilises)
**Depends on**: v5.1 — Miracast SRC clients use Wi-Fi P2P
**Spec**: `metamcp-local/docs/smdl-projector-v1.md` §13

Operate without a router:
- Receiver creates a Wi-Fi P2P group (`wpa_supplicant p2p_group_add`)
- Internal `dnsmasq` for DHCP on the P2P interface
- Senders connect directly via standard Wi-Fi Direct Miracast
- Useful for hotel rooms, conference rooms, demos at relatives' houses

**Caveat**: Wi-Fi regulatory power limits cap range — works
same-room, less reliable across walls.

### v5.3 · SMDL Projector v2 — multi-receiver matrix (~2-3 weeks)
**Status**: future (only build when there are multiple receivers in
the home)
**Depends on**: v5.2; multiple receiver hardware deployed

- Receiver list / picker UI on senders (Stremio-style "pick a
  destination")
- Multi-room audio: cast audio-only to one receiver, video to another
- Multi-display mirroring: one sender → N receivers simultaneously
- Per-receiver display rotation / scaling settings
- Audio output routing (HDMI vs. external sink like Sonos / Bluetooth
  speaker)

### v6.0 · Smart home integration (~2-4 weeks)
**Status**: future (after Projector stabilises)
**Depends on**: v5.x

Generalise the Projector pillar into a "home display + audio bus":
- Home Assistant integration — control Sentinel devices from HA dashboards
- Voice triggers (via existing Sentinel AI agent) — "Sentinel, put
  CNA on the projector"
- Scene automations: "movie mode" dims lights + casts to projector +
  routes audio to soundbar
- Tasmota / Shelly / Tapo plug integration for the projector + AVR
  power-on automation

### v6.1 · Multi-room audio (~2-3 weeks)
**Status**: future

Audio-only stream targets across the house:
- Bluetooth speaker connected to a Pi → Sentinel Audio Sink
- Multi-room sync (latency-aligned playback)
- Audio source = anything Sentinel can play (IPTV channel audio,
  music library, browser tab audio, Spotify Connect via librespot)

### v7.0 · Content library + media server (~3-4 weeks)
**Status**: future ambition (Plex/Jellyfin replacement scope)

If recording lifecycle becomes heavy:
- `/library` page indexing `/downloads/iptv/`, `/downloads/yt/`, etc.
- Metadata enrichment (TMDB lookup for movies, EPG cache for IPTV
  recordings)
- Native player via the existing Projector pipeline (cast library
  item directly)
- Tag-based browsing, watch history, "continue watching"

### v7.1 · DVR scheduler (~1-2 weeks)
**Status**: future

Beyond ad-hoc record-now buttons:
- `/iptv/dvr` page — schedule recordings from EPG (programme-aware,
  not just "5 min from now")
- Recurring rules ("record every weekday 18:00 CNA News")
- Storage quotas + retention policy (rolling window, auto-delete
  oldest)

### v8.0 · Federation (~weeks-months, far future)
**Status**: speculative

Multi-host Sentinel — your stack runs on Host A, friend's runs on
Host B, shared resources (recommended channels, public catalogues)
sync between them via cryptographic mutual auth. Stremio-addon-like
discovery: "here's my SMDL catalogue, federate me into your
recommendations".

### v9.1 · Entitlement model — Monetization Phase 2 (~1 week)
**Status**: in progress — plumbing built on `feat/edition-split` (2026-05-31, not merged/deployed)
**Depends on**: v9.0
**Spec**: [`sentinel-docs/planning/media-licensing-entitlements.md`](https://docs.az-sentinel.xyz/planning/media-licensing-entitlements/)

Replace the unenforced FAM/COM label with a real **plan → entitlement**
model:
- Registry-keyed plans (key = identity, registry = entitlement SoT) +
  **signed grants** so a rooted device can't forge entitlements
- **Two-rail gate**: legal-boundary caps stay the per-deployment edition
  hard-gate (404, never sold); commercial caps become
  `require_entitlement(grant, cap)` → 402 (mirrors existing `require_scope`)
- **Free funnel**: anonymous browse/YouTube → free-registered
  (favorites + cross-device sync) → paid
- IPTV premium layer: EPG, favorites sync, "channel is live" notifications,
  curated packs, seats — "the act is free, the scale is paid"
- **Modular verticals** (radio / podcast / music) as drop-in manifests on
  a shared spine — grant schema stays a flat namespaced set, never changes

Built so far (`feat/edition-split`): `app/entitlements.py` (plan→cap map,
`require_entitlement` → 402), validate grant enriched with plan/entitlements.
Still to do: registry-keyed plans + signed grants, free-funnel account wiring,
wiring `require_entitlement` into feature routes (needs the grant-transport
decision: how the APK presents its grant per request).

### v9.2 · Sentinel Media TV on Play — Monetization Phase 3 (~1-2 weeks)
**Status**: in progress — TV-first build scaffolded on `feat/edition-split` (2026-05-31); operator handoff remaining
**Depends on**: v9.1
**Spec**: media-licensing-entitlements.md §Distribution & Play compliance + §Implementation status

Ship **service-by-service** — Sentinel Media **TV first** to pull market share:
- **`play` build sub-profile** ✅ `app/profile.py` (`SENTINEL_MEDIA_PROFILE`/
  `SENTINEL_MEDIA_SURFACE`) = community minus aggregation minus key-redeem
- **Google Play Billing** ✅ `app/play_billing.py` + `/api/billing/play/verify`
  — parallel rail → same entitlement SoT (creds-optional, never silent-grants)
- **PWA + TWA** ✅ scaffolded: `app/pwa_routes.py` (manifest/sw/assetlinks) +
  `android-twa/twa-manifest.json` (Bubblewrap, `com.azsentinel.smdltv`)
- Compliance: account deletion ✅ (`/api/account/delete` + `/account/delete`),
  minimal perms ✅, targetSdk 35 ✅; Data Safety + privacy URL documented
- **Operator handoff** (can't automate): public non-CF-Access host, Play
  Console account, signing keys → `TWA_SHA256_CERT_FINGERPRINTS`, 512px PNG
  icon, Billing products + service account JSON. See doc §Launch handoff.

---

## Cross-pillar dependencies

```
v1.0 Foundation ──┬─► v1.5 Watchlist ──┬─► v3.x IPTV
                  │                    │
                  ├─► v2.0 Mini App ───┼─► v2.5 Stickers
                  │                    │
                  └─► v4.0 Apps + Auth─┴─► v4.2 Auth Phase 2
                                       │     │
                                       │     ▼
                                       │  v5.0 Projector (needs projector.cast scope)
                                       │
                                       └─► v9.0 Editions + license ─► v9.1 Entitlements ─► v9.2 Play Billing/dist
                                             │
                                             ▼
                                     v5.1 Miracast compat
                                             │
                                             ▼
                                     v5.2 Wi-Fi hotspot
                                             │
                                             ▼
                                     v5.3 Multi-receiver
                                             │
                                             ▼
                              v6.0 Smart home ───► v6.1 Multi-room audio
                                             │
                                             ▼
                                     v7.0 Library ───► v7.1 DVR
                                             │
                                             ▼
                                     v8.0 Federation
```

## Effort estimate table

| Version | Effort | Pre-req | Stops working without | Net new value |
|---|---|---|---|---|
| v4.1 Recording controls | 1 day | v3.2 | nothing | Cancel/delete recordings |
| v4.2 Auth Phase 2 | 3 hr | v4.0 | nothing (owner-only OK today) | First beta user invitation flow |
| v4.3 Auth Phase 3 | 30 min × N pillars | v4.2 | nothing (per-pillar scope optional) | Beta users can be scoped per-pillar |
| v5.0 Projector Phase 1 | 1 week | v4.0 (cookie auth) | nothing | Cast SMDL apps to projector |
| v5.1 Projector Phase 2 | 2-3 weeks | v5.0 | nothing (Phase 1 still works) | Win+K + MagicOS senders |
| v5.2 Projector Phase 3 | 1 week | v5.1 | Wi-Fi Direct support | No-router operation |
| v5.3 Projector v2 | 2-3 weeks | v5.2 + multi-hardware | nothing | Multi-room sender→receiver picking |
| v6.0 Smart home | 2-4 weeks | v5.x stable | HA / Tasmota deployed | Voice control + scene automations |
| v6.1 Multi-room audio | 2-3 weeks | v5.x + Pi speakers | extra hardware | House-wide audio |
| v7.0 Library | 3-4 weeks | none (parallel) | nothing | Plex-style local media library |
| v7.1 DVR | 1-2 weeks | v3.2 + v7.0 | nothing | Schedule recordings from EPG |
| v8.0 Federation | "weeks" | many | distant future | Multi-host Sentinel mesh |
| v9.0 Editions + license authority | shipped | v4.0 | nothing | Community/private split + key issuance |
| v9.1 Entitlement model | in progress (plumbing built) | v9.0 | nothing | Sellable plans + free funnel + verticals |
| v9.2 Sentinel Media TV on Play | in progress (scaffolded; operator handoff) | v9.1 | public non-CF-Access host + Play account | TV-first Play Store market share |

## Decision principles

When deciding what to build next:

1. **Phase 0 design doc first**. Every major version (v5.x, v6.x, …)
   needs a spec at `metamcp-local/docs/<feature>-v<N>.md` before any
   code. Lock the contract; build against it.
2. **Backwards-compatible rollout**. New pillars ride alongside old.
   Don't break existing flows during a refactor.
3. **Phase 1 first, judge before Phase 2**. Phase 1 is usually 80% of
   user value at 20% of effort. Phase 2+ optional.
4. **Owner-first, beta-user later**. Build the owner path first; gate
   later via auth-perms scopes. Never gate when there's no real beta
   user.
5. **Real-world dependency over speculative**. Don't build a feature
   for "in case I want to do X" — wait for the friction to hit.
6. **Honest cost framing**. If something is hard (Miracast, DRM,
   federation), say so in the spec, scope appropriately, and never
   ship without acknowledging the reliability ceiling.

## How to update this document

- After shipping a new version: add an entry under "Past" with the
  ship date and a 1-paragraph summary. Move the entry from "Planned"
  if it was there.
- After locking a new spec (`metamcp-local/docs/*-v<N>.md`): add an
  entry under "Planned" with the version, effort estimate, status,
  spec link, and 3-bullet summary of what it enables.
- After a checkpoint reveals a planned version needs to split: bump
  the MINOR / change the version number, edit the entry, leave a
  decision note.
- This document is committed to `azfarh95/sentinel-smdl` master.
  Treat it as code: PR-reviewable, never amended in place.

---

## Quick links

- IPTV aggregator v2 spec: [`metamcp-local/docs/iptv-aggregator-v2.md`](../metamcp-local/docs/iptv-aggregator-v2.md)
- Auth-perms v2 spec: [`metamcp-local/docs/auth-perms-v2.md`](../metamcp-local/docs/auth-perms-v2.md)
- SMDL Projector v1 spec: [`metamcp-local/docs/smdl-projector-v1.md`](../metamcp-local/docs/smdl-projector-v1.md)
- Licensing & entitlements + Play distribution spec: [`sentinel-docs/planning/media-licensing-entitlements.md`](https://docs.az-sentinel.xyz/planning/media-licensing-entitlements/)

## Changelog

- 2026-05-27 — initial roadmap; backfilled v1.0 → v4.0 from history.
                Drafted v4.1 → v8.0 forward plan. azfar.
- 2026-05-31 — backfilled shipped-since-v4.0 work: v1.6 (torrent/RD
                fallback + progressive), v3.5 (in-app HLS relay + Family
                TV), v9.0 (editions + license authority). Opened the v9
                monetization pillar; planned v9.1 (entitlement model) +
                v9.2 (Play Billing + TWA distribution). Specs cross-linked
                to sentinel-docs/planning/media-licensing-entitlements.md.
- 2026-05-31 — built v9.1 entitlement plumbing + v9.2 Sentinel Media TV
                Play build on `feat/edition-split` (TV-first, service-by-
                service strategy): app/entitlements.py, app/profile.py
                (play sub-profile), app/play_billing.py (+ verify route),
                account deletion, app/pwa_routes.py (PWA/TWA), android-twa
                Bubblewrap manifest. Operator handoff remains (public host,
                Play account, signing keys, Billing products). Not merged.
- 2026-06-05 — backfilled v2.6 (Sticker Studio + transparent video + unified
                pack model + send-a-sticker import + per-frame video cutout) on
                `feat/trakt-sync`. Recorded ADR MED-003 (CPU matting; GPU/
                DirectML evaluated + rejected with benchmarks). azfar + Claude.
