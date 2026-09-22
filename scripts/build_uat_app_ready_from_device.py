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
PUBLISH_SUBDIR = os.environ.get(
    "APP_READY_PUBLISH_SUBDIR",
    "data/statmaker/app_ready_uat/ou-direction-v2",
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
    if version != 11:
        raise SystemExit(f"Expected prepared schema 11, got {version}")

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

    rows = con.execute(
        """
        SELECT competition_id, snapshot_version, match_key, selection_market,
               COALESCE(selection_team,''), selection_line, selection_name,
               COALESCE(historical_outcomes_bits,''), COALESCE(identity_selection_side,'')
        FROM prepared_selections
        WHERE selection_line IS NOT NULL
        """
    ).fetchall()

    explicit = 0
    pairs = {}
    for competition, snapshot, match_key, market, team, line, name, bits, identity_side in rows:
        tokens = {
            token.lower()
            for token in re.findall(r"[A-Za-z]+", str(name or ""))
            if token.lower() in {"over", "under"}
        }
        if len(tokens) != 1:
            continue
        direction = next(iter(tokens))
        explicit += 1
        if str(identity_side or "").upper() != direction.upper():
            raise SystemExit(
                "UAT direction identity mismatch: "
                f"{competition}|{match_key}|{name}|identity={identity_side}"
            )
        key = (
            str(competition), str(snapshot), str(match_key), str(market),
            str(team), float(line),
        )
        pairs.setdefault(key, {})[direction] = str(bits or "")

    checked_pairs = 0
    for key, sides in pairs.items():
        over = sides.get("over", "")
        under = sides.get("under", "")
        line = key[-1]
        if not over or not under or len(over) != len(under):
            continue
        if abs(line * 2.0 - round(line * 2.0)) > 1e-9 or int(round(line * 2.0)) % 2 == 0:
            continue
        checked_pairs += 1
        if any(a == b for a, b in zip(over, under)):
            raise SystemExit(
                "UAT Over/Under complement invariant failed: "
                f"{key} over={over} under={under}"
            )

    if explicit <= 0 or checked_pairs <= 0:
        raise SystemExit(
            f"UAT direction integrity coverage is empty: explicit={explicit} pairs={checked_pairs}"
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
metadata["engineContract"] = "uat-current-source-direction-token-v2-ou-quality-gate-v1"
metadata["inputRetirementContract"] = "asian-and-handicap-market-types-only-before-engine-v2"
metadata["uatProfile"] = "ou-direction-v2"
manifest["profile"] = "app_ready_uat"
manifest_path.write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)

print(
    "APP_READY_UAT_DIRECTION_INTEGRITY_OK",
    f"explicit={explicit}",
    f"pairs={checked_pairs}",
    f"rules={EXPECTED_RULES}",
    f"statmaker={EXPECTED_STATMAKER_COMMIT}",
    f"fixture_matches={fixture_match_count}",
    f"fixture_markets={fixture_market_count}",
)
