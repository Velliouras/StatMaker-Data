#!/usr/bin/env bash
set -euo pipefail

: "${GITHUB_WORKSPACE:?GITHUB_WORKSPACE is required}"
PRIVATE_ROOT="${1:-statmaker-private}"
cd "$PRIVATE_ROOT"

# Product contract:
#   - recommendation engine semantics are pinned bit-for-bit to the exact verified Saturday baseline
#   - Asian countries/leagues and Asian/handicap markets are retired BEFORE engine execution
#     by the Data-repo App-Ready input policy, never by modifying recommendation code.
# Current StatMaker main must never silently alter App-Ready recommendation behavior.
SATURDAY_ENGINE_COMMIT="d06364ab2625815aeafcb48ae93d6a328f7d6ac5"
APP_DIR="app/src/main/java/com/statmaker/app"

ensure_commit() {
  local commit="$1"
  if ! git cat-file -e "${commit}^{commit}" 2>/dev/null; then
    git fetch --no-tags origin "$commit"
  fi
  git cat-file -e "${commit}^{commit}" >/dev/null
}

ensure_commit "$SATURDAY_ENGINE_COMMIT"

# Restore the complete app package from the exact production commit that generated the
# verified Saturday schema-12 App-Ready bundle.
git checkout "$SATURDAY_ENGINE_COMMIT" -- "$APP_DIR"

# Fail closed on ANY recommendation-source delta from Saturday. Product retirement is
# intentionally enforced in canonical inputs before this exact engine sees them.
mapfile -t actual_engine_delta < <(
  git diff --name-only "$SATURDAY_ENGINE_COMMIT" -- "$APP_DIR" | sort
)
if (( ${#actual_engine_delta[@]} != 0 )); then
  echo "Unexpected App-Ready engine drift from exact Saturday baseline." >&2
  printf '  %s\n' "${actual_engine_delta[@]}" >&2
  exit 1
fi

echo "APP_READY_EXACT_SATURDAY_ENGINE_SOURCE_OK baseline=$SATURDAY_ENGINE_COMMIT"

# Data-side recommendation semantics must also remain bit-for-bit identical to the
# verified Saturday publisher. Infrastructure/checkpoint/validation scripts are allowed
# to evolve independently, but these two files define candidate materialization semantics.
DATA_SATURDAY_COMMIT="54e9bd4e28b29a0eb6313f4d16da4c99f27490b9"

# Exact Saturday Data-side App-Ready pipeline. Download the immutable files directly
# from the verified commit and verify their Git blob hashes. This avoids shallow-checkout
# ambiguity and guarantees bit-for-bit rollback semantics.
declare -A SATURDAY_PIPELINE_BLOBS=(
  ["scripts/patch_app_ready_producer.py"]="5afbac5eb556e70ff996070fab22f259c979eca1"
  ["scripts/patch_prepared_publisher_diagnostics.py"]="66e368fcb6c8d6cd5bcbe1b27b44df95f24d5be0"
  ["scripts/patch_prepared_publisher_bulk.py"]="3f6bb21fa2115b18868d746ec052f58bb6fcb40c"
  ["scripts/run_app_ready_emulator.sh"]="e5e5903d60ac2afc074c5b47930ec8aeb697f0c1"
  ["scripts/build_app_ready_from_device.py"]="54d74c42ae3a58c6cf850f9860cf525e63a2e78c"
  ["scripts/materialize_app_ready_pattern_candidates.py"]="530f0ffb7a364121f13c1be2d4d02b6833f47269"
  ["scripts/materialize_prepared_fixture_index.py"]="5516b8d09bf91a2033feaa1133b90ef443d3c476"
  ["scripts/validate_domestic_cache_provider_identity.py"]="4d572254d9f6c315baeaf11c2ab113866d6a986f"
  ["scripts/app_ready_v10/AppReadyPatternPublisherBridge.kt"]="2e1a2d1d5804772ed2788d756d3dff6017a3848c"
)

for file in "${!SATURDAY_PIPELINE_BLOBS[@]}"; do
  mkdir -p "$GITHUB_WORKSPACE/$(dirname "$file")"
  raw_url="https://raw.githubusercontent.com/Velliouras/StatMaker-Data/$DATA_SATURDAY_COMMIT/$file"
  curl --fail --silent --show-error --location "$raw_url" --output "$GITHUB_WORKSPACE/$file"
  expected_blob="${SATURDAY_PIPELINE_BLOBS[$file]}"
  actual_blob="$(git -C "$GITHUB_WORKSPACE" hash-object "$GITHUB_WORKSPACE/$file")"
  if [[ "$actual_blob" != "$expected_blob" ]]; then
    echo "Saturday pipeline materialization mismatch: $file" >&2
    echo "Expected: $expected_blob" >&2
    echo "Actual:   $actual_blob" >&2
    exit 1
  fi
done

echo "APP_READY_EXACT_SATURDAY_DATA_PIPELINE_OK baseline=$DATA_SATURDAY_COMMIT files=${#SATURDAY_PIPELINE_BLOBS[@]}"

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
if [[ "${ACTUAL_BLOB}" != "${LEGACY_BUILDER_BLOB}" ]]; then
  echo "Pinned legacy builder blob mismatch." >&2
  echo "Expected: ${LEGACY_BUILDER_BLOB}" >&2
  echo "Actual:   ${ACTUAL_BLOB}" >&2
  exit 1
fi

git show "${LEGACY_BUILDER_REF}:${LEGACY_BUILDER_PATH}" > "${LEGACY_BUILDER_PATH}"
python "${GITHUB_WORKSPACE}/scripts/patch_app_ready_producer.py"
python "${GITHUB_WORKSPACE}/scripts/patch_prepared_publisher_diagnostics.py"
python "${GITHUB_WORKSPACE}/scripts/patch_app_ready_clean_current_data.py"

echo "APP_READY_PRODUCER_STAGED saturday_engine=$SATURDAY_ENGINE_COMMIT input_retirement=asian+handicap legacy_ref=$LEGACY_BUILDER_REF"
