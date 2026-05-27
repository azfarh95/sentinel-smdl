# SMDL IPTV — Android WebView wrapper

Thin Android shell that loads `https://media.az-sentinel.xyz/iptv` in a
WebView with persistent cookies. The Netflix-style channel browser, EPG,
recording UI and everything else lives on the SMDL backend (`/app/iptv*`);
this APK is just a 3 MB launcher icon + cookie jar.

## Build + deliver

```bash
./build_and_send.sh
```

What it does:
1. Spins up `mingc/android-build-box:1.29.0` Docker image (one-time ~4 GB pull)
2. Bootstraps Gradle 8.5 into a host-mounted `.gradle-bin/` (one-time ~120 MB)
3. Runs `gradle :app:assembleDebug` → `app/build/outputs/apk/debug/app-debug.apk`
4. Sends the APK to the owner's Telegram via `SMDL_BOT_TOKEN` from `/c/Users/azfar/metamcp-local/.env.local`

First build: ~5 min. Subsequent: ~30 s.

## Architecture

| Component | Purpose |
|---|---|
| `MainActivity.kt` | WebView with `CookieManager` + URL-handler intent routing for `.m3u8`/`.mpd` so VLC can claim them |
| `AndroidManifest.xml` | INTERNET perm; `LEANBACK_LAUNCHER` category so it appears on AndroidTV |
| `res/drawable/ic_launcher_*.xml` | Adaptive icon (TV + play triangle on dark blue) |

Min SDK 26 (Android 8+, covers Android TV 9+).

## When to rebuild

**Not required** for any HTML / JS / API change in SMDL — the WebView
fetches `/iptv` fresh on each launch. Only rebuild when changing:

- `MainActivity.kt` (intent handling, fullscreen, hardware-key nav)
- `AndroidManifest.xml` (permissions, intent filters)
- App icon or launcher metadata
- The hardcoded URL (currently `https://media.az-sentinel.xyz/iptv`)
- `versionCode` / `versionName` (only matters for Play Store; sideload doesn't care)

## Auth flow on first launch

WebView loads `/iptv` → JSON calls 401 → page shows a full-screen overlay
asking for `OWNER_AUTH_TOKEN`. Paste once → SMDL backend's `/auth/setup`
returns a `sentinel_apk_session` cookie domain-scoped to `.az-sentinel.xyz`
(90 day TTL) → all subsequent JSON calls authenticate via cookie.

The token never leaves the device; the cookie is just an HMAC-signed
session identifier, not the token itself.
