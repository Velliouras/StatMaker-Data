#!/usr/bin/env python3
"""Repackage isolated UAT betting candidate SQLite from immutable PROD App-Ready.

DO NOT replay historic source manifests against odds that no longer exist at
their manifest revisions. We reuse the *exact* already-published market and
stats payloads, replace only the prepared betting DB, and fail if any other
binary member changes. This never publishes to the production App-Ready path.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path


def sha_bytes(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_artifact(root: Path, record: dict) -> Path:
    rel = str(record.get("path") or "")
    path = root / rel
    if not path.is_file():
        raise ValueError(f"Immutable base artifact missing: {rel}")
    if path.stat().st_size != int(record.get("bytes") or -1):
        raise ValueError(f"Immutable base artifact size mismatch: {rel}")
    if sha_file(path) != record.get("sha256"):
        raise ValueError(f"Immutable base artifact SHA mismatch: {rel}")
    return path


def require_complete_zip(archive: zipfile.ZipFile, expected_type: str) -> dict:
    names = set(archive.namelist())
    if "bundle_manifest.json" not in names:
        raise ValueError("App-Ready bundle manifest missing")
    meta = json.loads(archive.read("bundle_manifest.json"))
    if meta.get("bundleType") != expected_type or meta.get("schemaVersion") != 1:
        raise ValueError("Invalid App-Ready bundle type")
    declarations = {row["path"]: row for row in meta["files"]}
    if names != set(declarations) | {"bundle_manifest.json"}:
        raise ValueError("Missing/extra binary members compared with App-Ready manifest")
    for name, entry in declarations.items():
        data = archive.read(name)
        if len(data) != int(entry["bytes"]) or sha_bytes(data) != entry["sha256"]:
            raise ValueError(f"Archived member mismatch: {name}")
    return meta


def inspect_db(db: Path, expected_rules: str) -> dict:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        if con.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Rebuilt UAT prepared DB failed SQLite quick_check")
        version = int(con.execute("PRAGMA user_version").fetchone()[0])
        if version < 12:
            raise ValueError("UAT prepared DB schema below 12")
        rows = con.execute("""
            SELECT generation_id,source_fingerprint,rules_fingerprint,
                   candidate_count, state
            FROM prepared_pattern_generation WHERE state='ready'
        """).fetchall()
        if len(rows) != 1 or rows[0][2] != expected_rules:
            raise ValueError("UAT generator contract mismatches checked Android UAT code")
        gen, fingerprint, rules, expected_count, _ = rows[0]
        actual = con.execute("""
            SELECT COUNT(*) FROM prepared_pattern_candidates WHERE generation_id=?
        """, (gen,)).fetchone()[0]
        if not actual or actual != expected_count:
            raise ValueError("UAT candidate count mismatch")
        eligible = con.execute("""
            SELECT COUNT(*) FROM prepared_pattern_candidates
            WHERE generation_id=? AND recommendation_eligible=1
        """, (gen,)).fetchone()[0]
        premium = con.execute("""
            SELECT COUNT(*) FROM prepared_pattern_candidates
            WHERE generation_id=? AND policy_premium_eligible=1
        """, (gen,)).fetchone()[0]
        stale = con.execute("""
            SELECT COUNT(*) FROM prepared_pattern_candidates c
            JOIN prepared_selections s
              ON s.competition_id=c.competition_id AND s.snapshot_version=c.snapshot_version
             AND s.selection_key=c.selection_key
            WHERE c.generation_id=?
              AND s.identity_sub_market_key IN ('RESULT_DOUBLE_CHANCE','HT_RESULT_DOUBLE_CHANCE')
              AND s.bm_market_probability_source='no-vig'
              AND s.bm_market_overround>1.40
        """, (gen,)).fetchone()[0]
        if stale: raise ValueError(f"Stale double-chance pool candidates: {stale}")
        model_count = con.execute("""
            SELECT COUNT(*) FROM prepared_selections WHERE opponent_model_probability IS NOT NULL
        """).fetchone()[0]
        required = con.execute("""
            SELECT COUNT(*) FROM prepared_selections WHERE opponent_adjusted_required=1
        """).fetchone()[0]
        return dict(schema=version, generationId=gen, sourceFingerprint=fingerprint,
                    rulesFingerprint=rules, candidateCount=actual, eligibleCount=eligible,
                    premiumCount=premium, modelCount=model_count,
                    opponentRequiredCount=required)
    finally:
        con.close()


def pack(root: Path, manifest: dict, database: Path, out: Path,
         destination: str, engine_commit: str, rules: str) -> dict:
    if not destination.startswith("data/statmaker/app_ready_uat/"):
        raise ValueError("Never package into the PROD App-Ready destination")
    if not engine_commit or not rules:
        raise ValueError("Missing exact engine generation contract")
    records = {a["id"]: a for a in manifest.get("artifacts", [])}
    if set(records) != {"app_ready_stats_bundle", "app_ready_betting_bundle"}:
        raise ValueError("Unknown base artifact set")
    stats_path = verify_artifact(root, records["app_ready_stats_bundle"])
    betting_path = verify_artifact(root, records["app_ready_betting_bundle"])
    info = inspect_db(database, rules)
    out.mkdir(parents=True, exist_ok=True)
    stats_copy = out / stats_path.name
    shutil.copy2(stats_path, stats_copy)

    with zipfile.ZipFile(stats_path) as source_stats:
        require_complete_zip(source_stats, "stats")
    with zipfile.ZipFile(betting_path) as source:
        base_manifest = require_complete_zip(source, "betting")
        rows = []
        new_contents = {}
        for original in base_manifest["files"]:
            name = original["path"]
            if name == "databases/statmaker_prepared_betting.db":
                data = database.read_bytes()
            else:
                data = source.read(name)
                if sha_bytes(data) != original["sha256"]:
                    raise ValueError("Original odds changed during repack")
            new_contents[name] = data
            rows.append({"path": name, "bytes": len(data), "sha256": sha_bytes(data)})
        if "databases/statmaker_prepared_betting.db" not in new_contents:
            raise ValueError("Prepared DB missing from base betting bundle")
        replacement_manifest = json.dumps(
            {"schemaVersion": 1, "bundleType": "betting", "files": rows},
            sort_keys=True, separators=(",", ":")
        ).encode()
        new_contents["bundle_manifest.json"] = replacement_manifest

    tmp = out / "app_ready_betting_bundle.tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(new_contents.items()):
            zi = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o644 << 16
            archive.writestr(zi, data, compress_type=zipfile.ZIP_DEFLATED,
                             compresslevel=9)
    digest = sha_file(tmp)
    betting_copy = out / f"app_ready_betting_bundle-{digest}.zip"
    tmp.replace(betting_copy)
    with zipfile.ZipFile(betting_copy) as verify:
        require_complete_zip(verify, "betting")

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    artifacts = []
    for kind, record, path in [
        ("app_ready_stats_bundle", records["app_ready_stats_bundle"], stats_copy),
        ("app_ready_betting_bundle", records["app_ready_betting_bundle"], betting_copy)
    ]:
        sha = sha_file(path)
        artifacts.append(dict(id=kind, group=record["group"],
            path=destination + "/" + path.name,
            url="https://raw.githubusercontent.com/Velliouras/StatMaker-Data/main/"
                + destination + "/" + path.name,
            sha256=sha, bytes=path.stat().st_size, generatedAt=now))
    metadata = dict(manifest["metadata"])
    metadata.update({
        "preparedBettingSchemaVersion": info["schema"],
        "preparedPatternGenerationId": info["generationId"],
        "preparedPatternSourceFingerprint": info["sourceFingerprint"],
        "preparedPatternRulesFingerprint": info["rulesFingerprint"],
        "preparedPatternCandidateCount": info["candidateCount"],
        "preparedPatternRecommendationEligibleCount": info["eligibleCount"],
        "preparedPerformanceRowCount": info["eligibleCount"],
        "preparedOpponentAdjustedRequiredCount": info["opponentRequiredCount"],
        "preparedOpponentModelCount": info["modelCount"],
        "statmakerCommit": engine_commit,
        "engineContract": "uat-probability-first-v1-prod-source-data",
        "uatProfile": "probability-first-v1",
        "inputRetirementContract": "asian-and-handicap-market-types-only-before-engine-v2",
        "baseAppReadyContentVersion": manifest["contentVersion"],
        "inputOddsProvenance": "immutable-published-PROD-App-Ready-betting-archive",
    })
    fingerprint = "\n".join(sorted([
        "base|" + manifest["contentVersion"],
        "stats|" + artifacts[0]["sha256"],
        "betting|" + artifacts[1]["sha256"],
        "engine|" + engine_commit,
        "rules|" + rules,
    ]))
    published = {
        "schemaVersion": 1, "profile": "app_ready_uat",
        "contentVersion": sha_bytes(fingerprint.encode()),
        "generatedAt": now, "artifactCount": len(artifacts),
        "artifacts": artifacts, "metadata": metadata
    }
    (out / "update_manifest.json").write_text(
        json.dumps(published, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("UAT_IMMUTABLE_REPACK_OK",
          f"candidates={info['candidateCount']}",
          f"eligible={info['eligibleCount']}",
          f"premium={info['premiumCount']}",
          f"engine={engine_commit[:12]}",
          f"base={manifest['contentVersion'][:12]}", flush=True)
    return published


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--prepared", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--destination", required=True)
    args = ap.parse_args()
    contract = os.environ.get("APP_READY_PATTERN_RULES_FINGERPRINT", "")
    commit = os.environ.get("APP_READY_STATMAKER_COMMIT", "")
    if not contract or not commit:
        raise SystemExit("Missing exact UAT engine contract")
    root = Path.cwd()
    m = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    pack(root, m, Path(args.prepared), Path(args.out), args.destination,
         commit, contract)


if __name__ == "__main__":
    main()
