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

## RESUMABLE_CHECKPOINT_SEED
# Prefer an exact resumable checkpoint from a previous failed/cancelled publisher.
CHECKPOINT_IN="$GITHUB_WORKSPACE/app-ready-checkpoint-incoming"
CHECKPOINT_RESTORED=0
if [[ -s "$CHECKPOINT_IN/checkpoint.json" ]]; then
  if python3 - "$GITHUB_WORKSPACE" "$u" "$CHECKPOINT_IN/checkpoint.json" <<'PY'
import json, sys
from pathlib import Path
workspace=Path(sys.argv[1]); uefa_root=Path(sys.argv[2])
checkpoint=json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
main=json.loads((workspace/"data/statmaker/update_manifest.json").read_text(encoding="utf-8"))
uefa=json.loads((uefa_root/"data/statmaker/uefa_update_manifest.json").read_text(encoding="utf-8"))
if int(checkpoint.get("preparedReadyCount",0)) != 4:
    raise SystemExit(1)
exact = (
    checkpoint.get("mainContentVersion") == main.get("contentVersion")
    and checkpoint.get("uefaContentVersion") == uefa.get("contentVersion")
)
print(
    "APP_READY_CHECKPOINT_SEED",
    "exact" if exact else "stale-but-reusable",
    "checkpoint_main="+str(checkpoint.get("mainContentVersion",""))[:12],
    "current_main="+str(main.get("contentVersion",""))[:12],
    "checkpoint_uefa="+str(checkpoint.get("uefaContentVersion",""))[:12],
    "current_uefa="+str(uefa.get("contentVersion",""))[:12],
)
PY
  then
    for rel in \
      databases/statmaker.db \
      databases/statmaker_prepared_betting.db \
      files/domestic_normalized_stats_v2.bin \
      files/statmaker_stats_snapshots/champions_league.bin \
      files/statmaker_stats_snapshots/europa_league.bin \
      files/statmaker_stats_snapshots/conference_league.bin \
      files/app_ready_odds/domestic.json \
      files/app_ready_odds/champions_league.json \
      files/app_ready_odds/europa_league.json \
      files/app_ready_odds/conference_league.json \
      shared_prefs/statmaker_prepared_data_versions.xml \
      shared_prefs/statmaker_data_manifests.xml \
      shared_prefs/statmaker_uefa_support_history.xml \
      shared_prefs/statmaker_app_ready_artifacts.xml
    do
      test -s "$CHECKPOINT_IN/$rel"
      dir="$(dirname "$rel")"
      adb shell run-as "$APP_ID" mkdir -p "$dir"
      tmp="/data/local/tmp/$(basename "$rel")"
      adb push "$CHECKPOINT_IN/$rel" "$tmp" >/dev/null
      adb shell run-as "$APP_ID" cp "$tmp" "$rel"
      adb shell rm -f "$tmp"
    done
    CHECKPOINT_RESTORED=1
    echo "APP_READY_CHECKPOINT_RESTORED_AS_SEED"
  else
    echo "APP_READY_CHECKPOINT_INVALID ignored"
  fi
fi

# Otherwise seed from the most recent verified app-ready generation.
SEED_ROOT="$GITHUB_WORKSPACE/app-ready-seed"
rm -rf "$SEED_ROOT"
mkdir -p "$SEED_ROOT"

git -C "$GITHUB_WORKSPACE" fetch --no-tags origin +refs/heads/main:refs/remotes/origin/main
SEED_COMMIT="$(git -C "$GITHUB_WORKSPACE" log -1 --format=%H -- data/statmaker/app_ready/update_manifest.json || true)"
if [[ "$CHECKPOINT_RESTORED" -eq 0 && -n "$SEED_COMMIT" ]]; then
  echo "APP_READY_SEED_COMMIT $SEED_COMMIT"
  git -C "$GITHUB_WORKSPACE" show "$SEED_COMMIT:data/statmaker/app_ready/update_manifest.json" > "$SEED_ROOT/update_manifest.json"

  python3 - "$GITHUB_WORKSPACE" "$SEED_ROOT" "$SEED_COMMIT" <<'PY'
import html
import json
import subprocess
import sys
import zipfile
from pathlib import Path

workspace = Path(sys.argv[1])
seed_root = Path(sys.argv[2])
seed_commit = sys.argv[3]

manifest = json.loads((seed_root / "update_manifest.json").read_text(encoding="utf-8"))
artifacts = manifest.get("artifacts", [])
betting = next((x for x in artifacts if x.get("id") == "app_ready_betting_bundle"), None)
if not betting:
    raise SystemExit("Seed manifest has no app_ready_betting_bundle")

