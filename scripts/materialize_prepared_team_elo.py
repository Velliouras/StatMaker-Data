#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from materialize_prepared_simulations import FINISHED_STATUSES, INDEX_PATH, ROOT, finite, norm

MODEL_VERSION = "team-elo-v1"
RATING_SCALE = 400.0
HOME_ADVANTAGE = 65.0
HISTORICAL_K = 24.0
CURRENT_K = 18.0
SEASON_RETENTION = 0.85
NEW_TEAM_PENALTY = 55.0

# Ratings are globally comparable enough for promotion/relegation carry-over because
# lower divisions start below their country's top-flight baseline.
LEAGUE_BASELINES = {
    "AUT": 1500.0, "AUT2": 1400.0,
    "B1": 1500.0,
    "CRO": 1500.0,
    "CYP": 1500.0,
    "EGY": 1500.0,
    "E0": 1500.0, "E1": 1400.0, "E2": 1325.0, "E3": 1260.0, "EC": 1200.0,
    "F1": 1500.0, "F2": 1400.0,
    "D1": 1500.0, "D2": 1400.0,
    "G1": 1500.0,
    "ISR": 1500.0,
    "I1": 1500.0, "I2": 1400.0,
    "JPN": 1500.0,
    "N1": 1500.0,
    "P1": 1500.0,
    "SAU": 1500.0,
    "SC0": 1500.0, "SC1": 1400.0, "SC2": 1325.0, "SC3": 1260.0,
    "RSA": 1500.0,
    "SP1": 1500.0, "SP2": 1400.0,
    "T1": 1500.0,
    "UKR": 1500.0,
    # Already-supported calendar-year / additional scopes retained for continuity.
    "POL": 1500.0, "RUS": 1500.0,
    "SWE": 1500.0, "SWE2": 1400.0,
    "NOR": 1500.0, "NOR2": 1400.0,
    "BRA": 1500.0, "BRA2": 1400.0,
    "CHN": 1500.0,
    "CHL": 1500.0,
    "EST": 1500.0,
    "HUN": 1500.0,
    "IRL": 1500.0,
    "LVA": 1500.0,
    "LTU": 1500.0,
    "SVN": 1500.0,
    "UAE": 1500.0,
    "BGR": 1500.0,
    "CZE": 1500.0,
    "DNK": 1500.0,
    "FIN": 1500.0, "FIN2": 1400.0,
    "ISL": 1500.0,
    "ROM": 1500.0,
    "SRB": 1500.0,
    "SVK": 1500.0,
    "KOR": 1500.0,
    "SWZ": 1500.0,
    "ARG": 1500.0,
    "COL": 1500.0,
    "ECU": 1500.0,
    "MEX": 1500.0,
    "MAR": 1500.0,
    "PER": 1500.0,
    "USA": 1500.0,
    "URU": 1500.0,
}

# Active same-season scopes that are already fully cached in StatMaker-Data but are
# tagged historical_support because betting readiness/odds coverage is a separate concern.
# League Simulation is score/Elo driven and may safely use these caches without any API call.
SIMULATION_CACHE_CURRENT_CODES = {
    "BRA", "BRA2", "CHL", "CHN", "EST", "HUN", "IRL", "LVA", "LTU",
    "NOR", "NOR2", "POL", "RUS", "SWE", "SWE2", "SVN", "UAE",
    "BGR", "CZE", "DNK", "FIN", "FIN2", "ISL", "ROM", "SRB", "SVK",
    "KOR", "SWZ",
    "ARG", "COL", "ECU", "MEX", "MAR", "PER", "USA", "URU",
}

# Current participants that may not yet appear in a newly-started fixture cache.
# Keep these explicit: they are part of the competition, not synthetic clubs.
EXTRA_CURRENT_TEAMS: dict[str, tuple[str, ...]] = {
    "MAR": ("FAR Rabat",),
}


