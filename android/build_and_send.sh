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

CAPTION=$'📺 SMDL IPTV v0.1.0 (debug-signed)\n\nSideload: enable "Install unknown apps" for your file manager, tap the APK.\n\nFirst launch: paste your OWNER_AUTH_TOKEN into the overlay → cookie persists 90d → channels load.\n\nVLC handoff: tapping a stream URL inside a channel hands `.m3u8` to the OS; install VLC for Android so the chooser offers it.'

echo "==> sending to Telegram chat $CHAT_ID"
curl -fsS -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendDocument" \
     -F "chat_id=${CHAT_ID}" \
     -F "document=@${APK}" \
     -F "caption=${CAPTION}" \
     -o /tmp/tg_send.json
echo
echo "TG response:"
head -c 400 /tmp/tg_send.json; echo
