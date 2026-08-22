#!/usr/bin/env bash
set -euo pipefail

: "${GITHUB_WORKSPACE:?GITHUB_WORKSPACE is required}"
: "${APP_ID:?APP_ID is required}"

# Keep the publisher aligned with the applicationId that was actually compiled from StatMaker UAT.
# The workflow's APP_ID is only an expectation; stale hard-coding must not make adb target a package
# that is not present in the freshly built APK.
BUILD_GRADLE="$GITHUB_WORKSPACE/statmaker-private/app/build.gradle.kts"
test -s "$BUILD_GRADLE"
COMPILED_APP_ID="$(sed -nE 's/^[[:space:]]*applicationId[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/p' "$BUILD_GRADLE" | head -1)"
if [[ -z "$COMPILED_APP_ID" ]]; then
  echo "Could not resolve StatMaker applicationId from $BUILD_GRADLE" >&2
  exit 1
fi
if [[ "$APP_ID" != "$COMPILED_APP_ID" ]]; then
  echo "APP_READY_APP_ID_NORMALIZED workflow=$APP_ID compiled=$COMPILED_APP_ID"
fi
APP_ID="$COMPILED_APP_ID"

UEFA_REF=origin/build/uefa-qualifier-feed-20260720
u="$GITHUB_WORKSPACE/__uefa__"
rm -rf "$u"
mkdir -p "$u/data/statmaker" "$u/odds/odds_api_io"

git -C "$GITHUB_WORKSPACE" show "$UEFA_REF:data/statmaker/uefa_update_manifest.json" > "$u/data/statmaker/uefa_update_manifest.json"
for c in champions_league europa_league conference_league; do
  git -C "$GITHUB_WORKSPACE" show "$UEFA_REF:odds/odds_api_io/${c}_odds.json" > "$u/odds/odds_api_io/${c}_odds.json"
done

HTTP_LOG="$GITHUB_WORKSPACE/app-ready-http.log"
python3 -m http.server 8765 --bind 127.0.0.1 --directory "$GITHUB_WORKSPACE" > "$HTTP_LOG" 2>&1 &
server_pid=$!
cleanup() {
  adb reverse --remove tcp:8765 >/dev/null 2>&1 || true
  kill "$server_pid" >/dev/null 2>&1 || true
}
trap cleanup EXIT

server_healthy() {
  kill -0 "$server_pid" >/dev/null 2>&1 &&
    curl -fsS --max-time 5 "http://127.0.0.1:8765/data/statmaker/domestic_enriched/index.json" >/dev/null &&
    curl -fsS --max-time 5 "http://127.0.0.1:8765/__uefa__/data/statmaker/uefa_update_manifest.json" >/dev/null
}

server_ready=0
for _ in $(seq 1 20); do
  if server_healthy; then
    server_ready=1
    break
  fi
  sleep 1
done
if [[ "$server_ready" -ne 1 ]]; then
  echo "App-ready HTTP server did not become healthy" >&2
  cat "$HTTP_LOG" >&2 || true
  exit 1
fi

echo "APP_READY_HTTP_OK pid=$server_pid"

APK="$GITHUB_WORKSPACE/statmaker-private/app/build/outputs/apk/debug/app-debug.apk"
test -s "$APK"
adb install -r "$APK"

# Do not depend on the emulator-specific 10.0.2.2 host route. The temporary publisher APK uses
# 127.0.0.1:8765 and ADB reverse provides a deterministic tunnel back to the runner HTTP server.
adb reverse --remove tcp:8765 >/dev/null 2>&1 || true
adb reverse tcp:8765 tcp:8765
if ! adb reverse --list | grep -q "tcp:8765 tcp:8765"; then
  echo "ADB reverse tunnel for app-ready HTTP server was not installed" >&2
  adb reverse --list >&2 || true
  exit 1
fi
echo "APP_READY_ADB_REVERSE_OK"

adb logcat -c
adb shell am start -W -n "$APP_ID/com.statmaker.app.StatMakerWelcomeActivity"

ok=0
last_stage=""
# This is an off-device artifact build, not an interactive phone update. Keep a bounded window,
# but fail immediately if transport or producer semantics become invalid instead of exporting a
# corrupt/empty generation and discovering it in a later workflow step.
for _ in $(seq 1 300); do
  if ! server_healthy; then
    echo "App-ready HTTP server became unhealthy while producer was running" >&2
    cat "$HTTP_LOG" >&2 || true
    adb logcat -d "StatMakerAppReady:V" "*:S" >&2 || true
    exit 1
  fi

  appready_logs="$(adb logcat -d -s StatMakerAppReady:V "*:S" || true)"
  if grep -q " E StatMakerAppReady:" <<<"$appready_logs"; then
    echo "App-ready producer reported a data task error" >&2
    printf '%s\n' "$appready_logs" >&2
    exit 1
  fi

  stage="$(grep "stage=" <<<"$appready_logs" | tail -1 || true)"
  if [[ -n "$stage" && "$stage" != "$last_stage" ]]; then
    echo "$stage"
    last_stage="$stage"
  fi

  if adb logcat -d -s StatMakerWelcomePerf:I "*:S" | grep -q "total="; then
    ok=1
    break
  fi
  sleep 5
