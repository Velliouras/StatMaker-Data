#!/usr/bin/env bash
set -euo pipefail

: "${GITHUB_WORKSPACE:?GITHUB_WORKSPACE is required}"
: "${APP_ID:?APP_ID is required}"

UEFA_REF=origin/build/uefa-qualifier-feed-20260720
u="$GITHUB_WORKSPACE/__uefa__"
rm -rf "$u"
mkdir -p "$u/data/statmaker" "$u/odds/odds_api_io"

git -C "$GITHUB_WORKSPACE" show "$UEFA_REF:data/statmaker/uefa_update_manifest.json" > "$u/data/statmaker/uefa_update_manifest.json"
for c in champions_league europa_league conference_league; do
  git -C "$GITHUB_WORKSPACE" show "$UEFA_REF:odds/odds_api_io/${c}_odds.json" > "$u/odds/odds_api_io/${c}_odds.json"
done

python3 -m http.server 8765 --bind 0.0.0.0 --directory "$GITHUB_WORKSPACE" > "$GITHUB_WORKSPACE/app-ready-http.log" 2>&1 &
server_pid=$!
cleanup_server() {
  kill "$server_pid" >/dev/null 2>&1 || true
}
trap cleanup_server EXIT

for _ in $(seq 1 20); do
  if curl -fsS "http://127.0.0.1:8765/data/statmaker/domestic_enriched/index.json" >/dev/null; then
    break
  fi
  sleep 1
done
curl -fsS "http://127.0.0.1:8765/data/statmaker/domestic_enriched/index.json" >/dev/null
curl -fsS "http://127.0.0.1:8765/__uefa__/data/statmaker/uefa_update_manifest.json" >/dev/null

APK="$GITHUB_WORKSPACE/statmaker-private/app/build/outputs/apk/debug/app-debug.apk"
test -s "$APK"
adb install -r "$APK"
adb logcat -c
adb shell am start -W -n "$APP_ID/com.statmaker.app.StatMakerWelcomeActivity" || true

ok=0
last_stage=""
for _ in $(seq 1 96); do
  if adb logcat -d -s StatMakerWelcomePerf:I "*:S" | grep -q "total="; then
    ok=1
    break
  fi
  stage="$(adb logcat -d -s StatMakerAppReady:I "*:S" | grep "stage=" | tail -1 || true)"
  if [[ -n "$stage" && "$stage" != "$last_stage" ]]; then
    echo "$stage"
    last_stage="$stage"
  fi
  sleep 5
done

adb logcat -d "StatMakerAppReady:V" "*:S" || true
if [[ "$ok" -ne 1 ]]; then
  echo "App-ready producer did not complete within 8 minutes" >&2
  adb logcat -d | tail -400
  exit 1
fi

adb shell am force-stop "$APP_ID"
sleep 2

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
