# Sentinel SMDL — Desktop

Native Windows / macOS / Linux wrap of the SMDL Mini App. Same pattern
as [sentinel-watchdog/admin](https://github.com/azfarh95/sentinel-watchdog/tree/master/admin).

The window opens straight to `https://media.az-sentinel.xyz/iptv`. The
SMDL session cookie lives on `.az-sentinel.xyz`, so once you've
activated the suite anywhere (TG Mini App, browser, etc.) this app
picks the auth up automatically.

## What's in the window

Whatever the live `/iptv` page renders — the desktop app is intentionally
a thin pane, not a fork. Bug fixes + features land in
`app/iptv_routes.py` and reach the desktop on the next browser refresh.

## Build (Windows)

```cmd
cd desktop
build_tauri.bat
```

First run is 5–10 min (full release compile). Reruns ~30s.

Prerequisites:
- **Rust** (stable) via [rustup](https://rustup.rs/)
- **Visual Studio 2022 Build Tools** with the "C++ Build Tools" workload
- **Node.js** + npm (for `@tauri-apps/cli`)
- **WebView2 runtime** (preinstalled on Windows 11; auto-installed by the
  NSIS bundle on Windows 10)

Outputs:
```
src-tauri/target/release/sentinel-smdl-desktop.exe       ← bare exe
src-tauri/target/release/bundle/msi/*.msi                ← MSI installer
src-tauri/target/release/bundle/nsis/*-setup.exe         ← NSIS installer
```

## Build (macOS / Linux)

```bash
cd desktop
npm install
npx tauri build
```

## Install

Run the NSIS `-setup.exe` (per-machine install — adds Start menu entry
+ optional desktop shortcut) **or** the `.msi` (silent / GPO-friendly).

## Dev loop

```cmd
cd desktop
npx tauri dev
```

Opens an unsigned debug window pointed at the same remote URL. Devtools
available via F12. The hot-reload only applies to the placeholder
`dist/index.html`; for SMDL UI changes you still rebuild the SMDL
container.

## Architecture note

The window's `url` field in `src-tauri/tauri.conf.json` is set directly
to the public HTTPS hostname. This skips the bundled `dist/` entirely
once the WebView navigates. The `dist/index.html` placeholder only ever
shows during the boot frame, and contains a JS redirect as a safety
net in case the Tauri runtime ever falls back to it.

## Why not Electron?

- Tauri produces ~3 MB exes vs Electron's ~150 MB.
- Reuses WebView2 / WKWebView already on the OS instead of bundling Chromium.
- Watchdog v2's admin app already established the Sentinel desktop pattern
  on Tauri — keeping the stack uniform.

## Future

- macOS `.dmg` signing (currently unsigned — gatekeeper will warn).
- Auto-update — Tauri's updater plugin against a GitHub releases feed.
- A "Live TV" tray icon that pops the latest scheduled-DVR recording.