def is_simulation_current_entry(row: dict[str, Any]) -> bool:
    code = str(row.get("league_code") or "")
    role = str(row.get("stats_role") or "")
    lifecycle = str(row.get("lifecycle") or "")
    app_season = str(row.get("app_season") or "")
    target_app_season = str(row.get("target_app_season") or "")
    return (
        lifecycle == "active"
        and (
            role == "current_target"
            or (
                code in SIMULATION_CACHE_CURRENT_CODES
                and role == "historical_support"
                and app_season
                and app_season == target_app_season
            )
        )
    )


@dataclass
class RatingState:
    team_key: str
    team_name: str
    rating: float
    matches: int
    league_code: str
    season: str


def league_baseline(code: str) -> float:
    return LEAGUE_BASELINES.get(str(code or ""), 1500.0)


def load_payload(entry: dict[str, Any]) -> dict[str, Any] | None:
    path = ROOT / str(entry.get("cache_path") or "")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def fixture_date(row: dict[str, Any]) -> str:
    return str(row.get("date") or row.get("date_utc") or "")[:10]


def completed_fixtures(
    payload: dict[str, Any],
    start_date: str = "",
    end_date: str = "",
) -> list[dict[str, Any]]:
    rows = []
    for row in (payload.get("fixtures") or []):
        if not isinstance(row, dict):
            continue
        if str(row.get("status") or "").upper() not in FINISHED_STATUSES:
            continue
        if finite(row.get("home_goals")) is None or finite(row.get("away_goals")) is None:
            continue
        date = fixture_date(row)
        if start_date and date and date < start_date:
            continue
        if end_date and date and date > end_date:
            continue
        rows.append(row)
    rows.sort(key=lambda row: (fixture_date(row), int(row.get("fixture_id") or 0)))
    return rows


def expected_home_score(home_rating: float, away_rating: float) -> float:
    exponent = (away_rating - (home_rating + HOME_ADVANTAGE)) / RATING_SCALE
    return 1.0 / (1.0 + 10.0 ** exponent)


def actual_home_score(home_goals: int, away_goals: int) -> float:
    if home_goals > away_goals:
        return 1.0
    if home_goals < away_goals:
        return 0.0
    return 0.5


def margin_multiplier(home_goals: int, away_goals: int) -> float:
    margin = abs(home_goals - away_goals)
    if margin <= 1:
        return 1.0
    return 1.0 + min(0.50, 0.12 * (margin - 1))


def season_start_rating(
    previous: RatingState | None,
    target_baseline: float,
    new_team_penalty: bool,
) -> float:
    if previous is None:
        return target_baseline - (NEW_TEAM_PENALTY if new_team_penalty else 0.0)
    return target_baseline + SEASON_RETENTION * (previous.rating - target_baseline)


def run_entry(
    entry: dict[str, Any],
    previous_by_team: dict[str, RatingState],
    k_factor: float,
    new_team_penalty: bool,
    current_season_only: bool = False,
) -> dict[str, RatingState]:
    payload = load_payload(entry)
    if payload is None:
        return {}
    fixtures = completed_fixtures(
        payload,
        start_date=str(entry.get("target_season_start") or "") if current_season_only else "",
        end_date=str(entry.get("target_season_end") or "") if current_season_only else "",
    )
    if not fixtures:
        return {}

    code = str(entry.get("league_code") or "")
    season = str(entry.get("app_season") or entry.get("target_app_season") or "")
    baseline = league_baseline(code)

    names: dict[str, str] = {}
    for fixture in fixtures:
        for field in ("home_team", "away_team"):
            name = str(fixture.get(field) or "").strip()
            key = norm(name)
            if key:
                names[key] = name

    for extra_name in EXTRA_CURRENT_TEAMS.get(code, ()):
        extra_key = norm(extra_name)
        if extra_key:
            names.setdefault(extra_key, extra_name)

    ratings: dict[str, float] = {
        key: season_start_rating(previous_by_team.get(key), baseline, new_team_penalty)
        for key in names
    }
    matches = {key: 0 for key in names}

    for fixture in fixtures:
        home_name = str(fixture.get("home_team") or "").strip()
        away_name = str(fixture.get("away_team") or "").strip()
        home = norm(home_name)
        away = norm(away_name)
        if not home or not away or home == away:
            continue
        if home not in ratings:
            ratings[home] = season_start_rating(previous_by_team.get(home), baseline, new_team_penalty)
            names[home] = home_name
            matches[home] = 0
        if away not in ratings:
            ratings[away] = season_start_rating(previous_by_team.get(away), baseline, new_team_penalty)
            names[away] = away_name
            matches[away] = 0

        hg = int(float(fixture.get("home_goals")))
        ag = int(float(fixture.get("away_goals")))
        expected = expected_home_score(ratings[home], ratings[away])
        actual = actual_home_score(hg, ag)
        change = k_factor * margin_multiplier(hg, ag) * (actual - expected)

        ratings[home] += change
        ratings[away] -= change
        matches[home] += 1
        matches[away] += 1

    return {
        key: RatingState(
            team_key=key,
            team_name=names.get(key, key),
            rating=rating,
            matches=matches.get(key, 0),
            league_code=code,
            season=season,
        )
        for key, rating in ratings.items()
    }


