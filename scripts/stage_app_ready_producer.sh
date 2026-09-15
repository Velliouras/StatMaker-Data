#!/usr/bin/env bash
set -euo pipefail

: "${GITHUB_WORKSPACE:?GITHUB_WORKSPACE is required}"
PRIVATE_ROOT="${1:-statmaker-private}"
cd "$PRIVATE_ROOT"

LEGACY_BUILDER_REF="origin/automation/app-ready-v2-bootstrap-20260817"
LEGACY_BUILDER_PATH="app/src/main/java/com/statmaker/app/WelcomeDataUpdater.kt"
LEGACY_BUILDER_BLOB="b329ef56878dc991d797b17f64c4f127c71f6e63"

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
python "$GITHUB_WORKSPACE/scripts/patch_app_ready_producer.py"
python "$GITHUB_WORKSPACE/scripts/patch_prepared_publisher_diagnostics.py"

echo "APP_READY_PRODUCER_STAGED ref=$LEGACY_BUILDER_REF blob=$LEGACY_BUILDER_BLOB"
