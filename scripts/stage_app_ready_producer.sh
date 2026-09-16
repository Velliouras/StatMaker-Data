#!/usr/bin/env bash
set -euo pipefail

: "${GITHUB_WORKSPACE:?GITHUB_WORKSPACE is required}"
PRIVATE_ROOT="${1:-statmaker-private}"
cd "$PRIVATE_ROOT"

# The Data pipeline must build the exact StatMaker ref selected by the workflow.
# Never replace that checkout with a historical engine snapshot.
EXPECTED_RULES_FINGERPRINT="pattern-policy-v2-final-read-model-v7-result-context-v1"
RULES_FILE="app/src/main/java/com/statmaker/app/PreparedPatternRecommendationModels.kt"
APP_DIR="app/src/main/java/com/statmaker/app"

test -s "$RULES_FILE" || {
  echo "Missing StatMaker recommendation contract: $RULES_FILE" >&2
  exit 1
}

ENGINE_SHA="$(git rev-parse HEAD)"
ENGINE_DIRTY="$(git status --porcelain -- "$APP_DIR")"
if [[ -n "$ENGINE_DIRTY" ]]; then
  echo "Selected StatMaker ref is not clean before App-Ready staging." >&2
  printf '%s\n' "$ENGINE_DIRTY" >&2
  exit 1
fi

ACTUAL_RULES_FINGERPRINT="$(python - "$RULES_FILE" <<'PY'
import re, sys
from pathlib import Path
text=Path(sys.argv[1]).read_text(encoding="utf-8")
match=re.search(r'PREPARED_PATTERN_RULES_FINGERPRINT\s*=\s*"([^"]+)"', text)
if not match:
    raise SystemExit("Could not read PREPARED_PATTERN_RULES_FINGERPRINT")
print(match.group(1))
PY
)"

if [[ "$ACTUAL_RULES_FINGERPRINT" != "$EXPECTED_RULES_FINGERPRINT" ]]; then
  echo "Refusing App-Ready staging for incompatible StatMaker recommendation contract." >&2
  echo "Expected: $EXPECTED_RULES_FINGERPRINT" >&2
  echo "Actual:   $ACTUAL_RULES_FINGERPRINT" >&2
  echo "Commit:   $ENGINE_SHA" >&2
  exit 1
fi

{
  echo "APP_READY_STATMAKER_COMMIT=$ENGINE_SHA"
  echo "APP_READY_PATTERN_RULES_FINGERPRINT=$ACTUAL_RULES_FINGERPRINT"
} >> "$GITHUB_ENV"

echo "APP_READY_RESULT_CONTEXT_ENGINE_SOURCE_OK commit=$ENGINE_SHA rules=$ACTUAL_RULES_FINGERPRINT"

# The legacy off-device entry point remains pinned because it is only the harness that
# invokes the current checked-out producer. Recommendation semantics come from the
# selected StatMaker ref above and from the current Data-branch materializer.
LEGACY_BUILDER_REF="origin/automation/app-ready-v2-bootstrap-20260817"
LEGACY_BUILDER_PATH="app/src/main/java/com/statmaker/app/WelcomeDataUpdater.kt"
LEGACY_BUILDER_BLOB="b329ef56878dc991d797b17f64c4f127c71f6e63"

if ! git rev-parse --verify --quiet "${LEGACY_BUILDER_REF}^{commit}" >/dev/null; then
  git fetch --no-tags origin "automation/app-ready-v2-bootstrap-20260817:refs/remotes/origin/automation/app-ready-v2-bootstrap-20260817"
fi
if ! git rev-parse --verify --quiet "${LEGACY_BUILDER_REF}^{commit}" >/dev/null; then
  echo "Pinned legacy builder ref is unavailable in checkout: ${LEGACY_BUILDER_REF}" >&2
  exit 1
fi

ACTUAL_BLOB="$(git rev-parse "${LEGACY_BUILDER_REF}:${LEGACY_BUILDER_PATH}")"
if [[ "$ACTUAL_BLOB" != "$LEGACY_BUILDER_BLOB" ]]; then
  echo "Pinned legacy builder blob mismatch." >&2
  echo "Expected: $LEGACY_BUILDER_BLOB" >&2
  echo "Actual:   $ACTUAL_BLOB" >&2
  exit 1
fi

git show "${LEGACY_BUILDER_REF}:${LEGACY_BUILDER_PATH}" > "$LEGACY_BUILDER_PATH"
python "${GITHUB_WORKSPACE}/scripts/patch_app_ready_producer.py"
python "${GITHUB_WORKSPACE}/scripts/patch_prepared_publisher_diagnostics.py"

echo "APP_READY_PRODUCER_STAGED engine_commit=$ENGINE_SHA rules=$ACTUAL_RULES_FINGERPRINT input_retirement=asian+handicap legacy_ref=$LEGACY_BUILDER_REF"