def historical_entries(index: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [
        row for row in (index.get("leagues") or [])
        if str(row.get("league_code") or "") in LEAGUE_BASELINES
        and str(row.get("stats_role") or "") == "historical_support"
        and str(row.get("lifecycle") or "") == "active"
        and not is_simulation_current_entry(row)
        and (ROOT / str(row.get("cache_path") or "")).is_file()
        and int(row.get("completed_fixtures") or 0) >= 20
    ]
    return sorted(rows, key=lambda row: (
        str(row.get("app_season") or ""),
        str(row.get("country") or ""),
        str(row.get("league_code") or ""),
    ))


def current_entries(index: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [
        row for row in (index.get("leagues") or [])
        if str(row.get("league_code") or "") in LEAGUE_BASELINES
        and is_simulation_current_entry(row)
        and (ROOT / str(row.get("cache_path") or "")).is_file()
        and int(row.get("completed_fixtures") or 0) >= 10
    ]
    return sorted(rows, key=lambda row: (
        str(row.get("country") or ""),
        str(row.get("league_code") or ""),
    ))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("db", type=Path)
    args = parser.parse_args()
    if not args.db.is_file():
        raise SystemExit(f"Prepared DB missing: {args.db}")

    index = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    history = historical_entries(index)
    current = current_entries(index)
    if not history:
        raise SystemExit("No Elo historical support entries")
    if not current:
        raise SystemExit("No Elo current target entries")

    # Build a reusable cross-division prior. Each completed historical season advances
    # ratings chronologically; team movement between divisions therefore carries strength.
    prior_by_team: dict[str, RatingState] = {}
    historical_matches_by_team: dict[str, int] = {}

    for season in sorted({str(row.get("app_season") or "") for row in history}):
        season_states: dict[str, RatingState] = {}
        for entry in [row for row in history if str(row.get("app_season") or "") == season]:
            states = run_entry(
                entry=entry,
                previous_by_team=prior_by_team,
                k_factor=HISTORICAL_K,
                new_team_penalty=False,
            )
            for key, state in states.items():
                # A team should exist in one domestic league per season. If source data ever
                # duplicates it, prefer the state with more completed matches.
                existing = season_states.get(key)
                if existing is None or state.matches > existing.matches:
                    season_states[key] = state
                historical_matches_by_team[key] = historical_matches_by_team.get(key, 0) + state.matches
        prior_by_team.update(season_states)

    generated_at_ms = int(time.time() * 1000)
    con = sqlite3.connect(args.db)
    try:
        con.executescript(
            """
            DROP TABLE IF EXISTS prepared_team_elo;
            DROP TABLE IF EXISTS prepared_team_elo_meta;

            CREATE TABLE prepared_team_elo_meta (
                league_code TEXT NOT NULL,
                country TEXT NOT NULL,
                league_name TEXT NOT NULL,
                season TEXT NOT NULL,
                team_count INTEGER NOT NULL,
                model_version TEXT NOT NULL,
                league_baseline REAL NOT NULL,
                rating_scale REAL NOT NULL,
                home_advantage REAL NOT NULL,
                historical_k REAL NOT NULL,
                current_k REAL NOT NULL,
                season_retention REAL NOT NULL,
                generated_at_ms INTEGER NOT NULL,
                PRIMARY KEY (league_code, season)
            );

            CREATE TABLE prepared_team_elo (
                league_code TEXT NOT NULL,
                season TEXT NOT NULL,
                team_key TEXT NOT NULL,
                team_name TEXT NOT NULL,
                elo_rating REAL NOT NULL,
                start_rating REAL NOT NULL,
                league_baseline REAL NOT NULL,
                previous_rating REAL,
                previous_league_code TEXT,
                previous_season TEXT,
                historical_matches INTEGER NOT NULL,
                current_matches INTEGER NOT NULL,
                generated_at_ms INTEGER NOT NULL,
                PRIMARY KEY (league_code, season, team_key)
            );
            CREATE INDEX idx_prepared_team_elo_rating
              ON prepared_team_elo(league_code, season, elo_rating DESC);
            """
        )

        league_count = 0
        team_count = 0
        e0_sample: list[tuple[str, float, float, int]] = []

        for entry in current:
            code = str(entry.get("league_code") or "")
            season = str(entry.get("app_season") or entry.get("target_app_season") or "")
            baseline = league_baseline(code)

            states = run_entry(
                entry=entry,
                previous_by_team=prior_by_team,
                k_factor=CURRENT_K,
                new_team_penalty=True,
                current_season_only=True,
            )
            if not states:
                continue

            rows: list[tuple[Any, ...]] = []
            for key, state in states.items():
                previous = prior_by_team.get(key)
                start = season_start_rating(previous, baseline, True)
                rows.append(
                    (
                        code,
                        season,
                        key,
                        state.team_name,
                        state.rating,
                        start,
                        baseline,
                        previous.rating if previous is not None else None,
                        previous.league_code if previous is not None else None,
                        previous.season if previous is not None else None,
                        historical_matches_by_team.get(key, 0),
                        state.matches,
                        generated_at_ms,
                    )
                )
                if code == "E0":
                    e0_sample.append((state.team_name, state.rating, start, state.matches))

            con.execute(
                """
                INSERT INTO prepared_team_elo_meta(
                    league_code,country,league_name,season,team_count,model_version,
                    league_baseline,rating_scale,home_advantage,historical_k,current_k,
                    season_retention,generated_at_ms
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    code,
                    str(entry.get("country") or ""),
                    str(entry.get("league") or ""),
                    season,
                    len(rows),
                    MODEL_VERSION,
                    baseline,
                    RATING_SCALE,
                    HOME_ADVANTAGE,
                    HISTORICAL_K,
                    CURRENT_K,
                    SEASON_RETENTION,
                    generated_at_ms,
                ),
            )
            con.executemany(
                """
                INSERT INTO prepared_team_elo(
                    league_code,season,team_key,team_name,elo_rating,start_rating,
                    league_baseline,previous_rating,previous_league_code,previous_season,
                    historical_matches,current_matches,generated_at_ms
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                rows,
            )
            league_count += 1
            team_count += len(rows)

        con.commit()
        quick = con.execute("PRAGMA quick_check").fetchone()
        if not quick or quick[0] != "ok":
            raise SystemExit(f"Prepared DB quick_check failed after Elo: {quick}")
        if league_count <= 0 or team_count <= 0:
            raise SystemExit("No Elo ratings were materialized")

        print(
            "PREPARED_TEAM_ELO_OK",
            f"model={MODEL_VERSION}",
            f"leagues={league_count}",
            f"teams={team_count}",
            f"home_advantage={HOME_ADVANTAGE:.0f}",
            f"season_retention={SEASON_RETENTION:.2f}",
        )
        if e0_sample:
            top = sorted(e0_sample, key=lambda row: row[1], reverse=True)[:10]
            print(
                "ELO_E0_TOP",
                " | ".join(
                    f"{name}={rating:.1f}(start={start:.1f},current_matches={matches})"
                    for name, rating, start, matches in top
                ),
            )
    finally:
        con.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