bundle_rel = str(betting.get("path") or "").strip()
if not bundle_rel:
    raise SystemExit("Seed betting artifact path is empty")

bundle_path = seed_root / "betting_bundle.zip"
with bundle_path.open("wb") as handle:
    handle.write(
        subprocess.check_output(
            ["git", "-C", str(workspace), "show", f"{seed_commit}:{bundle_rel}"]
        )
    )

extract_root = seed_root / "bundle"
with zipfile.ZipFile(bundle_path) as archive:
    archive.extractall(extract_root)

required = [
    extract_root / "databases/statmaker_prepared_betting.db",
    extract_root / "files/app_ready_odds/domestic.json",
    extract_root / "files/app_ready_odds/champions_league.json",
    extract_root / "files/app_ready_odds/europa_league.json",
    extract_root / "files/app_ready_odds/conference_league.json",
]
for path in required:
    if not path.is_file() or path.stat().st_size <= 0:
        raise SystemExit(f"Seed bundle missing {path}")

metadata = manifest.get("metadata", {})
main_raw = str(metadata.get("mainManifestRaw") or "")
uefa_raw = str(metadata.get("uefaManifestRaw") or "")
if not main_raw or not uefa_raw:
    raise SystemExit("Seed manifest is missing raw canonical manifests")

def git_text(path: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(workspace), "show", f"{seed_commit}:{path}"],
        text=True,
    )

support_values = {
    "uefa_support_history_json": git_text("data/statmaker/uefa_support_history.json"),
    "uefa_team_support_history_json": git_text("data/statmaker/uefa_team_support_history.json"),
    "uefa_domestic_team_aliases_json": git_text("mappings/domestic_team_aliases.json"),
}
greek_path = "data/api_football/fixture_stats/greece/super-league/2025/fixture_stats.json"
try:
    support_values["uefa_greece_super_league_2025_json"] = git_text(greek_path)
except subprocess.CalledProcessError:
    support_values["uefa_greece_super_league_2025_json"] = '{"fixtures":[]}'

def write_prefs(path: Path, values: dict[str, str]) -> None:
    body = "\n".join(
        f'    <string name="{html.escape(key, quote=True)}">{html.escape(value)}</string>'
        for key, value in values.items()
    )
    path.write_text(
        "<?xml version='1.0' encoding='utf-8' standalone='yes' ?>\n"
        "<map>\n" + body + "\n</map>\n",
        encoding="utf-8",
    )

prefs_root = seed_root / "prefs"
prefs_root.mkdir(parents=True, exist_ok=True)
write_prefs(
    prefs_root / "statmaker_app_ready_artifacts.xml",
    {"betting_bundle_sha256": str(betting.get("sha256") or "seed")},
)
write_prefs(
    prefs_root / "statmaker_data_manifests.xml",
    {
        "main_manifest_raw": main_raw,
        "uefa_manifest_raw": uefa_raw,
    },
)
write_prefs(
    prefs_root / "statmaker_uefa_support_history.xml",
    support_values,
)

print(
    "APP_READY_SEED_PREPARED",
    f"commit={seed_commit}",
    f"db_bytes={required[0].stat().st_size}",
    f"betting_sha={str(betting.get('sha256') or '')[:12]}",
)
PY

  adb shell run-as "$APP_ID" mkdir -p databases files/app_ready_odds shared_prefs

  adb push "$SEED_ROOT/bundle/databases/statmaker_prepared_betting.db" /data/local/tmp/statmaker_prepared_betting.db >/dev/null
  adb shell run-as "$APP_ID" cp /data/local/tmp/statmaker_prepared_betting.db databases/statmaker_prepared_betting.db
  adb shell rm -f /data/local/tmp/statmaker_prepared_betting.db

  for name in domestic champions_league europa_league conference_league; do
    adb push "$SEED_ROOT/bundle/files/app_ready_odds/$name.json" "/data/local/tmp/$name.json" >/dev/null
    adb shell run-as "$APP_ID" cp "/data/local/tmp/$name.json" "files/app_ready_odds/$name.json"
    adb shell rm -f "/data/local/tmp/$name.json"
  done

  for pref in statmaker_app_ready_artifacts statmaker_data_manifests statmaker_uefa_support_history; do
    adb push "$SEED_ROOT/prefs/$pref.xml" "/data/local/tmp/$pref.xml" >/dev/null
    adb shell run-as "$APP_ID" cp "/data/local/tmp/$pref.xml" "shared_prefs/$pref.xml"
    adb shell rm -f "/data/local/tmp/$pref.xml"
  done

  echo "APP_READY_SEED_INSTALLED"
