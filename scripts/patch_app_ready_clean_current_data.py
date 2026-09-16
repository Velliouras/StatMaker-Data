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
exact = (
    complete_for_target
    and bool(engine_contract)
    and checkpoint.get("mainContentVersion") == main.get("contentVersion")
    and checkpoint.get("uefaContentVersion") == uefa.get("contentVersion")
    and checkpoint.get("engineContract") == engine_contract
)'''
if text.count(old_exact) != 1:
    raise SystemExit("Could not locate checkpoint exact-match contract")
text = text.replace(old_exact, new_exact, 1)

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

# Stamp exported checkpoints with the exact engine contract so future retries
# can prove compatibility before restoring any DB state.
export_import = "import hashlib, json, sqlite3, sys\n"
if text.count(export_import) != 1:
    raise SystemExit("Could not locate checkpoint export import")
text = text.replace(export_import, "import hashlib, json, os, sqlite3, sys\n", 1)

payload_marker = ''' "uefaContentVersion":uefa.get("contentVersion",""),
 "completeForTarget":complete_for_target,'''
payload_replacement = ''' "uefaContentVersion":uefa.get("contentVersion",""),
 "engineContract":os.environ.get("APP_READY_ENGINE_CONTRACT",""),
 "completeForTarget":complete_for_target,'''
if text.count(payload_marker) != 1:
    raise SystemExit("Could not locate checkpoint payload contract")
text = text.replace(payload_marker, payload_replacement, 1)

path.write_text(text, encoding="utf-8")
print("APP_READY_RESUME_CONTRACT_PATCH_OK")
