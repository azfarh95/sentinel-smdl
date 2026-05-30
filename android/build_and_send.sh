#!/usr/bin/env bash
# Build the SMDL IPTV APK in a Docker container, then send it to the
# owner on Telegram via the SMDL bot. Self-contained — re-runnable.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="/c/Users/azfar/metamcp-local/.env.local"
BUILD_IMAGE="mingc/android-build-box:1.29.0"
CHAT_ID="898259417"   # azfar (memory: project_sentinel_finance_*)

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: $ENV_FILE missing — needed for SMDL_BOT_TOKEN" >&2
  exit 1
fi
BOT_TOKEN="$(grep -E '^SMDL_BOT_TOKEN=' "$ENV_FILE" | head -1 | cut -d= -f2-)"
if [[ -z "$BOT_TOKEN" ]]; then
  echo "ERROR: SMDL_BOT_TOKEN not in $ENV_FILE" >&2
  exit 1
fi

mkdir -p "$PROJECT_DIR/.gradle-cache" "$PROJECT_DIR/.gradle-bin"

echo "==> building APK (first run downloads gradle ~120MB + AGP deps; cached after)"
# mingc/android-build-box has the Android SDK but no gradle — we install it
# into a host-mounted dir so the second run is instant.
# MSYS_NO_PATHCONV stops Git-Bash from rewriting the in-container linux
# paths (-w /project, etc.) into Windows paths.
# Signing: app/build.gradle.kts pins to debug.keystore in the project root
# (committed to the repo) — every build produces APKs with the same cert,
# so sideloaded rebuilds install in place. No /root/.android mount needed.
MSYS_NO_PATHCONV=1 docker run --rm \
  -v "$PROJECT_DIR":/project \
  -v "$PROJECT_DIR/.gradle-cache":/root/.gradle \
  -v "$PROJECT_DIR/.gradle-bin":/opt/gradle-tools \
  -w //project \
  "$BUILD_IMAGE" \
  bash -lc '
    set -e
    GRADLE_VER=8.5
    GRADLE_HOME=/opt/gradle-tools/gradle-${GRADLE_VER}
    if [ ! -x "${GRADLE_HOME}/bin/gradle" ]; then
      echo "==> bootstrapping gradle ${GRADLE_VER}"
      apt-get update -qq && apt-get install -qq -y curl unzip
      curl -fsSL "https://services.gradle.org/distributions/gradle-${GRADLE_VER}-bin.zip" -o /tmp/g.zip
      unzip -q /tmp/g.zip -d /opt/gradle-tools
    fi
    export PATH="${GRADLE_HOME}/bin:$PATH"
    export ANDROID_HOME=/opt/android-sdk
    gradle --version | head -8
    gradle --no-daemon :app:assembleDebug --stacktrace
  '

APK="$PROJECT_DIR/app/build/outputs/apk/debug/app-debug.apk"
if [[ ! -f "$APK" ]]; then
  echo "ERROR: APK not found at $APK" >&2
  exit 1
fi
ls -lh "$APK"

