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
start_epoch="$(date +%s)"
last_progress_epoch="$start_epoch"
max_total_seconds=2700
max_idle_seconds=1200
# This is an off-device artifact build, not an interactive phone update. Use a progress-aware
# watchdog rather than a fixed 25-minute wall clock: real producer stage changes extend the run,
# while transport/data errors still fail immediately and a genuinely stalled producer is bounded.
for _ in $(seq 1 540); do
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
    last_progress_epoch="$(date +%s)"
  fi

  now_epoch="$(date +%s)"
  if (( now_epoch - last_progress_epoch >= max_idle_seconds )); then
    echo "App-ready producer made no stage progress for ${max_idle_seconds}s; last stage: ${last_stage:-none}" >&2
    printf '%s\\n' "$appready_logs" >&2
    adb logcat -d | tail -600 >&2 || true
    exit 1
  fi
  if (( now_epoch - start_epoch >= max_total_seconds )); then
    echo "App-ready producer exceeded total build ceiling ${max_total_seconds}s; last stage: ${last_stage:-none}" >&2
    printf '%s\\n' "$appready_logs" >&2
    exit 1
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
  echo "App-ready producer did not complete within the progress-aware build window" >&2
  adb logcat -d | tail -600 >&2
  exit 1
fi

# A Welcome trace completion alone is not a valid app-ready generation. Require every data stage
# and require the producer to prepare exactly the competitions it requested. Some competitions can
# legitimately have zero pending/live fixtures, so the requested count is dynamic.
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
  recommendations_begin
  recommendations_candidates_begin
  recommendations_candidates_complete
  recommendations_complete
)
for stage_name in "${required_stages[@]}"; do
  if ! grep -q "stage=${stage_name}" <<<"$appready_logs"; then
    echo "App-ready producer missing required stage=${stage_name}" >&2
    exit 1
  fi
done
prepared_complete_line="$(grep "stage=prepared_complete ready=" <<<"$appready_logs" | tail -1 || true)"
if [[ ! "$prepared_complete_line" =~ stage=prepared_complete[[:space:]]ready=([0-9]+)[[:space:]]requested=([0-9]+) ]]; then
  echo "App-ready producer missing valid prepared_complete summary" >&2
  exit 1
fi
prepared_ready="${BASH_REMATCH[1]}"
prepared_requested="${BASH_REMATCH[2]}"
if (( prepared_requested != 4 || prepared_ready != 4 )); then
  echo "App-ready producer must prepare production-compatible 4/4 snapshots; got ${prepared_ready}/${prepared_requested}" >&2
  exit 1
fi
if grep -q " E StatMakerAppReady:" <<<"$appready_logs"; then
  echo "App-ready producer completed with task errors" >&2
  exit 1
fi

recommendations_complete_line="$(grep "stage=recommendations_complete generation=" <<<"$appready_logs" | tail -1 || true)"
if [[ ! "$recommendations_complete_line" =~ stage=recommendations_complete[[:space:]]generation=([0-9a-f]{64})[[:space:]]candidates=([0-9]+)[[:space:]]reused=(true|false)[[:space:]]elapsedMs=([0-9]+) ]]; then
  echo "App-ready producer missing valid recommendations_complete summary" >&2
  exit 1
fi
recommendation_generation="${BASH_REMATCH[1]}"
recommendation_candidates="${BASH_REMATCH[2]}"
if (( recommendation_candidates <= 0 )); then
  echo "App-ready producer created an empty prepared recommendation generation" >&2
  exit 1
fi
echo "APP_READY_PATTERN_GENERATION_OK generation=$recommendation_generation candidates=$recommendation_candidates"

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
        if not result or result[0] != "ok":
            raise SystemExit(f"SQLite quick_check failed after emulator export: {path}: {result}")

        if path.name == "statmaker_prepared_betting.db":
            user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if user_version < 10:
                raise SystemExit(f"Prepared DB schema must be >=10; got {user_version}")

            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            required_tables = {
                "prepared_pattern_generation",
                "prepared_pattern_candidates",
            }
            missing_tables = sorted(required_tables - tables)
            if missing_tables:
                raise SystemExit(
                    "Prepared DB missing v10 recommendation tables: " + ", ".join(missing_tables)
                )

            indexes = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                ).fetchall()
            }
            required_indexes = {
                "idx_prepared_pattern_generation_ready",
                "idx_prepared_pattern_candidates_scope",
                "idx_prepared_pattern_candidates_rank",
                "idx_prepared_pattern_candidates_competition_rank",
            }
            missing_indexes = sorted(required_indexes - indexes)
            if missing_indexes:
                raise SystemExit(
                    "Prepared DB missing v10 recommendation indexes: " + ", ".join(missing_indexes)
                )

            generation = connection.execute(
                """
                SELECT generation_id, candidate_count, rules_fingerprint
                FROM prepared_pattern_generation
                WHERE state='ready'
                ORDER BY built_at_ms DESC
                LIMIT 1
                """
            ).fetchone()
            if not generation:
                raise SystemExit("Prepared DB has no READY recommendation generation")
            generation_id, candidate_count, rules_fingerprint = generation
            if int(candidate_count) <= 0:
                raise SystemExit("Prepared recommendation generation has 0 candidates")
            if rules_fingerprint != "pattern-policy-v2-final-read-model-v3":
                raise SystemExit(
                    f"Unexpected prepared recommendation rules fingerprint: {rules_fingerprint}"
                )
            actual_candidates = int(
                connection.execute(
                    "SELECT COUNT(*) FROM prepared_pattern_candidates WHERE generation_id=?",
                    (generation_id,),
                ).fetchone()[0]
            )
            if actual_candidates != int(candidate_count):
                raise SystemExit(
                    "Prepared recommendation candidate-count mismatch: "
                    f"meta={candidate_count} actual={actual_candidates}"
                )
            print(
                "APP_READY_PATTERN_SQLITE_OK",
                f"schema={user_version}",
                f"generation={generation_id}",
                f"candidates={candidate_count}",
            )
    finally:
        connection.close()
    print(f"APP_READY_SQLITE_EXPORT_OK path={path} bytes={path.stat().st_size}")
PY
