#!/usr/bin/env python3
"""Exercise every cached league format before starting the expensive Android build.

Uses the same Elo and league materializers with a small deterministic run count.
The temporary database is deleted; no app artifact or provider request is created.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import materialize_prepared_league_simulations as simulation


def validate(runs: int = 25) -> tuple[int, int]:
    index = json.loads(simulation.INDEX_PATH.read_text(encoding="utf-8"))
    entries = simulation.current_entries(index)
    if not entries:
        raise SystemExit("LEAGUE_SIMULATION_PREFLIGHT_FAILED no current cached leagues")
    teams = 0
    with tempfile.TemporaryDirectory(prefix="statmaker-league-preflight-") as directory:
        database = Path(directory) / "preflight.db"
        sqlite3.connect(database).close()
        subprocess.run(
            [sys.executable, str(Path(__file__).with_name("materialize_prepared_team_elo.py")), str(database)],
            cwd=simulation.ROOT, check=True,
        )
        with sqlite3.connect(database) as connection:
            for entry in entries:
                code = str(entry.get("league_code") or "")
                season = str(entry.get("app_season") or entry.get("target_app_season") or "")
                scope = simulation.load_elo_scope(connection, code, season)
                payload = simulation.load_fixture_payload(entry)
                if scope is None or payload is None:
                    raise SystemExit(f"LEAGUE_SIMULATION_PREFLIGHT_FAILED {code}@{season}: missing cached Elo/fixtures")
                fixtures = simulation.current_season_fixtures(entry, payload)
                result = simulation.simulate_league(entry, fixtures, runs, scope)
                if result is None:
                    raise SystemExit(f"LEAGUE_SIMULATION_PREFLIGHT_FAILED {code}@{season}: league simulation contract failed")
                meta, rows = result
                if not rows or len(rows) != meta["team_count"]:
                    raise SystemExit(f"LEAGUE_SIMULATION_PREFLIGHT_FAILED {code}@{season}: inconsistent team count")
                for row in rows:
                    positions = json.loads(row["position_probabilities_json"])
                    if len(positions) != len(rows) or not math.isclose(sum(positions), 1.0, abs_tol=1e-9):
                        raise SystemExit(f"LEAGUE_SIMULATION_PREFLIGHT_FAILED {code}@{season}: position probability mass for {row['team_name']}")
                teams += len(rows)
                print(f"LEAGUE_SIMULATION_PREFLIGHT_SCOPE_OK league={code} season={season} teams={len(rows)} fixtures={len(fixtures)}", flush=True)
    return len(entries), teams


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=25)
    args = parser.parse_args()
    if not 1 <= args.runs <= 1000:
        parser.error("--runs must be between 1 and 1000; this is a disposable input preflight")
    leagues, teams = validate(args.runs)
    print(f"LEAGUE_SIMULATION_PREFLIGHT_OK leagues={leagues} teams={teams} diagnostic_runs={args.runs} providerCalls=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
