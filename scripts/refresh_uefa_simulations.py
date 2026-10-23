#!/usr/bin/env python3
"""Refresh only UEFA simulations from published results. No provider access."""
from __future__ import annotations
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from scripts import materialize_prepared_uefa_simulations as sim

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "data/statmaker/app_ready/update_manifest.json"
REPORT = ROOT / "reports/uefa_simulation_refresh.json"
FINGERPRINT_KEY = "preparedUefaSimulationSourceFingerprint"


def bundle_db(manifest, folder):
    artifact = next(a for a in manifest["artifacts"] if a["id"] == "app_ready_betting_bundle")
    path = ROOT / artifact["path"]
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != artifact["sha256"]:
        raise ValueError("Prepared bundle SHA256 mismatch")
    with zipfile.ZipFile(path) as z:
        z.extract("databases/statmaker_prepared_betting.db", folder)
    return Path(folder) / "databases/statmaker_prepared_betting.db"


def unaffected_digest(con):
    """Every non-UEFA table and every domestic match row must remain identical."""
    h = hashlib.sha256()
    excluded = {"prepared_uefa_simulation_meta", "prepared_uefa_team_simulations"}
    tables = con.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
    counts = {}
    for table, schema in tables:
        if table in excluded or table.startswith("sqlite_"):
            continue
        quoted = '"' + table.replace('"', '""') + '"'
        query = "SELECT * FROM " + quoted
        if table == "prepared_match_simulations":
            query += " WHERE competition_id NOT IN ('champions_league','europa_league','conference_league')"
        pk = [r[1] for r in sorted(con.execute("PRAGMA table_info(" + quoted + ")"), key=lambda r: r[5]) if r[5]]
        order = ",".join('"' + k.replace('"', '""') + '"' for k in pk) if pk else "rowid"
        h.update(repr((table, schema)).encode())
        count = 0
        for row in con.execute(query + " ORDER BY " + order):
            h.update(repr(row).encode())
            h.update(b"\n")
            count += 1
        counts[table] = count
    return h.hexdigest(), counts


def main():
    config = json.loads(sim.CONFIG_PATH.read_text())
    fingerprint = sim.cached_source_fingerprint(config)
    before = json.loads(MANIFEST.read_text())
    current_bundle = next(a for a in before["artifacts"] if a["id"] == "app_ready_betting_bundle")
    if (before.get("metadata", {}).get(FINGERPRINT_KEY) == fingerprint
            and before.get("metadata", {}).get("preparedUefaSimulationValidatedBundleSha256") == current_bundle["sha256"]):
        print("UEFA_SIMULATION_CURRENT source=" + fingerprint)
        return 0
    with tempfile.TemporaryDirectory() as td:
        before_db = bundle_db(before, Path(td) / "before")
        with sqlite3.connect(before_db) as con:
            for comp in config["competitions"]:
                fixtures, _, _, _ = sim.cached_competition(comp, int(config["season"]))
                completed = sum(f.status in sim.FINISHED for f in fixtures)
                old = con.execute("SELECT completed_league_matches FROM prepared_uefa_simulation_meta "
                                  "WHERE competition_id=?", (comp["competitionId"],)).fetchone()
                if old and int(old[0]) > completed:
                    print("UEFA_SIMULATION_WAITING_FOR_NEWER_TABLES " + comp["competitionId"])
                    return 0
            protected_before = unaffected_digest(con)
            previous = con.execute("SELECT team_name,current_position,current_played,current_points "
                "FROM prepared_uefa_team_simulations WHERE team_name='Panathinaikos'").fetchall()
        subprocess.run([sys.executable, str(ROOT / "scripts/hotpatch_app_ready_simulation.py"),
                        "--uefa-only"], cwd=ROOT, check=True)
        after = json.loads(MANIFEST.read_text())
        after_db = bundle_db(after, Path(td) / "after")
        with sqlite3.connect(after_db) as con:
            verified = sim.verify_cached_simulations(con, config)
            if protected_before != unaffected_digest(con):
                raise ValueError("UEFA refresh modified protected prepared/history/performance/domestic data")
            current = con.execute("SELECT team_name,current_position,current_played,current_points "
                "FROM prepared_uefa_team_simulations WHERE team_name='Panathinaikos'").fetchall()
            probabilities = con.execute("SELECT competition_id,SUM(winner_probability),MIN(top8_probability),"
                "MAX(top8_probability) FROM prepared_uefa_team_simulations GROUP BY competition_id").fetchall()
            if any(abs(r[1]-1) > 1e-8 or r[2] < 0 or r[3] > 1 for r in probabilities):
                raise ValueError("Invalid simulated probability mass")
        # Keep the initial manifest for retention; never discard the last usable generation.
        previous_path = Path(td) / "previous_manifest.json"
        previous_path.write_text(json.dumps(before))
        subprocess.run([sys.executable, str(ROOT / "scripts/prune_app_ready_unreferenced_bundles.py"),
                        "--previous-manifest", str(previous_path)], cwd=ROOT, check=True)
        if fingerprint != sim.cached_source_fingerprint(config):
            raise ValueError("UEFA results changed during generation")
        after.setdefault("metadata", {})[FINGERPRINT_KEY] = fingerprint
        after["metadata"]["preparedUefaSimulationValidatedBundleSha256"] = next(
            a["sha256"] for a in after["artifacts"] if a["id"] == "app_ready_betting_bundle")
        after["metadata"]["preparedUefaSimulationInputContract"] = "published-league-tables-results-v1"
        after["contentVersion"] = hashlib.sha256(
            (after["contentVersion"] + "|" + fingerprint).encode()).hexdigest()
        MANIFEST.write_text(json.dumps(after, ensure_ascii=False, indent=2) + "\n")
        report = {"sourceFingerprint": fingerprint, "generatedAt": after["generatedAt"],
                  "apiRequestsUsed": 0, "verified": verified,
                  "panathinaikosBefore": previous, "panathinaikosAfter": current,
                  "protectedTablesUnchanged": True, "protectedDigest": protected_before[0],
                  "preparedBundle": next(a["sha256"] for a in after["artifacts"]
                                         if a["id"] == "app_ready_betting_bundle")}
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print("UEFA_SIMULATION_REFRESH_VERIFIED " + json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
