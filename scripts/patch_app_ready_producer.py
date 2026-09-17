#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

# Trigger after the pre-v6 validator path fix; recommendation semantics are unchanged.
ENGINE_COMMIT = "561e152bc8302bb8240131cefc65b5350522c180"
HISTORICAL_DATA_COMMIT = "17fa84485df5e1f46d9a35c919b1a33255a69961"
EXPECTED_RULES = "pattern-policy-v2-final-read-model-v5-performance-shadow-v1"
APP_DIR = Path("app/src/main/java/com/statmaker/app")
LEGACY_REF = "origin/automation/app-ready-v2-bootstrap-20260817"
LEGACY_PATH = "app/src/main/java/com/statmaker/app/WelcomeDataUpdater.kt"
LEGACY_BLOB = "b329ef56878dc991d797b17f64c4f127c71f6e63"


def run(*args: str) -> None:
    subprocess.run(list(args), check=True)


def output(*args: str) -> str:
    return subprocess.check_output(list(args), text=True).strip()


# Pin the complete producer package to the exact pre-v6 Saturday source.
run("git", "fetch", "--no-tags", "origin", ENGINE_COMMIT)
run("git", "checkout", ENGINE_COMMIT, "--", str(APP_DIR))
if output("git", "diff", "--name-only", ENGINE_COMMIT, "--", str(APP_DIR)):
    raise SystemExit("Pre-v6 engine checkout is not exact")

# Reapply the immutable off-device builder after the package checkout.
if subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"{LEGACY_REF}^{{commit}}"], stdout=subprocess.DEVNULL).returncode != 0:
    run("git", "fetch", "--no-tags", "origin", "automation/app-ready-v2-bootstrap-20260817:refs/remotes/origin/automation/app-ready-v2-bootstrap-20260817")
actual_legacy_blob = output("git", "rev-parse", f"{LEGACY_REF}:{LEGACY_PATH}")
if actual_legacy_blob != LEGACY_BLOB:
    raise SystemExit(f"Legacy builder blob mismatch: {actual_legacy_blob} != {LEGACY_BLOB}")
Path(LEGACY_PATH).write_bytes(subprocess.check_output(["git", "show", f"{LEGACY_REF}:{LEGACY_PATH}"]))

# Execute the exact historical v5/schema11 producer patch, not the later v6 patch.
url = f"https://raw.githubusercontent.com/Velliouras/StatMaker-Data/{HISTORICAL_DATA_COMMIT}/scripts/patch_app_ready_producer.py"
with urllib.request.urlopen(url, timeout=60) as response:
    historical = response.read()
with tempfile.NamedTemporaryFile(prefix="statmaker-v5-producer-", suffix=".py", delete=False) as tmp:
    tmp.write(historical)
    tmp_path = Path(tmp.name)
try:
    run(sys.executable, str(tmp_path))
finally:
    tmp_path.unlink(missing_ok=True)

# Reliability-only delta: cache four constant ICU regex objects once.
# This changes allocation behaviour only; regex text, ordering and matching semantics remain identical.
normalizer = APP_DIR / "RepositoryBackedCompetitionBettingProvider.kt"
text = normalizer.read_text(encoding="utf-8")
anchor = '''    private val providerLocationSuffixes = setOf(
        "athens", "istanbul", "amsterdam", "dublin", "belgrade", "thessaloniki", "piraeus"
    )
'''
if "private val combiningMarksRegex" not in text:
    replacement = anchor + '''    private val combiningMarksRegex = Regex("\\\\p{Mn}+")
    private val olympiakosRegex = Regex("\\\\bolympiakos\\\\b")
    private val nonIdentityCharacterRegex = Regex("[^a-z0-9]+")
    private val whitespaceRegex = Regex("\\\\s+")
'''
    if text.count(anchor) != 1:
        raise SystemExit("Could not locate identity normalizer anchor")
    text = text.replace(anchor, replacement, 1)
    replacements = {
        '.replace(Regex("\\\\p{Mn}+"), "")': '.replace(combiningMarksRegex, "")',
        '.replace(Regex("\\\\bolympiakos\\\\b"), "olympiacos")': '.replace(olympiakosRegex, "olympiacos")',
        '.replace(Regex("[^a-z0-9]+"), " ")': '.replace(nonIdentityCharacterRegex, " ")',
        'ascii.split(Regex("\\\\s+"))': 'ascii.split(whitespaceRegex)',
    }
    for old, new in replacements.items():
        if text.count(old) != 1:
            raise SystemExit(f"Could not locate exact regex expression: {old}")
        text = text.replace(old, new, 1)
    normalizer.write_text(text, encoding="utf-8")

# Fail fast before the expensive emulator step if any v6/schema12 recommendation contract leaked in.
models = (APP_DIR / "PreparedPatternRecommendationModels.kt").read_text(encoding="utf-8")
store = (APP_DIR / "PreparedBettingSnapshotStore.kt").read_text(encoding="utf-8")
if EXPECTED_RULES not in models:
    raise SystemExit("Expected v5 rules fingerprint is missing from producer source")
if "pattern-policy-v2-final-read-model-v6-probability-parity-v1" in models:
    raise SystemExit("v6 rules fingerprint leaked into pre-v6 producer")
if "private const val DATABASE_VERSION = 11" not in store:
    raise SystemExit("Prepared DB schema is not v11")
if "private const val DATABASE_VERSION = 12" in store:
    raise SystemExit("schema12 leaked into pre-v6 producer")

print("APP_READY_PRE_V6_ENGINE_LOCK_OK", ENGINE_COMMIT, EXPECTED_RULES, "schema=11")
print("APP_READY_IDENTITY_REGEX_CACHE_OK patterns=4 semantics=unchanged")
