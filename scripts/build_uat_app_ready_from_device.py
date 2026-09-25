#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

HISTORICAL_DATA_COMMIT = "17fa84485df5e1f46d9a35c919b1a33255a69961"
OLD_RULES = "pattern-policy-v2-final-read-model-v5-performance-shadow-v1"
EXPECTED_RULES = os.environ["APP_READY_PATTERN_RULES_FINGERPRINT"].strip()
EXPECTED_STATMAKER_COMMIT = os.environ["APP_READY_STATMAKER_COMMIT"].strip()
EXPECTED_SCHEMA = int(os.environ["APP_READY_PREPARED_SCHEMA_VERSION"])
PUBLISH_SUBDIR = os.environ.get(
    "APP_READY_PUBLISH_SUBDIR",
    "data/statmaker/app_ready_uat/probability-first-v1",
).strip("/")
RAW_BASE = f"https://raw.githubusercontent.com/Velliouras/StatMaker-Data/main/{PUBLISH_SUBDIR}/"

if not EXPECTED_RULES or not EXPECTED_STATMAKER_COMMIT:
    raise SystemExit("Missing UAT app-ready contract environment")

url = (
    f"https://raw.githubusercontent.com/Velliouras/StatMaker-Data/"
    f"{HISTORICAL_DATA_COMMIT}/scripts/build_app_ready_from_device.py"
)
with urllib.request.urlopen(url, timeout=60) as response:
    historical = response.read().decode("utf-8")

needle = f'PREPARED_PATTERN_RULES_FINGERPRINT = "{OLD_RULES}"'
replacement = f'PREPARED_PATTERN_RULES_FINGERPRINT = "{EXPECTED_RULES}"'
if historical.count(needle) != 1:
    raise SystemExit("Historical builder rules anchor changed")
historical = historical.replace(needle, replacement, 1)

with tempfile.NamedTemporaryFile(
    prefix="statmaker-uat-builder-",
    suffix=".py",
    mode="w",
    encoding="utf-8",
    delete=False,
) as tmp:
    tmp.write(historical)
    tmp_path = Path(tmp.name)
try:
    subprocess.run([sys.executable, str(tmp_path), *sys.argv[1:]], check=True)
finally:
    tmp_path.unlink(missing_ok=True)

if len(sys.argv) < 3:
    raise SystemExit("Expected raw and output roots")
raw_root = Path(sys.argv[1])
out_root = Path(sys.argv[2])
db_path = raw_root / "databases/statmaker_prepared_betting.db"
manifest_path = out_root / "update_manifest.json"

if not db_path.is_file() or not manifest_path.is_file():
    raise SystemExit("UAT app-ready build output is incomplete")

con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
try:
    quick = con.execute("PRAGMA quick_check").fetchone()
    if not quick or quick[0] != "ok":
        raise SystemExit(f"Prepared DB quick_check failed: {quick}")
    version = int(con.execute("PRAGMA user_version").fetchone()[0])
    if version != EXPECTED_SCHEMA:
        raise SystemExit(f"Expected prepared schema {EXPECTED_SCHEMA}, got {version}")

    fixture_tables = {
        str(row[0])
        for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('prepared_fixture_matches','prepared_fixture_markets')"
        )
    }
    required_fixture_tables = {'prepared_fixture_matches','prepared_fixture_markets'}
    missing_fixture_tables = required_fixture_tables - fixture_tables
    if missing_fixture_tables:
        raise SystemExit(
            "UAT prepared fixture read model is missing: "
            + ",".join(sorted(missing_fixture_tables))
        )
    fixture_match_count = int(
        con.execute("SELECT COUNT(*) FROM prepared_fixture_matches").fetchone()[0]
    )
    fixture_market_count = int(
        con.execute("SELECT COUNT(*) FROM prepared_fixture_markets").fetchone()[0]
    )
    if fixture_match_count <= 0 or fixture_market_count <= 0:
        raise SystemExit(
            "UAT prepared fixture read model is empty: "
            f"matches={fixture_match_count} markets={fixture_market_count}"
        )

    rules = {
        str(row[0] or "").strip()
        for row in con.execute(
            "SELECT DISTINCT rules_fingerprint FROM prepared_pattern_generation WHERE state='ready'"
        )
        if str(row[0] or "").strip()
    }
    if rules != {EXPECTED_RULES}:
        raise SystemExit(f"Unexpected UAT prepared rules: {sorted(rules)}")

    candidate_count = int(
        con.execute(
            """
            SELECT COUNT(*)
            FROM prepared_pattern_candidates c
            JOIN prepared_pattern_generation g
              ON g.generation_id=c.generation_id
            WHERE g.state='ready'
            """
        ).fetchone()[0]
    )
    expanded_count = int(
        con.execute(
            """
            SELECT COUNT(*)
            FROM prepared_pattern_candidates c
            JOIN prepared_pattern_generation g
              ON g.generation_id=c.generation_id
            JOIN prepared_selections s
              ON s.competition_id=c.competition_id
             AND s.snapshot_version=c.snapshot_version
             AND s.selection_key=c.selection_key
            WHERE g.state='ready'
              AND s.qualifies_pattern=0
            """
        ).fetchone()[0]
    )
    if candidate_count <= 0:
        raise SystemExit("UAT probability-first candidate universe is empty")
    if expanded_count <= 0:
        raise SystemExit(
            "UAT probability-first candidate universe did not expand beyond the PROD pattern gate"
        )
finally:
    con.close()

manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
artifacts = manifest.get("artifacts", [])
if len(artifacts) != 2:
    raise SystemExit("UAT manifest must contain exactly two artifacts")
for item in artifacts:
    filename = Path(str(item.get("path") or "")).name
    artifact_path = out_root / filename
    if not filename or not artifact_path.is_file():
        raise SystemExit(f"Missing UAT artifact {filename!r}")
    digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    if digest != str(item.get("sha256") or ""):
        raise SystemExit(f"UAT artifact hash mismatch: {filename}")
    item["path"] = f"{PUBLISH_SUBDIR}/{filename}"
    item["url"] = RAW_BASE + filename

metadata = manifest.setdefault("metadata", {})
metadata["statmakerCommit"] = EXPECTED_STATMAKER_COMMIT
metadata["engineContract"] = "uat-probability-first-v1-prod-source-data"
metadata["inputRetirementContract"] = "asian-and-handicap-market-types-only-before-engine-v2"
metadata["uatProfile"] = "probability-first-v1"
manifest["profile"] = "app_ready_uat"
manifest_path.write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)

print(
    "APP_READY_UAT_PROBABILITY_FIRST_INTEGRITY_OK",
    f"candidates={candidate_count}",
    f"expanded_beyond_prod_gate={expanded_count}",
    f"rules={EXPECTED_RULES}",
    f"statmaker={EXPECTED_STATMAKER_COMMIT}",
    f"schema={EXPECTED_SCHEMA}",
    f"fixture_matches={fixture_match_count}",
    f"fixture_markets={fixture_market_count}",
)
