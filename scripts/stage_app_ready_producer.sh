#!/usr/bin/env bash
set -euo pipefail

: "${GITHUB_WORKSPACE:?GITHUB_WORKSPACE is required}"
PRIVATE_ROOT="${1:-statmaker-private}"
cd "$PRIVATE_ROOT"

# Product contract:
#   - recommendation semantics are pinned to the verified pre-v6 Saturday producer
#   - Asian countries/leagues and Asian/handicap markets are retired before engine execution
#     by the Data-repo input policy, never by modifying the pinned recommendation code.
SATURDAY_ENGINE_COMMIT="561e152bc8302bb8240131cefc65b5350522c180"
EXPECTED_RULES_FINGERPRINT="pattern-policy-v2-final-read-model-v5-performance-shadow-v1"
APP_DIR="app/src/main/java/com/statmaker/app"

ensure_commit() {
  local commit="$1"
  if ! git cat-file -e "${commit}^{commit}" 2>/dev/null; then
    git fetch --no-tags origin "$commit"
  fi
  git cat-file -e "${commit}^{commit}" >/dev/null
}

ensure_commit "$SATURDAY_ENGINE_COMMIT"

# Restore the complete app package from the exact StatMaker commit recorded by the
# verified 2026-09-12 schema-11/v5 App-Ready manifest. Modern application UI stays
# on main; only this off-device producer checkout is pinned.
git checkout "$SATURDAY_ENGINE_COMMIT" -- "$APP_DIR"

