#!/usr/bin/env bash
set -euo pipefail

: "${GITHUB_WORKSPACE:?GITHUB_WORKSPACE is required}"
PRIVATE_ROOT="${1:-statmaker-private}"
cd "$PRIVATE_ROOT"

# This is the only supported App-Ready producer contract. The temporary checkout
# is pinned to the approved PROD 0.1.5/schema11 source so App-Ready generation uses
# the same approved recommendation fixes as the installed production app. Asian scopes
# and all Asian/Handicap markets are retired from the Data inputs before this producer executes.
UAT_SOURCE="${APP_READY_UAT_SOURCE:-false}"
PREPARED_SCHEMA="11"
if [[ "$UAT_SOURCE" == "true" ]]; then
  PREPARED_SCHEMA="$(python3 - <<'PY'
import re
from pathlib import Path
text=Path("app/src/main/java/com/statmaker/app/PreparedBettingSnapshotStore.kt").read_text(encoding="utf-8")
match=re.search(r'private const val DATABASE_VERSION\s*=\s*(\d+)', text)
if not match:
    raise SystemExit("Could not resolve UAT prepared DB version")
print(match.group(1))
PY
)"
  STATMAKER_COMMIT="$(git rev-parse HEAD)"
  RULES_FINGERPRINT="$(python3 - <<'PY'
import re
from pathlib import Path
text=Path("app/src/main/java/com/statmaker/app/PreparedPatternRecommendationModels.kt").read_text(encoding="utf-8")
match=re.search(r'PREPARED_PATTERN_RULES_FINGERPRINT\s*=\s*"([^"]+)"', text)
if not match:
    raise SystemExit("Could not resolve UAT prepared rules fingerprint")
print(match.group(1))
PY
)"
else
  STATMAKER_COMMIT="5b7483d772a4cafc5715d5434bc3cdcf82cc1959"
  RULES_FINGERPRINT="pattern-policy-v2-final-read-model-v5-performance-shadow-v1"
fi
export APP_READY_STATMAKER_COMMIT="$STATMAKER_COMMIT"
export APP_READY_PATTERN_RULES_FINGERPRINT="$RULES_FINGERPRINT"
export APP_READY_PREPARED_SCHEMA_VERSION="$PREPARED_SCHEMA"
LEGACY_BUILDER_REF="origin/automation/app-ready-v2-bootstrap-20260817"
LEGACY_BUILDER_PATH="app/src/main/java/com/statmaker/app/WelcomeDataUpdater.kt"
LEGACY_BUILDER_BLOB="b329ef56878dc991d797b17f64c4f127c71f6e63"

if ! git rev-parse --verify --quiet "${LEGACY_BUILDER_REF}^{commit}" >/dev/null; then
  git fetch --no-tags origin \
    "automation/app-ready-v2-bootstrap-20260817:refs/remotes/origin/automation/app-ready-v2-bootstrap-20260817"
fi
if ! git rev-parse --verify --quiet "${LEGACY_BUILDER_REF}^{commit}" >/dev/null; then
  echo "Pinned legacy builder ref is unavailable: ${LEGACY_BUILDER_REF}" >&2
  exit 1
fi

actual_builder_blob="$(git rev-parse "${LEGACY_BUILDER_REF}:${LEGACY_BUILDER_PATH}")"
if [[ "$actual_builder_blob" != "$LEGACY_BUILDER_BLOB" ]]; then
  echo "Pinned legacy builder blob mismatch." >&2
  echo "Expected: $LEGACY_BUILDER_BLOB" >&2
  echo "Actual:   $actual_builder_blob" >&2
  exit 1
fi

git show "${LEGACY_BUILDER_REF}:${LEGACY_BUILDER_PATH}" > "$LEGACY_BUILDER_PATH"
python "$GITHUB_WORKSPACE/scripts/patch_app_ready_producer.py"
python "$GITHUB_WORKSPACE/scripts/patch_prepared_publisher_diagnostics.py"

models="app/src/main/java/com/statmaker/app/PreparedPatternRecommendationModels.kt"
store="app/src/main/java/com/statmaker/app/PreparedBettingSnapshotStore.kt"
grep -Fq "$RULES_FINGERPRINT" "$models"
grep -Fq "private const val DATABASE_VERSION = $PREPARED_SCHEMA" "$store"

{
  echo "APP_READY_STATMAKER_COMMIT=$STATMAKER_COMMIT"
  echo "APP_READY_PATTERN_RULES_FINGERPRINT=$RULES_FINGERPRINT"
  echo "APP_READY_PREPARED_SCHEMA_VERSION=$PREPARED_SCHEMA"
} >> "$GITHUB_ENV"

echo "APP_READY_PRODUCER_STAGED statmaker=$STATMAKER_COMMIT schema=$PREPARED_SCHEMA rules=$RULES_FINGERPRINT retirement=asian+handicap"
