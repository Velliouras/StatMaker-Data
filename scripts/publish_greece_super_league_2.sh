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
for attempt in 1 2 3 4 5; do
  git fetch --no-tags origin +main:refs/remotes/origin/main
  git checkout --detach refs/remotes/origin/main
  if ! git cherry-pick "$producer_commit"; then
    # A real overlap in owned data must be inspected, never resolved with ours/theirs.
    git cherry-pick --abort
    echo "Super League 2 source data changed concurrently; refusing to overwrite it." >&2
    exit 1
  fi
  python scripts/build_statmaker_update_manifest.py \
    --profile main --branch main --output data/statmaker/update_manifest.json
  git add data/statmaker/update_manifest.json
  git commit --amend --no-edit
  if git push origin HEAD:main; then
    echo "G2_PUBLISH_OK attempt=${attempt}"
    exit 0
  fi
  echo "G2 remote advanced; rebuilding publication without repeating provider calls."
  sleep "$((attempt * 2))"
done
echo "Could not publish Super League 2 artifacts after 5 retries." >&2
exit 1
