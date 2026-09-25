#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

# Approved PROD 0.1.5/schema11 source. Keep App-Ready generation pinned to the
# exact production release so recommendation evidence cannot silently regress to an older engine.
ENGINE_COMMIT = "5b7483d772a4cafc5715d5434bc3cdcf82cc1959"
HISTORICAL_DATA_COMMIT = "17fa84485df5e1f46d9a35c919b1a33255a69961"
EXPECTED_RULES = "pattern-policy-v2-final-read-model-v5-performance-shadow-v1"
APP_DIR = Path("app/src/main/java/com/statmaker/app")
LEGACY_REF = "origin/automation/app-ready-v2-bootstrap-20260817"
LEGACY_PATH = "app/src/main/java/com/statmaker/app/WelcomeDataUpdater.kt"
LEGACY_BLOB = "b329ef56878dc991d797b17f64c4f127c71f6e63"
UAT_SOURCE = os.environ.get("APP_READY_UAT_SOURCE", "false").lower() == "true"


def run(*args: str) -> None:
    subprocess.run(list(args), check=True)


def output(*args: str) -> str:
    return subprocess.check_output(list(args), text=True).strip()


# Production keeps an exact approved PROD/schema11 engine lock. Controlled UAT publishing is the only
# opt-in mode allowed to preserve the checked-out current source. The production pin must never point
# behind approved recommendation fixes, otherwise App-Ready rows can disagree with the installed app.
if UAT_SOURCE:
    print("APP_READY_UAT_CURRENT_ENGINE_OK", output("git", "rev-parse", "HEAD"))
else:
    run("git", "fetch", "--no-tags", "origin", ENGINE_COMMIT)
    subprocess.run(
        ["git", "rm", "-r", "-f", "--ignore-unmatch", "--", str(APP_DIR)],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    if APP_DIR.exists():
        import shutil
        shutil.rmtree(APP_DIR)
    run("git", "checkout", ENGINE_COMMIT, "--", str(APP_DIR))
    unexpected = output("git", "diff", "--name-only", ENGINE_COMMIT, "--", str(APP_DIR))
    if unexpected:
        raise SystemExit(f"Approved PROD engine checkout is not exact: {unexpected}")

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

# The historical patcher owns the approved schema11 App-Ready mechanics. For a controlled UAT
# source, preserve those mechanics but validate the checked-out UAT store against the explicitly
# staged UAT schema contract instead of forcing its DATABASE_VERSION back to 11.
if UAT_SOURCE:
    expected_uat_schema = os.environ.get("APP_READY_PREPARED_SCHEMA_VERSION", "").strip()
    if not expected_uat_schema.isdigit():
        raise SystemExit("UAT App-Ready staging has no valid prepared schema contract")
    historical_text = historical.decode("utf-8")
    schema_guard = 'and "private const val DATABASE_VERSION = 11" in text'
    schema_replacement = (
        'and f"private const val DATABASE_VERSION = '
        + '{os.environ[\\"APP_READY_PREPARED_SCHEMA_VERSION\\"]}" in text'
    )
    if historical_text.count(schema_guard) != 1:
        raise SystemExit("Could not locate historical UAT prepared-schema guard")
    historical_text = historical_text.replace(schema_guard, schema_replacement, 1)
    historical = historical_text.encode("utf-8")

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

# Fail fast before the expensive emulator step if the selected contract does not match source.
models = (APP_DIR / "PreparedPatternRecommendationModels.kt").read_text(encoding="utf-8")
store = (APP_DIR / "PreparedBettingSnapshotStore.kt").read_text(encoding="utf-8")
expected_rules = (
    os.environ.get("APP_READY_PATTERN_RULES_FINGERPRINT", "").strip()
    if UAT_SOURCE
    else EXPECTED_RULES
)
if not expected_rules or expected_rules not in models:
    raise SystemExit(f"Expected rules fingerprint is missing from producer source: {expected_rules!r}")
if not UAT_SOURCE and "pattern-policy-v2-final-read-model-v6-probability-parity-v1" in models:
    raise SystemExit("v6 rules fingerprint leaked into pre-v6 producer")
expected_schema = (
    os.environ.get("APP_READY_PREPARED_SCHEMA_VERSION", "").strip()
    if UAT_SOURCE
    else "11"
)
if not expected_schema.isdigit():
    raise SystemExit(f"Invalid prepared DB schema contract: {expected_schema!r}")
if f"private const val DATABASE_VERSION = {expected_schema}" not in store:
    raise SystemExit(
        f"Prepared DB schema does not match selected producer contract: expected={expected_schema}"
    )
if not UAT_SOURCE and "private const val DATABASE_VERSION = 12" in store:
    raise SystemExit("schema12 leaked into schema11 PROD producer")

# Regression guard: the approved Seattle Sounders/Hannover direction fix and the two
# approved direct O/U gates must be present in the source that actually builds App-Ready.
market_identity = (APP_DIR / "MarketIdentity.kt").read_text(encoding="utf-8")
pattern_matcher = (APP_DIR / "PatternOddsMatcher.kt").read_text(encoding="utf-8")
value_policy = (APP_DIR / "BettingValueSignalPolicy.kt").read_text(encoding="utf-8")
evidence_score = (APP_DIR / "BettingEvidenceScore.kt").read_text(encoding="utf-8")
if "internal fun marketTotalDirectionToken(value: String): MarketSelectionSide?" not in market_identity:
    raise SystemExit("Approved O/U direction-token regression fix is missing from App-Ready producer")
if "isUnderMarketSelection(selectionText)" not in pattern_matcher:
    raise SystemExit("Approved O/U matcher direction fix is missing from App-Ready producer")
if "internal fun bookmakerMispricingSignal(" not in value_policy:
    raise SystemExit("Approved direct O/U bookmaker-mispricing logic is missing from App-Ready producer")
if "return score.qualifiesForPattern &&" not in evidence_score:
    raise SystemExit("Approved direct O/U Strong quality gate is missing from App-Ready producer")

if UAT_SOURCE:
    print(
        "APP_READY_UAT_ENGINE_SOURCE_OK",
        output("git", "rev-parse", "HEAD"),
        expected_rules,
        f"schema={expected_schema}",
    )
else:
    print("APP_READY_APPROVED_PROD_ENGINE_LOCK_OK", ENGINE_COMMIT, EXPECTED_RULES, "schema=11")
print("APP_READY_IDENTITY_REGEX_CACHE_OK patterns=4 semantics=unchanged")
