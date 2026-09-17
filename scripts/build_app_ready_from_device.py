#!/usr/bin/env python3
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

HISTORICAL_DATA_COMMIT = "17fa84485df5e1f46d9a35c919b1a33255a69961"
EXPECTED_RULES = "pattern-policy-v2-final-read-model-v5-performance-shadow-v1"
EXPECTED_STATMAKER_COMMIT = "561e152bc8302bb8240131cefc65b5350522c180"
ENGINE_CONTRACT = "pre-v6-schema11-v5-performance-shadow-retired-inputs-v1"
RETIREMENT_CONTRACT = "asian-countries-and-all-asian-handicap-markets-before-engine-v1"


def run(*args: str) -> None:
    subprocess.run(list(args), check=True)

# Run the exact builder that produced the known-good Saturday v5/schema11 artifact.
url = f"https://raw.githubusercontent.com/Velliouras/StatMaker-Data/{HISTORICAL_DATA_COMMIT}/scripts/build_app_ready_from_device.py"
with urllib.request.urlopen(url, timeout=60) as response:
    historical = response.read()
with tempfile.NamedTemporaryFile(prefix="statmaker-v5-builder-", suffix=".py", delete=False) as tmp:
    tmp.write(historical)
    tmp_path = Path(tmp.name)
try:
    run(sys.executable, str(tmp_path), *sys.argv[1:])
finally:
    tmp_path.unlink(missing_ok=True)

if len(sys.argv) < 2:
    raise SystemExit("Expected raw export root argument")
raw_root = Path(sys.argv[1])
db = raw_root / "databases/statmaker_prepared_betting.db"
if not db.is_file():
    raise SystemExit(f"Prepared DB missing after build: {db}")

con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
try:
    quick = con.execute("PRAGMA quick_check").fetchone()
    if not quick or quick[0] != "ok":
        raise SystemExit(f"Prepared DB quick_check failed: {quick}")
    version = int(con.execute("PRAGMA user_version").fetchone()[0])
    if version != 11:
        raise SystemExit(f"Expected exact prepared schema 11, got {version}")

    tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "prepared_pattern_candidates" not in tables:
        raise SystemExit("Missing prepared_pattern_candidates")
    candidate_count = int(con.execute("SELECT COUNT(*) FROM prepared_pattern_candidates").fetchone()[0])
    if candidate_count <= 0:
        raise SystemExit("prepared_pattern_candidates is empty")

    if "prepared_pattern_generation" in tables:
        columns = [row[1] for row in con.execute("PRAGMA table_info(prepared_pattern_generation)")]
        if "rules_fingerprint" in columns:
            rules = {
                str(row[0] or "").strip()
                for row in con.execute("SELECT DISTINCT rules_fingerprint FROM prepared_pattern_generation")
                if str(row[0] or "").strip()
            }
            if rules != {EXPECTED_RULES}:
                raise SystemExit(f"Unexpected prepared rules fingerprints: {sorted(rules)}")

    retired_hits = 0
    for table in ("prepared_pattern_candidates", "prepared_selections"):
        if table not in tables:
            continue
        cols = [
            row[1] for row in con.execute(f"PRAGMA table_info({table})")
            if "market" in str(row[1]).lower()
        ]
        if not cols:
            continue
        predicates = []
        for col in cols:
            quoted = '"' + str(col).replace('"', '""') + '"'
            predicates.append(f"UPPER(COALESCE(CAST({quoted} AS TEXT),'')) LIKE '%ASIAN%'")
            predicates.append(f"UPPER(COALESCE(CAST({quoted} AS TEXT),'')) LIKE '%HANDICAP%'")
        retired_hits += int(con.execute(f"SELECT COUNT(*) FROM {table} WHERE " + " OR ".join(predicates)).fetchone()[0])
    if retired_hits:
        raise SystemExit(f"Retired Asian/Handicap markets leaked into prepared DB: {retired_hits}")
finally:
    con.close()

if len(sys.argv) < 3:
    raise SystemExit("Expected immutable output root argument")
manifest_path = Path(sys.argv[2]) / "update_manifest.json"
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
metadata = manifest.setdefault("metadata", {})
metadata["statmakerCommit"] = EXPECTED_STATMAKER_COMMIT
metadata["engineContract"] = ENGINE_CONTRACT
metadata["inputRetirementContract"] = RETIREMENT_CONTRACT
manifest_path.write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)

print("APP_READY_PRE_V6_POSTFLIGHT_OK", f"schema={version}", f"rules={EXPECTED_RULES}", f"candidates={candidate_count}", "retired_markets=0")