# Fail closed on any recommendation-source delta from the verified producer source.
mapfile -t actual_engine_delta < <(
  git diff --name-only "$SATURDAY_ENGINE_COMMIT" -- "$APP_DIR" | sort
)
if (( ${#actual_engine_delta[@]} != 0 )); then
  echo "Unexpected App-Ready engine drift from verified pre-v6 Saturday baseline." >&2
  printf '  %s\n' "${actual_engine_delta[@]}" >&2
  exit 1
fi

echo "APP_READY_EXACT_SATURDAY_ENGINE_SOURCE_OK baseline=$SATURDAY_ENGINE_COMMIT"

# Cache the same four constant ICU regex patterns in the temporary producer checkout.
# Matching semantics and call order remain unchanged; this is only a native-allocation
# reliability patch for the larger current canonical input set.
NORMALIZER_FILE="$APP_DIR/RepositoryBackedCompetitionBettingProvider.kt"
python - "$NORMALIZER_FILE" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
anchor = '''    private val providerLocationSuffixes = setOf(
        "athens", "istanbul", "amsterdam", "dublin", "belgrade", "thessaloniki", "piraeus"
    )
'''
replacement = anchor + '''    private val combiningMarksRegex = Regex("\\\\p{Mn}+")
    private val olympiakosRegex = Regex("\\\\bolympiakos\\\\b")
    private val nonIdentityCharacterRegex = Regex("[^a-z0-9]+")
    private val whitespaceRegex = Regex("\\\\s+")
'''
if text.count(anchor) != 1:
    raise SystemExit("Could not locate Saturday identity normalizer anchor")
text = text.replace(anchor, replacement, 1)
replacements = {
    '.replace(Regex("\\\\p{Mn}+"), "")': '.replace(combiningMarksRegex, "")',
    '.replace(Regex("\\\\bolympiakos\\\\b"), "olympiacos")': '.replace(olympiakosRegex, "olympiacos")',
    '.replace(Regex("[^a-z0-9]+"), " ")': '.replace(nonIdentityCharacterRegex, " ")',
    'ascii.split(Regex("\\\\s+"))': 'ascii.split(whitespaceRegex)',
}
for old, new in replacements.items():
    if text.count(old) != 1:
        raise SystemExit(f"Could not locate exact Saturday regex expression: {old}")
    text = text.replace(old, new, 1)
path.write_text(text, encoding="utf-8")
print("APP_READY_IDENTITY_REGEX_CACHE_PATCH_OK patterns=4 semantics=unchanged")
PY

# Pin the Data-side files that actually produced the verified schema-11/v5 snapshot.
# Current checkpoint/publish infrastructure stays in place; recommendation construction
# is restored from the immutable historical Data commit.
DATA_SATURDAY_COMMIT="17fa84485df5e1f46d9a35c919b1a33255a69961"
declare -A SATURDAY_PIPELINE_BLOBS=(
  ["scripts/patch_app_ready_producer.py"]="8ea67c8d26b1ffce0437ff3c17642ddec13ebd03"
  ["scripts/patch_prepared_publisher_diagnostics.py"]="66e368fcb6c8d6cd5bcbe1b27b44df95f24d5be0"
  ["scripts/patch_prepared_publisher_bulk.py"]="3f6bb21fa2115b18868d746ec052f58bb6fcb40c"
  ["scripts/run_app_ready_emulator.sh"]="2f2c58cab6a6603d3757f7213ae95f6a1b76f289"
  ["scripts/build_app_ready_from_device.py"]="f26932a59ef8b15133d6bee89077e9b39edc94ee"
  ["scripts/materialize_app_ready_pattern_candidates.py"]="8e327185cfd84fb91df5b85a923341b3fe9cd1ef"
  ["scripts/materialize_prepared_fixture_index.py"]="5516b8d09bf91a2033feaa1133b90ef443d3c476"
  ["scripts/validate_domestic_cache_provider_identity.py"]="4d572254d9f6c315baeaf11c2ab113866d6a986f"
  ["scripts/app_ready_v10/AppReadyPatternPublisherBridge.kt"]="26577cc624f83dc9bd851166ebd8e40fe62c8c86"
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

# Keep the modern aggregate/checkpoint preflight implementation, but bind its product
# contract to the exact v5/schema-11 producer staged above. This file is changed only in
# the runner workspace; the repository copy remains the modern infrastructure source.
PREFLIGHT_FILE="$GITHUB_WORKSPACE/scripts/preflight_app_ready_pipeline.py"
python - "$PREFLIGHT_FILE" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
replacements = {
    "SATURDAY_RULES='pattern-policy-v2-final-read-model-v6-probability-parity-v1'":
        "SATURDAY_RULES='pattern-policy-v2-final-read-model-v5-performance-shadow-v1'",
    "PREPARED_SCHEMA=12": "PREPARED_SCHEMA=11",
    "d06364ab2625815aeafcb48ae93d6a328f7d6ac5": "561e152bc8302bb8240131cefc65b5350522c180",
    "'scripts/patch_app_ready_producer.py':'5afbac5eb556e70ff996070fab22f259c979eca1'":
        "'scripts/patch_app_ready_producer.py':'8ea67c8d26b1ffce0437ff3c17642ddec13ebd03'",
    "'scripts/run_app_ready_emulator.sh':'e5e5903d60ac2afc074c5b47930ec8aeb697f0c1'":
        "'scripts/run_app_ready_emulator.sh':'2f2c58cab6a6603d3757f7213ae95f6a1b76f289'",
    "'scripts/build_app_ready_from_device.py':'54d74c42ae3a58c6cf850f9860cf525e63a2e78c'":
        "'scripts/build_app_ready_from_device.py':'f26932a59ef8b15133d6bee89077e9b39edc94ee'",
    "'scripts/materialize_app_ready_pattern_candidates.py':'530f0ffb7a364121f13c1be2d4d02b6833f47269'":
        "'scripts/materialize_app_ready_pattern_candidates.py':'8e327185cfd84fb91df5b85a923341b3fe9cd1ef'",
    "'scripts/app_ready_v10/AppReadyPatternPublisherBridge.kt':'2e1a2d1d5804772ed2788d756d3dff6017a3848c'":
        "'scripts/app_ready_v10/AppReadyPatternPublisherBridge.kt':'26577cc624f83dc9bd851166ebd8e40fe62c8c86'",
}
for old, new in replacements.items():
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"Preflight contract patch expected exactly one match for {old!r}, got {count}")
    text = text.replace(old, new, 1)
path.write_text(text, encoding="utf-8")
print("APP_READY_PREFLIGHT_V5_SCHEMA11_CONTRACT_OK")
PY

{
  echo "APP_READY_STATMAKER_COMMIT=$SATURDAY_ENGINE_COMMIT"
  echo "APP_READY_PATTERN_RULES_FINGERPRINT=$EXPECTED_RULES_FINGERPRINT"
} >> "$GITHUB_ENV"

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

echo "APP_READY_PRODUCER_STAGED saturday_engine=$SATURDAY_ENGINE_COMMIT rules=$EXPECTED_RULES_FINGERPRINT input_retirement=asian+handicap legacy_ref=$LEGACY_BUILDER_REF"