elif [[ "$CHECKPOINT_RESTORED" -eq 0 ]]; then
  echo "APP_READY_SEED_UNAVAILABLE rebuilding from scratch"
fi

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

checkpoint_root="$GITHUB_WORKSPACE/app-ready-checkpoint"
rm -rf "$checkpoint_root"

export_checkpoint() {
  local target="$checkpoint_root"
  rm -rf "$target"
  mkdir -p "$target/databases" "$target/files/statmaker_stats_snapshots" "$target/files/app_ready_odds" "$target/shared_prefs"

  adb shell am force-stop "$APP_ID"
  sleep 2

  adb exec-out run-as "$APP_ID" cat databases/statmaker.db > "$target/databases/statmaker.db"
  adb exec-out run-as "$APP_ID" cat databases/statmaker_prepared_betting.db > "$target/databases/statmaker_prepared_betting.db"
  adb exec-out run-as "$APP_ID" cat files/domestic_normalized_stats_v2.bin > "$target/files/domestic_normalized_stats_v2.bin"
  for competition in champions_league europa_league conference_league; do
    adb exec-out run-as "$APP_ID" cat "files/statmaker_stats_snapshots/$competition.bin" > "$target/files/statmaker_stats_snapshots/$competition.bin"
  done

  cp "$GITHUB_WORKSPACE/odds/odds_api_io/domestic_odds.json" "$target/files/app_ready_odds/domestic.json"
  cp "$u/odds/odds_api_io/champions_league_odds.json" "$target/files/app_ready_odds/champions_league.json"
  cp "$u/odds/odds_api_io/europa_league_odds.json" "$target/files/app_ready_odds/europa_league.json"
  cp "$u/odds/odds_api_io/conference_league_odds.json" "$target/files/app_ready_odds/conference_league.json"

  for pref in statmaker_prepared_data_versions statmaker_data_manifests statmaker_uefa_support_history statmaker_app_ready_artifacts; do
    adb exec-out run-as "$APP_ID" cat "shared_prefs/$pref.xml" > "$target/shared_prefs/$pref.xml"
  done

  python3 - "$GITHUB_WORKSPACE" "$u" "$target" <<'PY'
import hashlib, json, sqlite3, sys
from datetime import datetime, timezone
from pathlib import Path
workspace=Path(sys.argv[1]); uefa_root=Path(sys.argv[2]); root=Path(sys.argv[3])
db_path=root/"databases/statmaker_prepared_betting.db"
con=sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
try:
    quick=con.execute("PRAGMA quick_check").fetchone()
    if not quick or quick[0]!="ok": raise SystemExit(f"Checkpoint quick_check failed: {quick}")
    ready=con.execute("""
      SELECT competition_id,snapshot_version,match_count,selection_count
      FROM prepared_snapshot_meta WHERE state='ready' ORDER BY competition_id
    """).fetchall()
finally:
    con.close()
expected={"domestic","champions_league","europa_league","conference_league"}
if {str(r[0]) for r in ready} != expected:
    raise SystemExit(f"Checkpoint requires exact 4/4 READY snapshots; got {[r[0] for r in ready]}")
main=json.loads((workspace/"data/statmaker/update_manifest.json").read_text(encoding="utf-8"))
uefa=json.loads((uefa_root/"data/statmaker/uefa_update_manifest.json").read_text(encoding="utf-8"))
digest=hashlib.sha256(db_path.read_bytes()).hexdigest()
payload={
 "schemaVersion":1,
 "createdAt":datetime.now(timezone.utc).isoformat().replace("+00:00","Z"),
 "mainContentVersion":main.get("contentVersion",""),
 "uefaContentVersion":uefa.get("contentVersion",""),
 "preparedReadyCount":len(ready),
 "preparedDbSha256":digest,
 "preparedSnapshots":[
   {"competitionId":str(r[0]),"snapshotVersion":str(r[1]),"matchCount":int(r[2]),"selectionCount":int(r[3])}
   for r in ready
 ],
}
(root/"checkpoint.json").write_text(json.dumps(payload,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
print("APP_READY_CHECKPOINT_EXPORTED",f"db_bytes={db_path.stat().st_size}",f"db_sha={digest[:12]}",
      "snapshots="+",".join(f"{r[0]}:{r[2]}/{r[3]}" for r in ready))
PY

  for name in domestic champions_league europa_league conference_league; do
    adb push "$target/files/app_ready_odds/$name.json" "/data/local/tmp/$name.json" >/dev/null
    adb shell run-as "$APP_ID" mkdir -p files/app_ready_odds
    adb shell run-as "$APP_ID" cp "/data/local/tmp/$name.json" "files/app_ready_odds/$name.json"
    adb shell rm -f "/data/local/tmp/$name.json"
  done
}

app_cpu_ticks() {
  local pid stat
  pid="$(adb shell pidof "$APP_ID" 2>/dev/null | tr -d '\r' | awk '{print $1}')"
  [[ -n "$pid" ]] || return 1
  stat="$(adb shell run-as "$APP_ID" cat "/proc/$pid/stat" 2>/dev/null | tr -d '\r')" || return 1
  awk '{print $14 + $15}' <<<"$stat"
}
monitor_phase() {
  local phase="$1"
  local require_prepared="$2"
  local start_epoch="$(date +%s)"
  local last_progress_epoch="$start_epoch"
  local last_stage=""
  local max_total_seconds=3300
  local max_idle_seconds=600
  local last_cpu_ticks=""
  local last_heartbeat_epoch="$start_epoch"
  if [[ "$phase" == "recommendations" && "${APP_READY_RECOMMENDATION_ONLY:-false}" == "true" ]]; then
    max_total_seconds=900
    max_idle_seconds=300
  fi

  for _ in $(seq 1 540); do
    if ! server_healthy; then
      echo "App-ready HTTP server became unhealthy during phase=$phase" >&2
      return 1
    fi
    appready_logs="$(adb logcat -d -s StatMakerAppReady:V "*:S" || true)"
    if grep -q " E StatMakerAppReady:" <<<"$appready_logs"; then
      echo "App-ready producer reported a data task error during phase=$phase" >&2
      printf '%s\n' "$appready_logs" >&2
      return 1
    fi
    stage="$(grep "stage=" <<<"$appready_logs" | tail -1 || true)"
    if [[ -n "$stage" && "$stage" != "$last_stage" ]]; then
      echo "$stage"
      last_stage="$stage"
      last_progress_epoch="$(date +%s)"
    fi

    if [[ "$require_prepared" == "true" ]]; then
      prepared_line="$(grep "stage=prepared_complete ready=" <<<"$appready_logs" | tail -1 || true)"
      if [[ "$prepared_line" =~ stage=prepared_complete[[:space:]]ready=4[[:space:]]requested=4 ]]; then
        echo "APP_READY_PREPARED_PHASE_COMPLETE"
        return 0
      fi
    else
      recommendations_line="$(grep "stage=recommendations_complete generation=" <<<"$appready_logs" | tail -1 || true)"
      if [[ "$recommendations_line" =~ stage=recommendations_complete[[:space:]]generation=([0-9a-f]{64})[[:space:]]candidates=([0-9]+)[[:space:]]reused=(true|false)[[:space:]]elapsedMs=([0-9]+) ]]; then
        if (( BASH_REMATCH[2] <= 0 )); then
          echo "Prepared recommendation generation completed with 0 candidates" >&2
          return 1
        fi
        echo "APP_READY_RECOMMENDATION_PHASE_COMPLETE generation=${BASH_REMATCH[1]} candidates=${BASH_REMATCH[2]}"
        return 0
      fi
    fi

    now_epoch="$(date +%s)"
    cpu_ticks="$(app_cpu_ticks || true)"
    if [[ "$cpu_ticks" =~ ^[0-9]+$ ]]; then
      if [[ -n "$last_cpu_ticks" && "$cpu_ticks" != "$last_cpu_ticks" ]]; then
        last_progress_epoch="$now_epoch"
      fi
      last_cpu_ticks="$cpu_ticks"
      if (( now_epoch - last_heartbeat_epoch >= 60 )); then
        echo "APP_READY_PHASE_HEARTBEAT phase=$phase cpuTicks=$cpu_ticks idleSeconds=$((now_epoch - last_progress_epoch)) lastStage=${last_stage:-none}"
        last_heartbeat_epoch="$now_epoch"
      fi
    fi
    if (( now_epoch - last_progress_epoch >= max_idle_seconds )); then
      echo "App-ready phase=$phase made no stage progress for ${max_idle_seconds}s; last stage: ${last_stage:-none}" >&2
      printf '%s\n' "$appready_logs" >&2
      return 1
    fi
    if (( now_epoch - start_epoch >= max_total_seconds )); then
      echo "App-ready phase=$phase exceeded phase ceiling ${max_total_seconds}s; last stage: ${last_stage:-none}" >&2
      printf '%s\n' "$appready_logs" >&2
      return 1
    fi
    sleep 5
  done
  return 1
}

if [[ "${APP_READY_RECOMMENDATION_ONLY:-false}" == "true" ]]; then
  if [[ "$CHECKPOINT_RESTORED" -ne 1 ]]; then
    echo "Recommendation-only resume requires a restored checkpoint" >&2
    exit 1
  fi
  echo "APP_READY_RECOMMENDATION_ONLY_BEGIN"
else
  # Phase 1: expensive immutable source preparation.
  adb logcat -c
  adb shell am start -W -n "$APP_ID/com.statmaker.app.StatMakerWelcomeActivity"
  monitor_phase "prepared" "true"

  # Freeze and persist 4/4 READY work before final recommendation materialization.
  export_checkpoint

  if [[ "${APP_READY_SOURCE_ONLY:-false}" == "true" ]]; then
    echo "APP_READY_SOURCE_PHASE_ONLY_COMPLETE"
    exit 0
  fi
fi

# Legacy fallback only. Normal publisher runs materialize v10 candidates host-side from the
# frozen checkpoint and therefore never enter this Android recommendation phase.
# Phase 2: run the dedicated publisher-only activity against the restored/current prepared DB.
adb logcat -c
adb shell am start -W -n "$APP_ID/com.statmaker.app.AppReadyPatternPublisherActivity"
monitor_phase "recommendations" "false"

appready_logs="$(adb logcat -d -s StatMakerAppReady:V "*:S" || true)"
printf '%s\n' "$appready_logs"

prepared_complete_line="$(grep "stage=prepared_complete ready=" <<<"$appready_logs" | tail -1 || true)"
if [[ ! "$prepared_complete_line" =~ stage=prepared_complete[[:space:]]ready=([0-9]+)[[:space:]]requested=([0-9]+) ]]; then
  echo "Resumed app-ready producer missing valid prepared_complete summary" >&2
  exit 1
fi
prepared_ready="${BASH_REMATCH[1]}"
prepared_requested="${BASH_REMATCH[2]}"
if (( prepared_requested != 4 || prepared_ready != 4 )); then
  echo "Resumed app-ready producer must reuse 4/4 snapshots; got ${prepared_ready}/${prepared_requested}" >&2
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
            if user_version < 11:
                raise SystemExit(f"Prepared DB schema must be >=11; got {user_version}")

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
                    "Prepared DB missing v11 recommendation tables: " + ", ".join(missing_tables)
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
                    "Prepared DB missing v11 recommendation indexes: " + ", ".join(missing_indexes)
                )

            selection_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(prepared_selections)").fetchall()
            }
            required_v11_columns = {
                "opponent_adjusted_required",
                "opponent_model_probability",
                "opponent_base_model_probability",
                "opponent_without_favorite_probability",
                "opponent_without_xg_probability",
                "opponent_without_fatigue_probability",
                "opponent_without_injuries_probability",
                "opponent_without_lineup_probability",
                "opponent_without_formation_probability",
                "opponent_without_squad_turnover_probability",
                "opponent_modifier_profile",
            }
            missing_v11_columns = sorted(required_v11_columns - selection_columns)
            if missing_v11_columns:
                raise SystemExit(
                    "Prepared DB missing v11 performance/shadow columns: " + ", ".join(missing_v11_columns)
                )
            domestic_context = connection.execute(
                """
                SELECT
                    SUM(CASE WHEN opponent_adjusted_required=1 THEN 1 ELSE 0 END),
                    SUM(CASE WHEN opponent_model_probability IS NOT NULL THEN 1 ELSE 0 END),
                    SUM(CASE WHEN opponent_without_favorite_probability IS NOT NULL THEN 1 ELSE 0 END)
                FROM prepared_selections s
                JOIN prepared_snapshot_meta m
                  ON m.competition_id=s.competition_id AND m.snapshot_version=s.snapshot_version
                WHERE s.competition_id='domestic' AND m.state='ready' AND s.qualifies_pattern=1
                """
            ).fetchone()
            required_context = int(domestic_context[0] or 0)
            opponent_models = int(domestic_context[1] or 0)
            favorite_shadow = int(domestic_context[2] or 0)
            if required_context > 0 and opponent_models <= 0:
                raise SystemExit("Prepared v11 Domestic snapshot has required opponent context but no model probabilities")
            if opponent_models > 0 and favorite_shadow <= 0:
                raise SystemExit("Prepared v11 Domestic snapshot has opponent models but no Favorite shadow")

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
            if rules_fingerprint != "pattern-policy-v2-final-read-model-v5-performance-shadow-v1":
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
                f"opponent_models={opponent_models}",
                f"favorite_shadow={favorite_shadow}",
            )
    finally:
        connection.close()
    print(f"APP_READY_SQLITE_EXPORT_OK path={path} bytes={path.stat().st_size}")
PY
