#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path

RULES = "pattern-policy-v2-final-read-model-v5-performance-shadow-v1-ou-value-v1"
STATMAKER_COMMIT = "561e152bc8302bb8240131cefc65b5350522c180"
UAT_BRANCH = "uat-ou-value-mispricing-20260920"
REQUIRED_COMPETITIONS = {
    "domestic", "champions_league", "europa_league", "conference_league"
}
STATS_ID = "app_ready_stats_bundle"
BETTING_ID = "app_ready_betting_bundle"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def read_json(path: Path, label: str) -> tuple[str, dict]:
    if not path.is_file() or path.stat().st_size <= 0:
        raise SystemExit(f"Missing/empty {label}: {path}")
    raw = path.read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid {label}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"Invalid {label} root")
    return raw, payload


def pref_strings(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise SystemExit(f"Missing preferences: {path}")
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise SystemExit(f"Invalid preferences XML {path}: {exc}") from exc
    return {
        str(node.attrib.get("name")): node.text or ""
        for node in root
        if node.tag == "string" and node.attrib.get("name")
    }


def verify_stats_artifact(base_manifest: dict, repo_root: Path) -> dict:
    artifacts = base_manifest.get("artifacts") or []
    source = next((item for item in artifacts if item.get("id") == STATS_ID), None)
    if not isinstance(source, dict):
        raise SystemExit("Base App-Ready manifest has no stats artifact")
    rel = str(source.get("path") or "")
    file = repo_root / rel
    if not file.is_file():
        raise SystemExit(f"Base stats artifact missing: {file}")
    expected_bytes = int(source.get("bytes") or 0)
    expected_sha = str(source.get("sha256") or "").lower()
    if file.stat().st_size != expected_bytes:
        raise SystemExit(f"Base stats artifact size mismatch: {file.stat().st_size} != {expected_bytes}")
    actual_sha = sha256_file(file)
    if actual_sha != expected_sha:
        raise SystemExit(f"Base stats artifact SHA mismatch: {actual_sha} != {expected_sha}")
    result = dict(source)
    result["url"] = (
        f"https://raw.githubusercontent.com/Velliouras/StatMaker-Data/{UAT_BRANCH}/"
        + rel
    )
    return result


def validate_prepared_db(db_path: Path) -> dict:
    if not db_path.is_file() or db_path.stat().st_size <= 4096:
        raise SystemExit(f"Missing/empty prepared DB: {db_path}")
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        quick = con.execute("PRAGMA quick_check").fetchone()
        if not quick or quick[0] != "ok":
            raise SystemExit(f"Prepared DB quick_check failed: {quick}")
        schema = int(con.execute("PRAGMA user_version").fetchone()[0])
        if schema != 11:
            raise SystemExit(f"Prepared schema {schema} != 11")

        tables = {str(r[0]) for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required_tables = {
            "prepared_snapshot_meta", "prepared_matches", "prepared_selections",
            "prepared_pattern_generation", "prepared_pattern_candidates",
        }
        missing = sorted(required_tables - tables)
        if missing:
            raise SystemExit("Prepared DB missing tables: " + ", ".join(missing))

        ready_rows = con.execute(
            """
            SELECT competition_id, snapshot_version, match_count, selection_count
            FROM prepared_snapshot_meta
            WHERE state='ready'
            """
        ).fetchall()
        ready = {
            str(comp): (str(version), int(matches), int(selections))
            for comp, version, matches, selections in ready_rows
        }
        if set(ready) != REQUIRED_COMPETITIONS:
            raise SystemExit(f"READY snapshots mismatch: {sorted(ready)}")

        source_seed = "\n".join(
            f"{competition}|{ready[competition][0]}"
            for competition in sorted(REQUIRED_COMPETITIONS)
        )
        source_fingerprint = sha256_text(source_seed)
        generation_id = sha256_text(f"{source_fingerprint}|{RULES}")

        generation = con.execute(
            """
            SELECT source_fingerprint, rules_fingerprint, state, candidate_count, proposal_count
            FROM prepared_pattern_generation
            WHERE generation_id=?
            """,
            (generation_id,),
        ).fetchone()
        if generation is None:
            available = con.execute(
                "SELECT generation_id, rules_fingerprint, state, candidate_count FROM prepared_pattern_generation"
            ).fetchall()
            raise SystemExit(
                f"Exact O/U generation missing: expected={generation_id} available={available}"
            )
        stored_source, stored_rules, state, candidate_count, proposal_count = generation
        if str(stored_source) != source_fingerprint:
            raise SystemExit("Prepared source fingerprint mismatch")
        if str(stored_rules) != RULES:
            raise SystemExit(f"Prepared rules mismatch: {stored_rules}")
        if str(state) != "ready":
            raise SystemExit(f"Prepared generation state is {state!r}")
        if int(proposal_count) != 0:
            raise SystemExit("Candidate-only generation persisted proposal templates")

        actual_candidates = int(con.execute(
            "SELECT COUNT(*) FROM prepared_pattern_candidates WHERE generation_id=?",
            (generation_id,),
        ).fetchone()[0])
        if actual_candidates != int(candidate_count) or actual_candidates <= 0:
            raise SystemExit(
                f"Candidate count mismatch meta={candidate_count} actual={actual_candidates}"
            )

        eligible = int(con.execute(
            """
            SELECT COUNT(*) FROM prepared_pattern_candidates
            WHERE generation_id=? AND recommendation_eligible=1
            """,
            (generation_id,),
        ).fetchone()[0])
        if eligible <= 0:
            raise SystemExit("No recommendation-eligible candidates")

        joined = int(con.execute(
            """
            SELECT COUNT(*)
            FROM prepared_pattern_candidates c
            JOIN prepared_selections s
              ON s.competition_id=c.competition_id
             AND s.snapshot_version=c.snapshot_version
             AND s.selection_key=c.selection_key
            JOIN prepared_matches m
              ON m.competition_id=s.competition_id
             AND m.snapshot_version=s.snapshot_version
             AND m.match_key=s.match_key
            WHERE c.generation_id=? AND c.recommendation_eligible=1
            """,
            (generation_id,),
        ).fetchone()[0])
        if joined != eligible:
            raise SystemExit(f"Candidate/performance join mismatch eligible={eligible} joined={joined}")

        retired = int(con.execute(
            """
            SELECT COUNT(*)
            FROM prepared_pattern_candidates c
            JOIN prepared_selections s
              ON s.competition_id=c.competition_id
             AND s.snapshot_version=c.snapshot_version
             AND s.selection_key=c.selection_key
            WHERE c.generation_id=?
              AND (
                UPPER(COALESCE(s.identity_family,'')) LIKE '%ASIAN%' OR
                UPPER(COALESCE(s.identity_sub_market_key,'')) LIKE '%ASIAN%' OR
                UPPER(COALESCE(s.identity_sub_market_key,'')) LIKE '%HANDICAP%' OR
                UPPER(COALESCE(s.selection_market,'')) LIKE '%ASIAN%' OR
                UPPER(COALESCE(s.selection_market,'')) LIKE '%HANDICAP%'
              )
            """,
            (generation_id,),
        ).fetchone()[0])
        if retired:
            raise SystemExit(f"Retired Asian/Handicap candidates leaked: {retired}")

        context = con.execute(
            """
            SELECT
              SUM(CASE WHEN s.opponent_adjusted_required=1 THEN 1 ELSE 0 END),
              SUM(CASE WHEN s.opponent_model_probability IS NOT NULL THEN 1 ELSE 0 END),
              SUM(CASE WHEN s.opponent_without_favorite_probability IS NOT NULL THEN 1 ELSE 0 END)
            FROM prepared_pattern_candidates c
            JOIN prepared_selections s
              ON s.competition_id=c.competition_id
             AND s.snapshot_version=c.snapshot_version
             AND s.selection_key=c.selection_key
            WHERE c.generation_id=? AND c.recommendation_eligible=1
            """,
            (generation_id,),
        ).fetchone()
        opponent_required = int(context[0] or 0)
        opponent_models = int(context[1] or 0)
        favorite_shadow = int(context[2] or 0)
        if opponent_required > 0 and opponent_models <= 0:
            raise SystemExit("Opponent-adjusted candidates exist without persisted models")

        directions = {
            str(side): int(count)
            for side, count in con.execute(
                """
                SELECT s.identity_selection_side, COUNT(*)
                FROM prepared_pattern_candidates c
                JOIN prepared_selections s
                  ON s.competition_id=c.competition_id
                 AND s.snapshot_version=c.snapshot_version
                 AND s.selection_key=c.selection_key
                WHERE c.generation_id=?
                  AND c.recommendation_eligible=1
                  AND c.selection_odd>=1.50
                  AND s.identity_selection_side IN ('OVER','UNDER')
                GROUP BY s.identity_selection_side
                """,
                (generation_id,),
            ).fetchall()
        }

        return {
            "schemaVersion": schema,
            "generationId": generation_id,
            "sourceFingerprint": source_fingerprint,
            "rulesFingerprint": RULES,
            "candidateCount": actual_candidates,
            "recommendationEligibleCount": eligible,
            "performanceRowCount": joined,
            "opponentAdjustedRequiredCount": opponent_required,
            "opponentModelCount": opponent_models,
            "favoriteShadowCount": favorite_shadow,
            "directions": directions,
        }
    finally:
        con.close()


def build_bundle(out_dir: Path, db_path: Path, odds_paths: dict[str, Path]) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, Path] = {"databases/statmaker_prepared_betting.db": db_path}
    for competition, path in odds_paths.items():
        if not path.is_file() or path.stat().st_size <= 0:
            raise SystemExit(f"Missing odds file: {path}")
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Invalid odds JSON {path}: {exc}") from exc
        files[f"files/app_ready_odds/{competition}.json"] = path

    rows = [
        {"path": rel, "sha256": sha256_file(src), "bytes": src.stat().st_size}
        for rel, src in sorted(files.items())
    ]
    bundle_manifest = json.dumps(
        {"schemaVersion": 1, "bundleType": "betting", "files": rows},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    tmp = out_dir / f"{BETTING_ID}.tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        entries = {"bundle_manifest.json": bundle_manifest}
        for rel, src in files.items():
            entries[rel] = src.read_bytes()
        for rel in sorted(entries):
            info = zipfile.ZipInfo(rel, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, entries[rel], compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)

    digest = sha256_file(tmp)
    final = out_dir / f"{BETTING_ID}-{digest}.zip"
    tmp.replace(final)
    return {
        "id": BETTING_ID,
        "group": "prepared",
        "path": f"data/statmaker/app_ready/{final.name}",
        "url": (
            f"https://raw.githubusercontent.com/Velliouras/StatMaker-Data/{UAT_BRANCH}/"
            f"data/statmaker/app_ready/{final.name}"
        ),
        "sha256": digest,
        "bytes": final.stat().st_size,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("raw_root")
    parser.add_argument("source_root")
    parser.add_argument("out_root")
    parser.add_argument("--base-manifest", required=True)
    parser.add_argument("--repo-root", default=".")
    args = parser.parse_args()

    raw_root = Path(args.raw_root)
    source_root = Path(args.source_root)
    out_root = Path(args.out_root)
    repo_root = Path(args.repo_root)

    _, base_manifest = read_json(Path(args.base_manifest), "base App-Ready manifest")
    stats = verify_stats_artifact(base_manifest, repo_root)

    main_raw, main_manifest = read_json(source_root / "main_manifest.json", "main manifest")
    uefa_raw, uefa_manifest = read_json(source_root / "uefa_manifest.json", "UEFA manifest")
    if int(main_manifest.get("schemaVersion") or 0) < 2:
        raise SystemExit("Unsupported main manifest schema")
    if int(uefa_manifest.get("schemaVersion") or 0) < 2:
        raise SystemExit("Unsupported UEFA manifest schema")

    prepared = validate_prepared_db(raw_root / "databases/statmaker_prepared_betting.db")

    prefs = pref_strings(raw_root / "shared_prefs/statmaker_prepared_data_versions.xml")
    domestic_fp = prefs.get("domestic_history_fingerprint", "").strip()
    support_fp = prefs.get("uefa_support_fingerprint", "").strip()
    if not domestic_fp or not support_fp:
        raise SystemExit("Prepared-data fingerprints are missing")

    odds_paths = {
        "domestic": source_root / "domestic.json",
        "champions_league": source_root / "champions_league.json",
        "europa_league": source_root / "europa_league.json",
        "conference_league": source_root / "conference_league.json",
    }
    betting = build_bundle(out_root, raw_root / "databases/statmaker_prepared_betting.db", odds_paths)

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    stats["url"] = (
        f"https://raw.githubusercontent.com/Velliouras/StatMaker-Data/{UAT_BRANCH}/"
        + str(stats["path"])
    )
    betting["generatedAt"] = now

    seed = "\n".join(sorted([
        f"main|{main_manifest.get('contentVersion','')}",
        f"uefa|{uefa_manifest.get('contentVersion','')}",
        f"stats|{stats['sha256']}",
        f"betting|{betting['sha256']}",
        f"domestic|{domestic_fp}",
        f"support|{support_fp}",
        f"rules|{RULES}",
    ]))
    manifest = {
        "schemaVersion": 1,
        "profile": "app_ready_uat_ou_value",
        "contentVersion": sha256_text(seed),
        "generatedAt": now,
        "artifactCount": 2,
        "artifacts": [stats, betting],
        "metadata": {
            "domesticHistoryFingerprint": domestic_fp,
            "uefaSupportFingerprint": support_fp,
            "mainContentVersion": main_manifest.get("contentVersion", ""),
            "uefaContentVersion": uefa_manifest.get("contentVersion", ""),
            "mainManifestRaw": main_raw,
            "uefaManifestRaw": uefa_raw,
            "statmakerCommit": STATMAKER_COMMIT,
            "preparedBettingSchemaVersion": prepared["schemaVersion"],
            "preparedPatternGenerationId": prepared["generationId"],
            "preparedPatternSourceFingerprint": prepared["sourceFingerprint"],
            "preparedPatternRulesFingerprint": prepared["rulesFingerprint"],
            "preparedPatternCandidateCount": prepared["candidateCount"],
            "preparedPatternRecommendationEligibleCount": prepared["recommendationEligibleCount"],
            "preparedPerformanceRowCount": prepared["performanceRowCount"],
            "preparedOpponentAdjustedRequiredCount": prepared["opponentAdjustedRequiredCount"],
            "preparedOpponentModelCount": prepared["opponentModelCount"],
            "preparedFavoriteShadowCount": prepared["favoriteShadowCount"],
            "uatOverUnderDirectionCountsAt150": prepared["directions"],
            "uatBettingOnly": True,
        },
    }
    out_root.mkdir(parents=True, exist_ok=True)
    (out_root / "update_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        "UAT_OU_BETTING_APP_READY_OK",
        f"generation={prepared['generationId']}",
        f"candidates={prepared['candidateCount']}",
        f"eligible={prepared['recommendationEligibleCount']}",
        f"directions={prepared['directions']}",
        f"betting_sha={betting['sha256'][:12]}",
        f"stats_sha={stats['sha256'][:12]}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
