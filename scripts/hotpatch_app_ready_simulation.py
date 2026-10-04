#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

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


def main() -> int:
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
                "10000",
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
                "10000",
            ],
            cwd=ROOT,
            check=True,
        )

        con = sqlite3.connect(db_path)
        try:
            rows = int(con.execute("SELECT COUNT(*) FROM prepared_simulations").fetchone()[0])
            match_rows = int(con.execute("SELECT COUNT(*) FROM prepared_match_simulations").fetchone()[0])
            match_elo_rows = int(con.execute(
                "SELECT COUNT(*) FROM prepared_match_simulations "
                "WHERE simulation_model='match-monte-carlo-v2-elo' "
                "AND elo_model_version='team-elo-v1'"
            ).fetchone()[0])
            match_explorer_rows = int(con.execute(
                "SELECT COUNT(*) FROM prepared_match_explorer_simulations"
            ).fetchone()[0])
            runs = int(con.execute("SELECT COALESCE(MAX(simulation_runs),0) FROM prepared_simulations").fetchone()[0])
            elo_rows = int(con.execute("SELECT COUNT(*) FROM prepared_team_elo").fetchone()[0])
            elo_meta_rows = int(con.execute("SELECT COUNT(*) FROM prepared_team_elo_meta").fetchone()[0])
            league_rows = int(con.execute("SELECT COUNT(*) FROM prepared_league_simulation_meta").fetchone()[0])
            league_team_rows = int(con.execute("SELECT COUNT(*) FROM prepared_league_team_simulations").fetchone()[0])
            league_runs = int(con.execute("SELECT COALESCE(MAX(simulation_runs),0) FROM prepared_league_simulation_meta").fetchone()[0])
            quick = con.execute("PRAGMA quick_check").fetchone()
            if not quick or quick[0] != "ok":
                raise SystemExit(f"Hot-patched DB quick_check failed: {quick}")
        finally:
            con.close()

        if rows <= 0 or match_rows <= 0 or runs < 10000:
            raise SystemExit(
                f"Invalid hot-patched simulation rows={rows} match_rows={match_rows} runs={runs}"
            )
        if match_elo_rows != match_rows or match_explorer_rows <= 0:
            raise SystemExit(
                "Invalid Elo-backed Match Simulation "
                f"match_rows={match_rows} elo_match_rows={match_elo_rows} "
                f"explorer_rows={match_explorer_rows}"
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
    metadata["preparedMatchSimulationContract"] = "match-monte-carlo-v2-elo"
    metadata["preparedMatchSimulationExplorerRowCount"] = match_explorer_rows
    metadata["preparedMatchSimulationUsesPreparedElo"] = True
    metadata["preparedTeamEloContract"] = "team-elo-v1"
    metadata["preparedTeamEloCount"] = elo_rows
    metadata["preparedTeamEloLeagueCount"] = elo_meta_rows
    metadata["preparedLeagueSimulationContract"] = "league-season-monte-carlo-v3-all-domestic-elo"
    metadata["preparedLeagueSimulationCount"] = league_rows
    metadata["preparedLeagueSimulationTeamCount"] = league_team_rows
    metadata["preparedLeagueSimulationRuns"] = league_runs
    metadata["simulationHotPublish"] = True

    payload["generatedAt"] = now
    seed = "|".join(
        [
            str(payload.get("contentVersion") or ""), new_sha,
            str(rows), str(match_rows), str(match_explorer_rows), str(runs),
            str(elo_rows), str(elo_meta_rows),
            str(league_rows), str(league_team_rows), str(league_runs), now
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
        f"runs={runs}",
        f"elo_rows={elo_rows}",
        f"elo_meta_rows={elo_meta_rows}",
        f"league_rows={league_rows}",
        f"league_team_rows={league_team_rows}",
        f"league_runs={league_runs}",
        f"bundle={new_name}",
        f"sha256={new_sha}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
