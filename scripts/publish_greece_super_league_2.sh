#!/usr/bin/env bash
# Publish only this producer's data, rebuilding derived metadata on each fresh main.
set -euo pipefail
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add \
  data/api_football/fixture_stats/greece/super-league-2/2025/fixture_stats.json \
  data/statmaker/domestic_enriched/greece_super_league_2_2025.json \
  data/statmaker/domestic_enriched/index.json \
  odds/odds_api_io/domestic_odds.json \
  reports/greece_super_league_2_sync.json \
  reports/domestic_odds_integrity.json
if git diff --cached --quiet; then
  echo "No Super League 2 changes."
  exit 0
fi

# Never replay an old manifest: it describes another tree and will conflict with
# unrelated producers. Keep provider output in one immutable commit across retries.
git commit -m "Add Greece Super League 2 schedule and history"
producer_commit="$(git rev-parse HEAD)"
index=data/statmaker/domestic_enriched/index.json
for attempt in 1 2 3 4 5; do
  git fetch --no-tags origin +main:refs/remotes/origin/main
  git checkout --detach refs/remotes/origin/main
  if ! git diff --quiet "${producer_commit}^" "$producer_commit" -- . ":(exclude)$index" &&
     ! git diff --binary "${producer_commit}^" "$producer_commit" -- . ":(exclude)$index" |
       git apply --3way --index; then
    # A real overlap in owned data must be inspected, never resolved with ours/theirs.
    echo "Super League 2 source data changed concurrently; refusing to overwrite it." >&2
    exit 1
  fi
  # The shared index is also written by other leagues. Merge only the G2 row
  # onto the latest index, then build a manifest for the combined tree.
  python - "$producer_commit" "$index" <<'PY'
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

commit, filename = sys.argv[1:]
path = Path(filename)
base = json.loads(subprocess.check_output(["git", "show", f"{commit}^:{filename}"]))
incoming = json.loads(subprocess.check_output(["git", "show", f"{commit}:{filename}"]))
latest = json.loads(path.read_text())

def g2(doc):
    return [r for r in doc["leagues"] if r.get("league_code") == "G2"]

old, new, current = map(g2, (base, incoming, latest))
if old != new:
    if len(new) != 1 or current not in (old, new):
        sys.exit("Super League 2 index row changed concurrently; refusing to overwrite it.")
    if latest == base:
        result = incoming
    elif current != new:
        rows = [r for r in latest["leagues"] if r.get("league_code") != "G2"] + new
        rows.sort(key=lambda r: (str(r.get("country", "")), str(r.get("league", ""))))
        result = {**latest, "leagues": rows, "league_count": len(rows),
                  "generated_at": datetime.now(timezone.utc).isoformat()}
    else:
        result = latest
    if result != latest:
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
PY
  python scripts/build_statmaker_update_manifest.py \
    --profile main --branch main --output data/statmaker/update_manifest.json
  git add "$index" data/statmaker/update_manifest.json
  if git diff --cached --quiet; then
    echo "Super League 2 changes are already published."
    exit 0
  fi
  git commit -m "Add Greece Super League 2 schedule and history"
  if git push origin HEAD:main; then
    echo "G2_PUBLISH_OK attempt=${attempt}"
    exit 0
  fi
  echo "G2 remote advanced; rebuilding publication without repeating provider calls."
  sleep "$((attempt * 2))"
done
echo "Could not publish Super League 2 artifacts after 5 retries." >&2
exit 1
