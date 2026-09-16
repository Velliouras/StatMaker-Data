#!/usr/bin/env python3
from pathlib import Path

path = Path("scripts/run_app_ready_emulator.sh")
text = path.read_text(encoding="utf-8")

# Same-generation checkpoints are accepted ONLY when both canonical source
# content versions and the exact engine contract match. Old/stale checkpoints
# are ignored rather than used as approximate seeds.
text = text.replace(
    "import json, sys\nfrom pathlib import Path\nworkspace=Path(sys.argv[1]); uefa_root=Path(sys.argv[2])",
    "import json, os, sys\nfrom pathlib import Path\nworkspace=Path(sys.argv[1]); uefa_root=Path(sys.argv[2])",
    1,
)
old_exact = '''exact = (
    complete_for_target
    and checkpoint.get("mainContentVersion") == main.get("contentVersion")
    and checkpoint.get("uefaContentVersion") == uefa.get("contentVersion")
)'''
new_exact = '''engine_contract = os.environ.get("APP_READY_ENGINE_CONTRACT", "")
ready_count = int(checkpoint.get("preparedReadyCount", 0) or 0)
exact = (
    bool(engine_contract)
    and 0 <= ready_count <= 4
    and checkpoint.get("mainContentVersion") == main.get("contentVersion")
    and checkpoint.get("uefaContentVersion") == uefa.get("contentVersion")
    and checkpoint.get("engineContract") == engine_contract
)'''
if text.count(old_exact) != 1:
    raise SystemExit("Could not locate checkpoint exact-match contract")
text = text.replace(old_exact, new_exact, 1)

partial_guard_old = '''if int(checkpoint.get("preparedReadyCount",0)) != 4:
    raise SystemExit(1)
complete_for_target = bool(checkpoint.get("completeForTarget", True))'''
partial_guard_new = '''ready_count = int(checkpoint.get("preparedReadyCount",0) or 0)
if ready_count < 0 or ready_count > 4:
    raise SystemExit(1)
complete_for_target = bool(checkpoint.get("completeForTarget", False))'''
if text.count(partial_guard_old) != 1:
    raise SystemExit("Could not locate checkpoint ready-count guard")
text = text.replace(partial_guard_old, partial_guard_new, 1)


old_checkpoint_tail = '''    "complete_for_target="+str(complete_for_target).lower(),
)
PY'''
new_checkpoint_tail = '''    "complete_for_target="+str(complete_for_target).lower(),
    "engine_contract="+str(checkpoint.get("engineContract","")),
)
raise SystemExit(0 if exact else 1)
PY'''
if text.count(old_checkpoint_tail) < 1:
    raise SystemExit("Could not locate checkpoint validation tail")
text = text.replace(old_checkpoint_tail, new_checkpoint_tail, 1)


restore_required = '''      test -s "$CHECKPOINT_IN/$rel"
      dir="$(dirname "$rel")"
      adb shell run-as "$APP_ID" mkdir -p "$dir"'''
restore_optional = '''      if [[ ! -s "$CHECKPOINT_IN/$rel" ]]; then
        echo "APP_READY_CHECKPOINT_OPTIONAL_MISSING $rel"
        continue
      fi
      dir="$(dirname "$rel")"
      adb shell run-as "$APP_ID" mkdir -p "$dir"'''
if text.count(restore_required) != 1:
    raise SystemExit("Could not locate checkpoint restore file guard")
text = text.replace(restore_required, restore_optional, 1)

# Only use the previous published App-Ready generation as an incremental seed
# when it was produced by this exact engine/input-retirement contract.
seed_marker = '''SEED_COMMIT="$(git -C "$GITHUB_WORKSPACE" log -1 --format=%H -- data/statmaker/app_ready/update_manifest.json || true)"
if [[ "$CHECKPOINT_RESTORED" -eq 0 && -n "$SEED_COMMIT" ]]; then'''
seed_replacement = '''SEED_COMMIT="$(git -C "$GITHUB_WORKSPACE" log -1 --format=%H -- data/statmaker/app_ready/update_manifest.json || true)"
SEED_COMPATIBLE=0
if [[ -n "$SEED_COMMIT" ]]; then
  git -C "$GITHUB_WORKSPACE" show "$SEED_COMMIT:data/statmaker/app_ready/update_manifest.json" > "$SEED_ROOT/update_manifest.json"
  if python3 - "$SEED_ROOT/update_manifest.json" <<'PY'
import json, os, sys
manifest=json.loads(open(sys.argv[1], encoding="utf-8").read())
expected=os.environ.get("APP_READY_ENGINE_CONTRACT", "")
actual=str((manifest.get("metadata") or {}).get("engineContract") or "")
raise SystemExit(0 if expected and actual == expected else 1)
PY
  then
    SEED_COMPATIBLE=1
    echo "APP_READY_COMPATIBLE_PUBLISHED_SEED commit=$SEED_COMMIT contract=$APP_READY_ENGINE_CONTRACT"
  else
    echo "APP_READY_PUBLISHED_SEED_INCOMPATIBLE ignored commit=$SEED_COMMIT"
  fi
fi
if [[ "$CHECKPOINT_RESTORED" -eq 0 && "$SEED_COMPATIBLE" -eq 1 && -n "$SEED_COMMIT" ]]; then'''
if text.count(seed_marker) != 1:
    raise SystemExit("Could not locate published seed guard")
