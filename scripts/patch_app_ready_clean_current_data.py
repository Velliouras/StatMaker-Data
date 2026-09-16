#!/usr/bin/env python3
from pathlib import Path

path = Path("scripts/run_app_ready_emulator.sh")
text = path.read_text(encoding="utf-8")

checkpoint_old = 'if [[ -s "$CHECKPOINT_IN/checkpoint.json" ]]; then'
checkpoint_new = 'if [[ "${APP_READY_CLEAN_CURRENT_DATA_BUILD:-false}" != "true" && -s "$CHECKPOINT_IN/checkpoint.json" ]]; then'
if text.count(checkpoint_old) != 1:
    raise SystemExit("Could not locate App-Ready checkpoint seed guard")
text = text.replace(checkpoint_old, checkpoint_new, 1)

seed_old = 'if [[ "$CHECKPOINT_RESTORED" -eq 0 && -n "$SEED_COMMIT" ]]; then'
seed_new = 'if [[ "${APP_READY_CLEAN_CURRENT_DATA_BUILD:-false}" != "true" && "$CHECKPOINT_RESTORED" -eq 0 && -n "$SEED_COMMIT" ]]; then'
if text.count(seed_old) != 1:
    raise SystemExit("Could not locate App-Ready published seed guard")
text = text.replace(seed_old, seed_new, 1)

unavailable_old = '''elif [[ "$CHECKPOINT_RESTORED" -eq 0 ]]; then
  echo "APP_READY_SEED_UNAVAILABLE rebuilding from scratch"
fi'''
unavailable_new = '''elif [[ "$CHECKPOINT_RESTORED" -eq 0 ]]; then
  if [[ "${APP_READY_CLEAN_CURRENT_DATA_BUILD:-false}" == "true" ]]; then
    echo "APP_READY_CLEAN_CURRENT_DATA_BUILD no checkpoint or published App-Ready seed"
  else
    echo "APP_READY_SEED_UNAVAILABLE rebuilding from scratch"
  fi
fi'''
if text.count(unavailable_old) != 1:
    raise SystemExit("Could not locate App-Ready seed fallback message")
text = text.replace(unavailable_old, unavailable_new, 1)

path.write_text(text, encoding="utf-8")
print("APP_READY_CLEAN_CURRENT_DATA_PATCH_OK")
