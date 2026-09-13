#!/usr/bin/env python3
"""Re-materialize only the recommendation contract from the latest verified App-Ready source.

READY snapshot identity is normalized before generation hashing so Kotlin/Python parity is stable.

This path is intentionally emulator-free. It is valid only when the published betting source is
already schema v13 and the source bundles are verified by their published SHA/size. The immutable
prepared selections/history remain unchanged; only the rules-dependent recommendation generation,
fixture read model and betting bundle are rebuilt.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import materialize_app_ready_pattern_candidates as pattern_materializer
import materialize_prepared_fixture_index as fixture_materializer

APP_READY = ROOT / "data" / "statmaker" / "app_ready"
PUBLISHED_MANIFEST = APP_READY / "update_manifest.json"
OUT = ROOT / "app-ready-export" / "out"
RAW = ROOT / "app-ready-export" / "raw"
RULES_FINGERPRINT = (
    "pattern-policy-v2-final-read-model-v8-retire-asian-handicap-independent-precision-v1"
)
SCHEMA_VERSION = 13
RETIRED_SUB_MARKETS = {
    "RESULT_ASIAN_HANDICAP",
    "HT_RESULT_ASIAN_HANDICAP",
    "ASIAN_MATCH_GOALS_TOTAL",
    "ASIAN_FIRST_HALF_GOALS_TOTAL",
    "ASIAN_MATCH_CORNERS_TOTAL",
    "ASIAN_CORNER_HANDICAP",
    "CORNER_HANDICAP",
}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def verified_artifact(manifest: dict, artifact_id: str) -> tuple[dict, Path]:
    artifact = next(
        (row for row in manifest.get("artifacts", []) if row.get("id") == artifact_id),
        None,
    )
    if not isinstance(artifact, dict):
        raise SystemExit(f"Published manifest has no {artifact_id}")
    path = ROOT / str(artifact.get("path") or "")
    if not path.is_file():
        raise SystemExit(f"Published artifact is missing: {path}")
    if path.stat().st_size != int(artifact.get("bytes") or 0):
        raise SystemExit(f"Published artifact size mismatch: {path.name}")
    if sha256(path) != str(artifact.get("sha256") or ""):
        raise SystemExit(f"Published artifact SHA mismatch: {path.name}")
    return artifact, path


def extract_verified_source(stats_zip: Path, betting_zip: Path, seed: Path) -> None:
    shutil.rmtree(seed, ignore_errors=True)
    seed.mkdir(parents=True)
    for bundle in (stats_zip, betting_zip):
        with zipfile.ZipFile(bundle) as archive:
            for info in archive.infolist():
                if info.filename == "bundle_manifest.json":
                    continue
                target = seed / info.filename
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)

    required = [
        seed / "databases" / "statmaker.db",
        seed / "databases" / "statmaker_prepared_betting.db",
        seed / "files" / "statmaker_stats_snapshots" / "champions_league.bin",
        seed / "files" / "statmaker_stats_snapshots" / "europa_league.bin",
        seed / "files" / "statmaker_stats_snapshots" / "conference_league.bin",
        seed / "files" / "app_ready_odds" / "domestic.json",
        seed / "files" / "app_ready_odds" / "champions_league.json",
        seed / "files" / "app_ready_odds" / "europa_league.json",
        seed / "files" / "app_ready_odds" / "conference_league.json",
    ]
    missing = [str(path) for path in required if not path.is_file() or path.stat().st_size <= 0]
    if missing:
        raise SystemExit("Verified App-Ready source is incomplete: " + ", ".join(missing))


def finalize_sqlite_for_bundle(db_path: Path) -> None:
    """Merge any WAL into the main DB and make the database self-contained for the ZIP."""
    if not db_path.is_file():
        raise SystemExit(f"Prepared DB is missing before finalize: {db_path}")

    connection = sqlite3.connect(db_path)
    try:
        connection.commit()
        mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        if mode == "wal":
            checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            print("APP_READY_SQLITE_WAL_CHECKPOINT", checkpoint)
        connection.execute("PRAGMA journal_mode=DELETE").fetchone()
        connection.commit()
    finally:
        connection.close()

    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(db_path) + suffix)
        if sidecar.exists():
            sidecar.unlink()

    # The bundle must be valid from the main SQLite file alone.
    standalone = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        quick = standalone.execute("PRAGMA quick_check").fetchone()
        if not quick or str(quick[0]).lower() != "ok":
            raise SystemExit(f"Standalone prepared DB quick_check failed: {quick}")
        mode = str(standalone.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        if mode == "wal":
            raise SystemExit("Prepared DB remained in WAL mode after finalize")
    finally:
        standalone.close()


def validate_betting_zip(bundle_path: Path, expected_generation: str) -> None:
    """Validate the exact prepared DB bytes that the phone will receive from the ZIP."""
    with tempfile.TemporaryDirectory(prefix="statmaker-betting-zip-verify-") as temp:
        root = Path(temp)
        with zipfile.ZipFile(bundle_path) as archive:
            names = set(archive.namelist())
            required = "databases/statmaker_prepared_betting.db"
            if required not in names:
                raise SystemExit("Betting ZIP is missing prepared DB")
            forbidden = [
                name for name in names
                if name.endswith((".db-wal", ".db-shm", ".db-journal"))
            ]
            if forbidden:
                raise SystemExit("Betting ZIP contains SQLite sidecars: " + ", ".join(forbidden))
            archive.extract(required, root)

        extracted = root / required
        verified = validate_rules_generation(extracted)
        if verified["generationId"] != expected_generation:
            raise SystemExit(
                "Bundled prepared DB generation mismatch: "
                f"expected={expected_generation} actual={verified['generationId']}"
            )
        print(
            "APP_READY_BETTING_ZIP_DB_OK",
            f"generation={verified['generationId']}",
            f"precision={verified['precisionEligibleCount']}",
            f"performance={verified['performanceRowCount']}",
        )


def bundle_betting(raw_root: Path) -> dict:
    sources = {
        "databases/statmaker_prepared_betting.db": raw_root / "databases" / "statmaker_prepared_betting.db",
        **{
            f"files/app_ready_odds/{name}.json":
                raw_root / "files" / "app_ready_odds" / f"{name}.json"
            for name in (
                "domestic",
                "champions_league",
                "europa_league",
                "conference_league",
            )
        },
    }
    work = OUT / ".rules-betting"
    shutil.rmtree(work, ignore_errors=True)
    content = work / "content"
    content.mkdir(parents=True)
    rows = []
    for rel, source in sorted(sources.items()):
        if not source.is_file() or source.stat().st_size <= 0:
            raise SystemExit(f"Missing rules-only source {source}")
        target = content / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        rows.append({"path": rel, "sha256": sha256(target), "bytes": target.stat().st_size})

    (content / "bundle_manifest.json").write_text(
        json.dumps(
            {"schemaVersion": 1, "bundleType": "betting", "files": rows},
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    tmp = OUT / "app_ready_betting_bundle.tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for source in sorted(path for path in content.rglob("*") if path.is_file()):
            info = zipfile.ZipInfo(
                source.relative_to(content).as_posix(),
                (1980, 1, 1, 0, 0, 0),
            )
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(
                info,
                source.read_bytes(),
                compress_type=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            )
    digest = sha256(tmp)
    final = OUT / f"app_ready_betting_bundle-{digest}.zip"
    tmp.replace(final)
    return {
        "id": "app_ready_betting_bundle",
        "group": "prepared",
        "path": f"data/statmaker/app_ready/{final.name}",
        "url": f"https://raw.githubusercontent.com/Velliouras/StatMaker-Data/main/data/statmaker/app_ready/{final.name}",
        "sha256": digest,
        "bytes": final.stat().st_size,
    }


def normalize_ready_snapshots(db_path: Path) -> dict[str, str]:
    """Enforce exactly one deterministic READY snapshot per competition.

    Historical READY rows can coexist in schema v13. Python/Kotlin must never rely on SQLite row
    order when deriving the recommendation source fingerprint.
    """
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute(
            """
            SELECT competition_id, snapshot_version, built_at_ms
            FROM prepared_snapshot_meta
            WHERE state='ready'
            ORDER BY competition_id, built_at_ms DESC, snapshot_version DESC
            """
        ).fetchall()
        required = set(pattern_materializer.COMPETITIONS)
        by_competition: dict[str, list[tuple[str, int]]] = {}
        for competition, version, built_at_ms in rows:
            by_competition.setdefault(str(competition), []).append(
                (str(version), int(built_at_ms or 0))
            )
        if set(by_competition) != required:
            raise SystemExit(
                "Rules-only source does not contain all required READY competitions: "
                + str(sorted(by_competition))
            )

        chosen = {
            competition: versions[0][0]
            for competition, versions in by_competition.items()
        }
        connection.execute("BEGIN IMMEDIATE")
        try:
            for competition, versions in by_competition.items():
                keep = chosen[competition]
                connection.execute(
                    """
                    UPDATE prepared_snapshot_meta
                    SET state='stale'
                    WHERE competition_id=? AND state='ready' AND snapshot_version<>?
                    """,
                    (competition, keep),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

        normalized = connection.execute(
            """
            SELECT competition_id, snapshot_version
            FROM prepared_snapshot_meta
            WHERE state='ready'
            ORDER BY competition_id
            """
        ).fetchall()
        if len(normalized) != len(required):
            raise SystemExit(
                f"READY snapshot cardinality mismatch after normalization: rows={len(normalized)}"
            )
        result = {str(row[0]): str(row[1]) for row in normalized}
        if set(result) != required or len(result) != len(required):
            raise SystemExit("READY snapshot uniqueness invariant failed after normalization")

        print(
            "APP_READY_READY_SNAPSHOT_NORMALIZED",
            " ".join(f"{k}={result[k]}" for k in sorted(result)),
        )
        return result
    finally:
        connection.close()


def validate_rules_generation(db_path: Path) -> dict:
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        quick = connection.execute("PRAGMA quick_check").fetchone()
        if not quick or quick[0] != "ok":
            raise SystemExit(f"Rules-only prepared DB quick_check failed: {quick}")
        schema = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if schema < SCHEMA_VERSION:
            raise SystemExit(f"Rules-only prepared DB schema {schema} < {SCHEMA_VERSION}")

        ready_rows = connection.execute(
            """
            SELECT competition_id, snapshot_version
            FROM prepared_snapshot_meta
            WHERE state='ready'
            ORDER BY competition_id
            """
        ).fetchall()
        required = set(pattern_materializer.COMPETITIONS)
        if len(ready_rows) != len(required):
            raise SystemExit(
                f"Rules-only source must contain exactly {len(required)} READY rows, "
                f"found={len(ready_rows)}"
            )
        ready = {str(row[0]): str(row[1]) for row in ready_rows}
        if set(ready) != required or len(ready) != len(required):
            raise SystemExit(
                f"Rules-only source is not exactly one READY row per competition: {sorted(ready)}"
            )

        source_seed = "\n".join(
            f"{competition}|{ready[competition]}" for competition in sorted(required)
        )
        source_fingerprint = hashlib.sha256(source_seed.encode()).hexdigest()
        generation_id = hashlib.sha256(
            f"{source_fingerprint}|{RULES_FINGERPRINT}".encode()
        ).hexdigest()

        generation = connection.execute(
            """
            SELECT rules_fingerprint, state, candidate_count
            FROM prepared_pattern_generation
            WHERE generation_id=?
            LIMIT 1
            """,
            (generation_id,),
        ).fetchone()
        if not generation:
            raise SystemExit("Rules-only v8 recommendation generation is missing")
        rules, state, candidate_count = generation
        if str(rules) != RULES_FINGERPRINT or str(state) != "ready":
            raise SystemExit("Rules-only recommendation generation contract mismatch")

        actual = int(connection.execute(
            "SELECT COUNT(*) FROM prepared_pattern_candidates WHERE generation_id=?",
            (generation_id,),
        ).fetchone()[0])
        if actual != int(candidate_count) or actual <= 0:
            raise SystemExit(
                f"Rules-only candidate count mismatch meta={candidate_count} actual={actual}"
            )

        eligible = int(connection.execute(
            "SELECT COUNT(*) FROM prepared_pattern_candidates WHERE generation_id=? AND recommendation_eligible=1",
            (generation_id,),
        ).fetchone()[0])
        precision = int(connection.execute(
            "SELECT COUNT(*) FROM prepared_pattern_candidates WHERE generation_id=? AND precision_eligible=1",
            (generation_id,),
        ).fetchone()[0])

        invalid_precision = int(connection.execute(
            """
            SELECT COUNT(*)
            FROM prepared_pattern_candidates
            WHERE generation_id=? AND precision_eligible=1
              AND (
                recommendation_eligible<>1
                OR policy_premium_eligible<>1
                OR UPPER(TRIM(COALESCE(value_tier,'')))<>'STRONG_VALUE'
                OR selection_odd<1.50
                OR precision_probability IS NULL
                OR precision_probability<0.75
              )
            """,
            (generation_id,),
        ).fetchone()[0])
        if invalid_precision:
            raise SystemExit(f"Rules-only precision invariant failed: {invalid_precision}")

        placeholders = ",".join("?" for _ in RETIRED_SUB_MARKETS)
        retired = int(connection.execute(
            f"""
            SELECT COUNT(*)
            FROM prepared_pattern_candidates c
            JOIN prepared_selections s
              ON s.competition_id=c.competition_id
             AND s.snapshot_version=c.snapshot_version
             AND s.selection_key=c.selection_key
            WHERE c.generation_id=?
              AND s.identity_sub_market_key IN ({placeholders})
            """,
            (generation_id, *sorted(RETIRED_SUB_MARKETS)),
        ).fetchone()[0])
        if retired:
            raise SystemExit(f"Retired markets leaked into v8 generation: {retired}")

        probability_mismatches = int(connection.execute(
            """
            SELECT COUNT(*)
            FROM prepared_pattern_candidates c
            JOIN prepared_selections s
              ON s.competition_id=c.competition_id
             AND s.snapshot_version=c.snapshot_version
             AND s.selection_key=c.selection_key
            WHERE c.generation_id=? AND c.precision_eligible=1
              AND (
                c.precision_probability IS NULL
                OR (
                    s.opponent_model_probability IS NOT NULL
                    AND ABS(c.precision_probability-s.opponent_model_probability)>0.000000001
                )
                OR (
                    s.opponent_model_probability IS NULL
                    AND (
                        s.value_signal_conservative_probability IS NULL
                        OR s.value_signal_market_probability IS NULL
                        OR s.value_signal_conservative_probability<=s.value_signal_market_probability
                        OR ABS(s.value_signal_conservative_probability-s.bm_posterior_probability)<=0.000000001
                        OR ABS(c.precision_probability-s.value_signal_conservative_probability)>0.000000001
                    )
                )
              )
            """,
            (generation_id,),
        ).fetchone()[0])
        if probability_mismatches:
            raise SystemExit(
                f"Rules-only independent probability parity failed: {probability_mismatches}"
            )

        performance = int(connection.execute(
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
            WHERE c.generation_id=? AND c.precision_eligible=1
            """,
            (generation_id,),
        ).fetchone()[0])
        if performance != precision:
            raise SystemExit(
                f"Rules-only Performance join mismatch precision={precision} rows={performance}"
            )

        context = connection.execute(
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
              AND c.competition_id='domestic'
            """,
            (generation_id,),
        ).fetchone()
        opponent_required = int(context[0] or 0)
        opponent_models = int(context[1] or 0)
        favorite_shadow = int(context[2] or 0)
        if opponent_required > 0 and opponent_models <= 0:
            raise SystemExit("Rules-only Domestic context has no opponent model")
        if opponent_models > 0 and favorite_shadow <= 0:
            raise SystemExit("Rules-only Domestic context has no Favorite shadow")

        return {
            "schemaVersion": schema,
            "generationId": generation_id,
            "sourceFingerprint": source_fingerprint,
            "candidateCount": actual,
            "recommendationEligibleCount": eligible,
            "precisionEligibleCount": precision,
            "performanceRowCount": performance,
            "opponentAdjustedRequiredCount": opponent_required,
            "opponentModelCount": opponent_models,
            "favoriteShadowCount": favorite_shadow,
        }
    finally:
        connection.close()


def main() -> None:
    if not PUBLISHED_MANIFEST.is_file():
        raise SystemExit("Rules-only migration requires a published App-Ready manifest")
    published = json.loads(PUBLISHED_MANIFEST.read_text(encoding="utf-8"))
    metadata = dict(published.get("metadata") or {})
    if int(metadata.get("preparedBettingSchemaVersion") or 0) < SCHEMA_VERSION:
        raise SystemExit("Rules-only migration requires a verified schema-v13 source")

    stats_artifact, stats_zip = verified_artifact(published, "app_ready_stats_bundle")
    _, betting_zip = verified_artifact(published, "app_ready_betting_bundle")

    with tempfile.TemporaryDirectory(prefix="statmaker-rules-seed-") as temp:
        seed = Path(temp) / "seed"
        extract_verified_source(stats_zip, betting_zip, seed)
        normalize_ready_snapshots(seed / "databases" / "statmaker_prepared_betting.db")
        shutil.rmtree(ROOT / "app-ready-export", ignore_errors=True)
        pattern_materializer.materialize(seed, RAW)
        fixture_materializer.materialize(RAW / "databases" / "statmaker_prepared_betting.db")

    prepared_db = RAW / "databases" / "statmaker_prepared_betting.db"
    finalize_sqlite_for_bundle(prepared_db)
    rules_meta = validate_rules_generation(prepared_db)

    OUT.mkdir(parents=True, exist_ok=True)
    stats_out = OUT / stats_zip.name
    shutil.copy2(stats_zip, stats_out)
    if sha256(stats_out) != str(stats_artifact.get("sha256") or ""):
        raise SystemExit("Rules-only reused stats bundle SHA mismatch")

    betting = bundle_betting(RAW)
    betting_zip = OUT / Path(betting["path"]).name
    validate_betting_zip(betting_zip, rules_meta["generationId"])
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    main_version = str(metadata.get("mainContentVersion") or "")
    uefa_version = str(metadata.get("uefaContentVersion") or "")
    domestic_fp = str(metadata.get("domesticHistoryFingerprint") or "")
    support_fp = str(metadata.get("uefaSupportFingerprint") or "")
    if not all((main_version, uefa_version, domestic_fp, support_fp)):
        raise SystemExit("Published App-Ready metadata is missing source fingerprints")

    stats = dict(stats_artifact)
    manifest_metadata = dict(metadata)
    manifest_metadata.update(
        {
            "preparedBettingSchemaVersion": rules_meta["schemaVersion"],
            "preparedPatternGenerationId": rules_meta["generationId"],
            "preparedPatternSourceFingerprint": rules_meta["sourceFingerprint"],
            "preparedPatternRulesFingerprint": RULES_FINGERPRINT,
            "preparedPatternCandidateCount": rules_meta["candidateCount"],
            "preparedPatternRecommendationEligibleCount":
                rules_meta["recommendationEligibleCount"],
            "preparedPrecisionSinglesEligibleCount": rules_meta["precisionEligibleCount"],
            "preparedPerformanceRowCount": rules_meta["performanceRowCount"],
            "preparedOpponentAdjustedRequiredCount":
                rules_meta["opponentAdjustedRequiredCount"],
            "preparedOpponentModelCount": rules_meta["opponentModelCount"],
            "preparedFavoriteShadowCount": rules_meta["favoriteShadowCount"],
            "rulesOnlySourceReuse": True,
        }
    )
    seed_value = "\n".join(
        sorted(
            (
                f"main|{main_version}",
                f"uefa|{uefa_version}",
                f"stats|{stats['sha256']}",
                f"betting|{betting['sha256']}",
                f"domestic|{domestic_fp}",
                f"support|{support_fp}",
            )
        )
    )
    manifest = {
        "schemaVersion": 1,
        "profile": "app_ready",
        "contentVersion": hashlib.sha256(seed_value.encode()).hexdigest(),
        "generatedAt": now,
        "artifactCount": 2,
        "artifacts": [stats, betting],
        "metadata": manifest_metadata,
    }
    (OUT / "update_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        "APP_READY_RULES_ONLY_OK",
        f"source_main={main_version[:12]}",
        f"generation={rules_meta['generationId']}",
        f"candidates={rules_meta['candidateCount']}",
        f"eligible={rules_meta['recommendationEligibleCount']}",
        f"precision={rules_meta['precisionEligibleCount']}",
        f"performance={rules_meta['performanceRowCount']}",
    )


if __name__ == "__main__":
    main()