# ── Publish to the Sentinel Apps store (suite.az-sentinel.xyz/apps) ──
# Copies the APK to metamcp-local/sentinel-apps/smdl-iptv/v<version>/app.apk
# and patches the version entry in manifest.json with the new size + sha.
SENTINEL_APPS_DIR="/c/Users/azfar/metamcp-local/sentinel-apps"
APP_ID="smdl-iptv"
if [[ -d "$SENTINEL_APPS_DIR" ]]; then
  VER="$(grep -E 'versionName\s*=' "$PROJECT_DIR/app/build.gradle.kts" \
         | head -1 | sed -E 's/.*"([^"]+)".*/\1/')"
  if [[ -n "$VER" ]]; then
    DEST_DIR="$SENTINEL_APPS_DIR/$APP_ID/v$VER"
    mkdir -p "$DEST_DIR"
    cp "$APK" "$DEST_DIR/app.apk"
    SIZE=$(stat -c%s "$DEST_DIR/app.apk")
    SHA=$(sha256sum "$DEST_DIR/app.apk" | awk '{print $1}')
    echo "==> published to sentinel-apps: $DEST_DIR/app.apk (${SIZE}B, sha256=${SHA:0:16}…)"
    # Stamp the manifest's `latest` + the first versions[] entry to match.
    # Find host Python — Git-Bash on Windows exposes the launcher as
    # `py` (Python Launcher); some setups also have `python` or
    # `python.exe`. We try them in order and bail with a warning if
    # none are found (APK still publishes; only the manifest update
    # is skipped).
    PY_BIN=""
    for candidate in python python.exe py py.exe python3 python3.exe; do
      if command -v "$candidate" >/dev/null 2>&1; then
        PY_BIN="$candidate"
        break
      fi
    done
    if [[ -z "$PY_BIN" ]]; then
      echo "WARN: no host python found; APK published but manifest.json not updated" >&2
    else
      # Path normalisation for Git-Bash → Windows Python. Python on Windows
      # interprets `/c/Users/...` as a UNC-style path rooted at `\c\Users\...`
      # and FileNotFoundError follows. cygpath -w converts it to `C:\Users\...`
      # which pathlib.Path handles correctly via the raw-string literal below.
      MANIFEST_PATH="$SENTINEL_APPS_DIR/manifest.json"
      if command -v cygpath >/dev/null 2>&1; then
        MANIFEST_PATH_PY="$(cygpath -w "$MANIFEST_PATH")"
      else
        MANIFEST_PATH_PY="$MANIFEST_PATH"
      fi
      "$PY_BIN" -c "
import json, datetime, pathlib
mp = pathlib.Path(r'$MANIFEST_PATH_PY')
m = json.loads(mp.read_text(encoding='utf-8'))
for app in m.get('apps', []):
    if app.get('id') != '$APP_ID': continue
    app['latest'] = '$VER'
    new = {
        'version': '$VER',
        'released': datetime.date.today().isoformat(),
        'file': 'v$VER/app.apk',
        'size_bytes': $SIZE,
        'sha256': '$SHA',
        'min_sdk': app.get('versions', [{}])[0].get('min_sdk', 26),
        'target_sdk': app.get('versions', [{}])[0].get('target_sdk', 34),
        'changelog': app.get('versions', [{}])[0].get('changelog', ''),
    }
    # de-dup: drop any existing entry with the same version, then prepend
    app['versions'] = [v for v in app.get('versions', []) if v.get('version') != '$VER']
    app['versions'].insert(0, new)
m['updated_at'] = datetime.datetime.utcnow().isoformat() + 'Z'
mp.write_text(json.dumps(m, indent=2, ensure_ascii=False), encoding='utf-8')
print('==> manifest updated:', mp)
"
    fi
  else
    echo "WARN: could not read versionName from build.gradle.kts — skipping publish" >&2
  fi
else
  echo "WARN: $SENTINEL_APPS_DIR not present — skipping publish (build only)" >&2
fi

VER="${VER:-$(grep -E 'versionName\s*=' "$PROJECT_DIR/app/build.gradle.kts" | head -1 | sed -E 's/.*"([^"]+)".*/\1/')}"
CAPTION=$'SMDL IPTV v'"$VER"$' (debug-signed)\n\nWhat\'s new:\n- New app icon (Sentinel Media branding logo).\n- Theater video now goes fullscreen + landscape on phones: tap fullscreen on a movie/series and it rotates to widescreen; back exits.\n- Settings page now has a category top-nav (Accounts / Playback / Storage / Addons).\n\nSideload over the previous version - cookie + filters survive.'

echo "==> sending to Telegram chat $CHAT_ID"
# NB: keep the caption ASCII (no emoji/• bullets) and pass an explicit
# filename — Telegram sendDocument 400s on some non-ASCII captions.
curl -fsS -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendDocument" \
     -F "chat_id=${CHAT_ID}" \
     -F "document=@${APK};filename=smdl-iptv-v${VER}.apk" \
     -F "caption=${CAPTION}" \
     -o /tmp/tg_send.json
echo
echo "TG response:"
head -c 400 /tmp/tg_send.json; echo