text = text.replace(seed_marker, seed_replacement, 1)


export_files_old = '''  adb exec-out run-as "$APP_ID" cat databases/statmaker.db > "$target/databases/statmaker.db"
  adb exec-out run-as "$APP_ID" cat databases/statmaker_prepared_betting.db > "$target/databases/statmaker_prepared_betting.db"
  adb exec-out run-as "$APP_ID" cat files/domestic_normalized_stats_v2.bin > "$target/files/domestic_normalized_stats_v2.bin"
  for competition in champions_league europa_league conference_league; do
    adb exec-out run-as "$APP_ID" cat "files/statmaker_stats_snapshots/$competition.bin" > "$target/files/statmaker_stats_snapshots/$competition.bin"
  done'''
export_files_new = '''  pull_checkpoint_file() {
    local rel="$1"
    local dst="$2"
    if adb shell run-as "$APP_ID" test -s "$rel"; then
      adb exec-out run-as "$APP_ID" cat "$rel" > "$dst"
    fi
  }

  pull_checkpoint_file databases/statmaker.db "$target/databases/statmaker.db"
  pull_checkpoint_file databases/statmaker_prepared_betting.db "$target/databases/statmaker_prepared_betting.db"
  pull_checkpoint_file files/domestic_normalized_stats_v2.bin "$target/files/domestic_normalized_stats_v2.bin"
  for competition in champions_league europa_league conference_league; do
    pull_checkpoint_file "files/statmaker_stats_snapshots/$competition.bin" "$target/files/statmaker_stats_snapshots/$competition.bin"
  done'''
if text.count(export_files_old) != 1:
    raise SystemExit("Could not locate checkpoint export file block")
text = text.replace(export_files_old, export_files_new, 1)

export_prefs_old = '''  for pref in statmaker_prepared_data_versions statmaker_data_manifests statmaker_uefa_support_history statmaker_app_ready_artifacts; do
    adb exec-out run-as "$APP_ID" cat "shared_prefs/$pref.xml" > "$target/shared_prefs/$pref.xml"
  done'''
export_prefs_new = '''  for pref in statmaker_prepared_data_versions statmaker_data_manifests statmaker_uefa_support_history statmaker_app_ready_artifacts; do
    pull_checkpoint_file "shared_prefs/$pref.xml" "$target/shared_prefs/$pref.xml"
  done'''
if text.count(export_prefs_old) != 1:
    raise SystemExit("Could not locate checkpoint preference export block")
text = text.replace(export_prefs_old, export_prefs_new, 1)

# Stamp exported checkpoints with the exact engine contract so future retries
# can prove compatibility before restoring any DB state.
export_import = "import hashlib, json, sqlite3, sys\n"
if text.count(export_import) != 1:
    raise SystemExit("Could not locate checkpoint export import")
text = text.replace(export_import, "import hashlib, json, os, sqlite3, sys\n", 1)


ready_guard_old = '''expected={"domestic","champions_league","europa_league","conference_league"}
if {str(r[0]) for r in ready} != expected:
    raise SystemExit(f"Checkpoint requires exact 4/4 READY snapshots; got {[r[0] for r in ready]}")'''
ready_guard_new = '''expected={"domestic","champions_league","europa_league","conference_league"}
ready_ids={str(r[0]) for r in ready}
unexpected=ready_ids-expected
if unexpected:
    raise SystemExit(f"Checkpoint contains unexpected READY snapshots: {sorted(unexpected)}")
ready=[r for r in ready if str(r[0]) in expected]'''
if text.count(ready_guard_old) != 1:
    raise SystemExit("Could not locate checkpoint exporter READY guard")
text = text.replace(ready_guard_old, ready_guard_new, 1)

payload_marker = ''' "uefaContentVersion":uefa.get("contentVersion",""),
 "completeForTarget":complete_for_target,'''
payload_replacement = ''' "uefaContentVersion":uefa.get("contentVersion",""),
 "engineContract":os.environ.get("APP_READY_ENGINE_CONTRACT",""),
 "completeForTarget":bool(complete_for_target and len(ready) == 4),'''
if text.count(payload_marker) != 1:
    raise SystemExit("Could not locate checkpoint payload contract")
text = text.replace(payload_marker, payload_replacement, 1)

path.write_text(text, encoding="utf-8")
print("APP_READY_RESUME_CONTRACT_PATCH_OK")
