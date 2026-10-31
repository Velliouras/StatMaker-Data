#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from simulation_precision import MATCH_SIMULATION_RUNS, SEASON_SIMULATION_RUNS
from season_simulation_audit import MATCH_TABLES, protected_digest, validate_precision, validate_match_precision

ROOT = Path(__file__).resolve().parents[1]
APP_READY = ROOT / "data/statmaker/app_ready"
MANIFEST = APP_READY / "update_manifest.json"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def deterministic_zip(source: Path, target: Path) -> None:
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(p for p in source.rglob("*") if p.is_file()):
            rel = path.relative_to(source).as_posix()
            info = zipfile.ZipInfo(rel, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            zf.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def run_uefa_materializer(db_path: Path, *, cached: bool | None = None, matches_only: bool = False, league_only: bool = False) -> None:
    if cached is None:
        cached = os.environ.get("CACHED_RECOVERY", "").strip().lower() == "true"
    command = ["python", str(ROOT / "scripts/materialize_prepared_uefa_simulations.py"),
               str(db_path), "--runs", str(SEASON_SIMULATION_RUNS),
               "--match-runs", str(MATCH_SIMULATION_RUNS)]
    if matches_only and league_only:
        raise ValueError("Choose match-only or league-only UEFA simulation")
    if league_only:
        command.append("--league-only")
    if matches_only:
        command.append("--matches-only")
    elif cached:
        command.append("--cached-league-tables")
    subprocess.run(command, cwd=ROOT, check=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--score-edge-only", action="store_true",
        help="Re-materialize Score Edge from the existing 10k simulation rows only",
    )
    parser.add_argument(
        "--uefa-match-only", action="store_true",
        help="Add UEFA explorer match rows from published ratings; no API/engine refresh",
    )
    parser.add_argument("--uefa-only", action="store_true",
                        help="Refresh UEFA league/match simulation from published tables; no API calls")
    parser.add_argument("--season-only", action="store_true", help="Increase season precision without modifying matches or recommendation data")
    parser.add_argument("--match-precision-only", action="store_true", help="Increase all match simulation distributions without rebuilding seasons, fixtures or ratings")
    args = parser.parse_args()
    if sum((args.uefa_match_only, args.score_edge_only, args.uefa_only, args.season_only, args.match_precision_only)) > 1:
        parser.error("Choose only one targeted simulation update")
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    artifacts = payload.get("artifacts") or []
    betting = next((x for x in artifacts if x.get("id") == "app_ready_betting_bundle"), None)
    if not betting:
        raise SystemExit("Current App-Ready manifest has no betting bundle")

    old_bundle = ROOT / str(betting["path"])
    if not old_bundle.is_file():
        raise SystemExit(f"Current betting bundle missing: {old_bundle}")

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    with tempfile.TemporaryDirectory() as td:
        temp = Path(td)
        extracted = temp / "bundle"
        extracted.mkdir()
        with zipfile.ZipFile(old_bundle) as zf:
            zf.extractall(extracted)

        db_path = extracted / "databases/statmaker_prepared_betting.db"
        bundle_manifest_path = extracted / "bundle_manifest.json"
        if not db_path.is_file() or not bundle_manifest_path.is_file():
            raise SystemExit("Betting bundle is missing DB or bundle_manifest.json")

        precision_before = protected_digest(db_path) if args.season_only else None
        match_before = protected_digest(db_path, MATCH_TABLES) if args.match_precision_only else None
        precision_proof = None
        match_proof = None
        if args.season_only:
            started = time.perf_counter()
            subprocess.run(["python", str(ROOT / "scripts/materialize_prepared_league_simulations.py"),
                            str(db_path), "--runs", str(SEASON_SIMULATION_RUNS)], cwd=ROOT, check=True)
            print(f"SEASON_PRECISION_TIMING stage=domestic runs={SEASON_SIMULATION_RUNS} seconds={time.perf_counter()-started:.3f}", flush=True)
            started = time.perf_counter()
            run_uefa_materializer(db_path, cached=True, league_only=True)
            print(f"SEASON_PRECISION_TIMING stage=uefa runs={SEASON_SIMULATION_RUNS} seconds={time.perf_counter()-started:.3f}", flush=True)
            if precision_before != protected_digest(db_path):
                raise ValueError("Season precision refresh modified protected match/history/recommendation tables")
            precision_proof = validate_precision(db_path, SEASON_SIMULATION_RUNS)
            print("SEASON_PRECISION_VERIFIED", precision_proof, flush=True)
        elif args.match_precision_only:
            started = time.perf_counter()
            subprocess.run(["python", str(ROOT / "scripts/materialize_prepared_simulations.py"),
                            str(db_path), "--runs", str(MATCH_SIMULATION_RUNS)], cwd=ROOT, check=True)
            run_uefa_materializer(db_path, matches_only=True)
            print(f"MATCH_PRECISION_TIMING runs={MATCH_SIMULATION_RUNS} seconds={time.perf_counter()-started:.3f}", flush=True)
        elif args.uefa_only:
            run_uefa_materializer(db_path, cached=True)
        elif args.uefa_match_only:
            run_uefa_materializer(db_path, matches_only=True)
        elif not args.score_edge_only:
            # Refresh the fixture index first so schedule-only upcoming domestic fixtures
            # become eligible for Match Simulation even when bookmaker selections are absent.
            subprocess.run(
                [
                    "python",
                    str(ROOT / "scripts/materialize_prepared_fixture_index.py"),
                    str(db_path),
                ],
                cwd=ROOT,
                check=True,
            )

            # Canonical Elo must be refreshed first. Match Simulation and League Simulation
            # both consume the same prepared_team_elo contract from this exact DB snapshot.
            subprocess.run(
                [
                    "python",
                    str(ROOT / "scripts/materialize_prepared_team_elo.py"),
                    str(db_path),
                ],
                cwd=ROOT,
                check=True,
            )
            subprocess.run(
                [
                    "python",
                    str(ROOT / "scripts/materialize_prepared_simulations.py"),
                    str(db_path),
                    "--runs",
                    str(MATCH_SIMULATION_RUNS),
                ],
                cwd=ROOT,
                check=True,
            )
            subprocess.run(
                [
                    "python",
                    str(ROOT / "scripts/materialize_prepared_league_simulations.py"),
                    str(db_path),
                    "--runs",
                    str(SEASON_SIMULATION_RUNS),
                ],
                cwd=ROOT,
                check=True,
            )
            run_uefa_materializer(db_path)


        # Strictly targeted: reuse the 10k draws already stored in this App-Ready DB.
        if not args.uefa_match_only and not args.uefa_only and not args.season_only:
            subprocess.run(
                ["python", str(ROOT / "scripts/materialize_prepared_score_edge.py"),
                 str(db_path)], cwd=ROOT, check=True,
            )

        if args.match_precision_only:
            if match_before != protected_digest(db_path, MATCH_TABLES):
                raise ValueError("Match precision refresh modified protected seasons, statistics, ratings or recommendation tables")
            match_proof = validate_match_precision(db_path, MATCH_SIMULATION_RUNS)
            print("MATCH_PRECISION_VERIFIED", match_proof, flush=True)

        con = sqlite3.connect(db_path)
        try:
            rows = int(con.execute("SELECT COUNT(*) FROM prepared_simulations").fetchone()[0])
            match_rows = int(con.execute("SELECT COUNT(*) FROM prepared_match_simulations").fetchone()[0])
            match_runs = int(con.execute("SELECT COALESCE(MIN(simulation_runs),0) FROM prepared_match_simulations").fetchone()[0])
            match_elo_rows = int(con.execute(
                "SELECT COUNT(*) FROM prepared_match_simulations "
                "WHERE simulation_model='match-monte-carlo-v2-elo' "
                "AND elo_model_version='team-elo-v1'"
            ).fetchone()[0])
            match_explorer_rows = int(con.execute(
                "SELECT COUNT(*) FROM prepared_match_explorer_simulations"
            ).fetchone()[0])
            uefa_match_rows = int(con.execute(
                "SELECT COUNT(*) FROM prepared_match_simulations "
                "WHERE competition_id IN ('champions_league','europa_league','conference_league') "
                "AND simulation_model='uefa-match-monte-carlo-v1' "
                "AND elo_model_version='uefa-seeding-elo-v1' AND simulation_runs>=10000"
            ).fetchone()[0])
            match_score_distribution_rows = int(con.execute(
                "SELECT COUNT(*) FROM prepared_match_simulations "
                "WHERE score_distribution_json IS NOT NULL "
                "AND LENGTH(TRIM(score_distribution_json)) > 2"
            ).fetchone()[0])
            runs = int(con.execute("SELECT COALESCE(MAX(simulation_runs),0) FROM prepared_simulations").fetchone()[0])
            elo_rows = int(con.execute("SELECT COUNT(*) FROM prepared_team_elo").fetchone()[0])
            elo_meta_rows = int(con.execute("SELECT COUNT(*) FROM prepared_team_elo_meta").fetchone()[0])
            league_rows = int(con.execute("SELECT COUNT(*) FROM prepared_league_simulation_meta").fetchone()[0])
            league_team_rows = int(con.execute("SELECT COUNT(*) FROM prepared_league_team_simulations").fetchone()[0])
            league_runs = int(con.execute("SELECT COALESCE(MAX(simulation_runs),0) FROM prepared_league_simulation_meta").fetchone()[0])
            uefa_rows = int(con.execute("SELECT COUNT(*) FROM prepared_uefa_simulation_meta").fetchone()[0])
            uefa_team_rows = int(con.execute("SELECT COUNT(*) FROM prepared_uefa_team_simulations").fetchone()[0])
            uefa_runs = int(con.execute("SELECT COALESCE(MAX(simulation_runs),0) FROM prepared_uefa_simulation_meta").fetchone()[0])
            score_edge_rows = int(con.execute(
                "SELECT COUNT(*) FROM prepared_score_edge_values"
            ).fetchone()[0])
            score_edge_matches = int(con.execute(
                "SELECT COUNT(DISTINCT competition_id || '|' || snapshot_version || '|' || match_key) "
                "FROM prepared_score_edge_values"
            ).fetchone()[0])
            quick = con.execute("PRAGMA quick_check").fetchone()
            if not quick or quick[0] != "ok":
                raise SystemExit(f"Hot-patched DB quick_check failed: {quick}")
        finally:
            con.close()

        if rows <= 0 or match_rows <= 0 or runs < 10000:
            raise SystemExit(
                f"Invalid hot-patched simulation rows={rows} match_rows={match_rows} runs={runs}"
            )
        if (
            match_elo_rows + uefa_match_rows != match_rows
            or match_explorer_rows <= 0
            or match_score_distribution_rows != match_rows
        ):
            raise SystemExit(
                "Invalid Elo-backed Match Simulation "
                f"match_rows={match_rows} elo_match_rows={match_elo_rows} "
                f"explorer_rows={match_explorer_rows} "
                f"score_distribution_rows={match_score_distribution_rows}"
            )
        if elo_rows <= 0 or elo_meta_rows <= 0:
            raise SystemExit(
                f"Invalid Elo rows={elo_rows} meta_rows={elo_meta_rows}"
            )
        if league_rows <= 0 or league_team_rows <= 0 or league_runs < 10000:
            raise SystemExit(
                "Invalid league simulation "
                f"leagues={league_rows} teams={league_team_rows} runs={league_runs}"
            )
        if uefa_rows != 3 or uefa_team_rows != 108 or uefa_runs < 10000:
            raise SystemExit(
                "Invalid UEFA simulation "
                f"competitions={uefa_rows} teams={uefa_team_rows} runs={uefa_runs}"
            )

        if score_edge_rows <= 0 or score_edge_matches <= 0:
            raise SystemExit("Refusing to publish empty precomputed Score Edge rows")

        bundle_manifest = json.loads(bundle_manifest_path.read_text(encoding="utf-8"))
        found_db = False
        for item in bundle_manifest.get("files", []):
            if item.get("path") == "databases/statmaker_prepared_betting.db":
                item["bytes"] = db_path.stat().st_size
                item["sha256"] = sha256(db_path)
                found_db = True
                break
        if not found_db:
            raise SystemExit("Bundle manifest does not list prepared betting DB")
        bundle_manifest_path.write_text(
            json.dumps(bundle_manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        provisional = temp / "betting.zip"
        deterministic_zip(extracted, provisional)
        new_sha = sha256(provisional)
        new_name = f"app_ready_betting_bundle-{new_sha}.zip"
        new_bundle = APP_READY / new_name
        shutil.copy2(provisional, new_bundle)

    betting["path"] = f"data/statmaker/app_ready/{new_name}"
    betting["url"] = f"https://raw.githubusercontent.com/Velliouras/StatMaker-Data/main/{betting['path']}"
    betting["sha256"] = new_sha
    betting["bytes"] = new_bundle.stat().st_size
    betting["generatedAt"] = now

    metadata = payload.setdefault("metadata", {})
    metadata["preparedSimulationContract"] = "monte-carlo-v1-prod-data-uat-engine"
    metadata["preparedSimulationRowCount"] = rows
    metadata["preparedSimulationRuns"] = runs
    metadata["preparedMatchSimulationCount"] = match_rows
    metadata["preparedMatchSimulationRuns"] = match_runs
    metadata["preparedMatchSimulationContract"] = "match-monte-carlo-v2-elo"
    metadata["preparedMatchSimulationExplorerRowCount"] = match_explorer_rows
    metadata["preparedMatchScoreDistributionCount"] = match_score_distribution_rows
    metadata["preparedMatchScoreDistributionContract"] = "same-run-monte-carlo-exact-score-v1"
    metadata["preparedMatchSimulationUsesPreparedElo"] = True
    metadata["preparedUefaMatchSimulationContract"] = "uefa-match-monte-carlo-v1"
    metadata["preparedUefaMatchSimulationCount"] = uefa_match_rows
    metadata["preparedTeamEloContract"] = "team-elo-v1"
    metadata["preparedTeamEloCount"] = elo_rows
    metadata["preparedTeamEloLeagueCount"] = elo_meta_rows
    metadata["preparedLeagueSimulationContract"] = "league-season-monte-carlo-v3-all-domestic-elo"
    metadata["preparedLeagueSimulationCount"] = league_rows
    metadata["preparedLeagueSimulationTeamCount"] = league_team_rows
    metadata["preparedLeagueSimulationRuns"] = league_runs
    metadata["preparedUefaSimulationContract"] = "uefa-club-monte-carlo-v1"
    metadata["preparedUefaSimulationStrengthModel"] = "uefa-seeding-elo-v1"
    metadata["preparedUefaSimulationCount"] = uefa_rows
    metadata["preparedUefaSimulationTeamCount"] = uefa_team_rows
    metadata["preparedUefaSimulationRuns"] = uefa_runs
    metadata["preparedScoreEdgeContract"] = "exact-score-monte-carlo-10k-value-v1"
    metadata["preparedScoreEdgeRowCount"] = score_edge_rows
    metadata["preparedScoreEdgeMatchCount"] = score_edge_matches
    metadata["simulationHotPublish"] = True
    if match_proof is not None:
        metadata["matchSimulationPrecision"] = match_proof
    if precision_proof is not None:
        metadata["seasonSimulationPrecision"] = precision_proof
    if precision_proof is not None or match_proof is not None:
        from materialize_prepared_uefa_simulations import cached_source_fingerprint
        config = json.loads((ROOT / "config/uefa_simulation_2026_27.json").read_text())
        metadata["preparedUefaSimulationSourceFingerprint"] = cached_source_fingerprint(config)
        metadata["preparedUefaSimulationValidatedBundleSha256"] = new_sha

    payload["generatedAt"] = now
    seed = "|".join(
        [
            str(payload.get("contentVersion") or ""), new_sha,
            str(rows), str(match_rows), str(match_explorer_rows),
            str(match_score_distribution_rows), str(runs),
            str(elo_rows), str(elo_meta_rows),
            str(league_rows), str(league_team_rows), str(league_runs),
            str(uefa_rows), str(uefa_team_rows), str(uefa_runs),
            str(score_edge_rows), str(score_edge_matches), now
        ]
    )
    payload["contentVersion"] = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    MANIFEST.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(
        "APP_READY_SIMULATION_HOT_PUBLISH_OK",
        f"rows={rows}",
        f"match_rows={match_rows}",
        f"match_elo_rows={match_elo_rows}",
        f"match_explorer_rows={match_explorer_rows}",
        f"match_score_distribution_rows={match_score_distribution_rows}",
        f"runs={runs}",
        f"elo_rows={elo_rows}",
        f"elo_meta_rows={elo_meta_rows}",
        f"league_rows={league_rows}",
        f"league_team_rows={league_team_rows}",
        f"league_runs={league_runs}",
        f"uefa_rows={uefa_rows}",
        f"uefa_team_rows={uefa_team_rows}",
        f"uefa_runs={uefa_runs}",
        f"score_edge_rows={score_edge_rows}",
        f"score_edge_matches={score_edge_matches}",
        f"bundle={new_name}",
        f"sha256={new_sha}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