done

appready_logs="$(adb logcat -d -s StatMakerAppReady:V "*:S" || true)"
printf '%s\n' "$appready_logs"
if [[ "$ok" -ne 1 ]]; then
  echo "App-ready producer did not complete within 25 minutes" >&2
  adb logcat -d | tail -600 >&2
  exit 1
fi

# A Welcome trace completion alone is not a valid app-ready generation. Require every data stage
# and all four prepared competitions before exporting anything from the emulator.
required_stages=(
  domestic_resolved
  normalized_resolved
  domestic_odds_resolved
  champions_odds_resolved
  europa_odds_resolved
  conference_odds_resolved
  support_resolved
  all_futures_resolved
  prepared_begin
)
for stage_name in "${required_stages[@]}"; do
  if ! grep -q "stage=${stage_name}" <<<"$appready_logs"; then
    echo "App-ready producer missing required stage=${stage_name}" >&2
    exit 1
  fi
done
if ! grep -q "stage=prepared_complete ready=4 requested=4" <<<"$appready_logs"; then
  echo "App-ready producer did not prepare all four competitions" >&2
  exit 1
fi
if grep -q " E StatMakerAppReady:" <<<"$appready_logs"; then
  echo "App-ready producer completed with task errors" >&2
  exit 1
fi

echo "APP_READY_PRODUCER_SEMANTICS_OK"

adb shell am force-stop "$APP_ID"
sleep 2

# Fail here, inside the producer step, if the expected SQLite files were never created. This avoids
# redirecting run-as error text into a .db file and reporting the problem later as "file is not a database".
adb shell run-as "$APP_ID" ls -l databases/statmaker.db >/dev/null
adb shell run-as "$APP_ID" ls -l databases/statmaker_prepared_betting.db >/dev/null

r="$GITHUB_WORKSPACE/app-ready-export/raw"
rm -rf "$GITHUB_WORKSPACE/app-ready-export"
mkdir -p "$r/databases" "$r/files/statmaker_stats_snapshots" "$r/shared_prefs"
adb exec-out run-as "$APP_ID" cat databases/statmaker.db > "$r/databases/statmaker.db"
adb exec-out run-as "$APP_ID" cat databases/statmaker_prepared_betting.db > "$r/databases/statmaker_prepared_betting.db"
adb exec-out run-as "$APP_ID" cat files/domestic_normalized_stats_v2.bin > "$r/files/domestic_normalized_stats_v2.bin"
for c in champions_league europa_league conference_league; do
  adb exec-out run-as "$APP_ID" cat "files/statmaker_stats_snapshots/$c.bin" > "$r/files/statmaker_stats_snapshots/$c.bin"
done
adb exec-out run-as "$APP_ID" cat shared_prefs/statmaker_prepared_data_versions.xml > "$r/shared_prefs/statmaker_prepared_data_versions.xml"

test -s "$r/shared_prefs/statmaker_prepared_data_versions.xml"
grep -q "domestic_history_fingerprint" "$r/shared_prefs/statmaker_prepared_data_versions.xml"
grep -q "uefa_support_fingerprint" "$r/shared_prefs/statmaker_prepared_data_versions.xml"

python3 - "$r/databases/statmaker.db" "$r/databases/statmaker_prepared_betting.db" <<'PY'
import sqlite3
import sys
from pathlib import Path

for raw in sys.argv[1:]:
    path = Path(raw)
    if not path.is_file() or path.stat().st_size <= 16:
        raise SystemExit(f"Missing/empty exported SQLite DB: {path}")
    with path.open("rb") as handle:
        header = handle.read(16)
    if header != b"SQLite format 3\x00":
        raise SystemExit(f"Invalid SQLite header after emulator export: {path}")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        result = connection.execute("PRAGMA quick_check").fetchone()
    finally:
        connection.close()
    if not result or result[0] != "ok":
        raise SystemExit(f"SQLite quick_check failed after emulator export: {path}: {result}")
    print(f"APP_READY_SQLITE_EXPORT_OK path={path} bytes={path.stat().st_size}")
PY
