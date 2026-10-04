#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from materialize_prepared_simulations import (
    FINISHED_STATUSES,
    INDEX_PATH,
    ROOT,
    LeagueHistory,
    distribution_cdf,
    finite,
    norm,
    simulate_counts,
)

DEFAULT_RUNS = 10_000
MODEL_VERSION = "league-season-monte-carlo-v2-elo"
ELO_MODEL_VERSION = "team-elo-v1"
TIE_BREAK_MODEL = "POINTS_GD_GF"

ELO_GOAL_LOG_COEFF = 0.55
ELO_WEIGHT_EARLY = 0.65
ELO_WEIGHT_FLOOR = 0.35
ELO_WEIGHT_DECAY_PER_MATCH = 0.03

# v2 remains intentionally limited to competitions whose championship can be
# represented as a standard home/away double round-robin. Competition-specific
# split/playoff rules will be added separately rather than approximated.
SAFE_DOUBLE_ROUND_ROBIN_CODES = {
    "E0", "E1", "E2", "E3", "EC",
    "D1", "D2",
    "I1", "I2",
    "SP1", "SP2",
    "F1", "F2",
    "N1", "P1", "T1",
    "POL", "RUS",
    "SWE", "SWE2",
    "NOR", "NOR2",
    "BRA", "BRA2",
    "CHN", "JPN",
}


@dataclass
class TeamStanding:
    key: str
    name: str
    logo: str | None = None
    played: int = 0
    points: int = 0
    goals_for: int = 0
    goals_against: int = 0

    @property
    def goal_difference(self) -> int:
        return self.goals_for - self.goals_against


@dataclass
class EloTeam:
    rating: float
    current_matches: int


@dataclass
class EloScope:
    model_version: str
    rating_scale: float
    home_advantage: float
    teams: dict[str, EloTeam]


def fixture_goals(fixture: dict[str, Any]) -> tuple[int, int] | None:
    home = finite(fixture.get("home_goals"))
    away = finite(fixture.get("away_goals"))
    if home is None:
        home = finite((fixture.get("goals") or {}).get("home"))
    if away is None:
        away = finite((fixture.get("goals") or {}).get("away"))
    if home is None or away is None:
        return None
    return int(home), int(away)


def team_logo_from_fixture(fixture: dict[str, Any], team_key: str) -> str | None:
    for side in fixture.get("raw_statistics") or []:
        if not isinstance(side, dict):
            continue
        team = side.get("team") or {}
        if norm(team.get("name")) != team_key:
            continue
        logo = str(team.get("logo") or "").strip()
        if logo:
            return logo
    return None


def current_entries(index: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in index.get("leagues") or []:
        if str(row.get("league_code") or "") not in SAFE_DOUBLE_ROUND_ROBIN_CODES:
            continue
        if str(row.get("stats_role") or "") != "current_target":
            continue
        if str(row.get("lifecycle") or "") != "active":
            continue
        path = ROOT / str(row.get("cache_path") or "")
        if not path.is_file():
            continue
        if int(row.get("completed_fixtures") or 0) < 10:
            continue
        rows.append(row)
    rows.sort(key=lambda row: (str(row.get("country") or ""), str(row.get("league") or "")))
    return rows


def load_fixture_payload(entry: dict[str, Any]) -> dict[str, Any] | None:
    path = ROOT / str(entry.get("cache_path") or "")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def load_elo_scope(con: sqlite3.Connection, league_code: str, season: str) -> EloScope | None:
    tables = {
        row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('prepared_team_elo','prepared_team_elo_meta')"
        )
    }
    if tables != {"prepared_team_elo", "prepared_team_elo_meta"}:
        return None

    meta = con.execute(
        """
        SELECT model_version,rating_scale,home_advantage
        FROM prepared_team_elo_meta
        WHERE league_code=? AND season=?
        """,
        (league_code, season),
    ).fetchone()
    if meta is None or str(meta[0] or "") != ELO_MODEL_VERSION:
        return None

    teams = {
        str(row[0]): EloTeam(rating=float(row[1]), current_matches=int(row[2]))
        for row in con.execute(
            """
            SELECT team_key,elo_rating,current_matches
            FROM prepared_team_elo
            WHERE league_code=? AND season=?
            """,
            (league_code, season),
        )
    }
    if not teams:
        return None
    return EloScope(
        model_version=str(meta[0]),
        rating_scale=float(meta[1]),
        home_advantage=float(meta[2]),
        teams=teams,
    )


def build_current_table(fixtures: list[dict[str, Any]]) -> tuple[dict[str, TeamStanding], set[tuple[str, str]], bool]:
    teams: dict[str, TeamStanding] = {}
    completed_pairs: set[tuple[str, str]] = set()
    duplicate_pair = False

    for fixture in fixtures:
        if str(fixture.get("status") or "").upper() not in FINISHED_STATUSES:
            continue
        home_name = str(fixture.get("home_team") or "").strip()
        away_name = str(fixture.get("away_team") or "").strip()
        home_key = norm(home_name)
        away_key = norm(away_name)
        if not home_key or not away_key or home_key == away_key:
            continue
        goals = fixture_goals(fixture)
        if goals is None:
            continue

        pair = (home_key, away_key)
        if pair in completed_pairs:
            duplicate_pair = True
        completed_pairs.add(pair)

        home = teams.setdefault(home_key, TeamStanding(home_key, home_name))
        away = teams.setdefault(away_key, TeamStanding(away_key, away_name))
        if not home.logo:
            home.logo = team_logo_from_fixture(fixture, home_key)
        if not away.logo:
            away.logo = team_logo_from_fixture(fixture, away_key)

        hg, ag = goals
        home.played += 1
        away.played += 1
        home.goals_for += hg
        home.goals_against += ag
        away.goals_for += ag
        away.goals_against += hg
        if hg > ag:
            home.points += 3
        elif hg < ag:
            away.points += 3
        else:
            home.points += 1
            away.points += 1

    return teams, completed_pairs, duplicate_pair


def current_ranking(teams: dict[str, TeamStanding]) -> list[str]:
    return sorted(
        teams,
        key=lambda key: (
            -teams[key].points,
            -teams[key].goal_difference,
            -teams[key].goals_for,
            teams[key].name.casefold(),
        ),
    )


def elo_weight(home_matches: int, away_matches: int) -> float:
    sample = min(home_matches, away_matches, 10)
    return max(ELO_WEIGHT_FLOOR, ELO_WEIGHT_EARLY - ELO_WEIGHT_DECAY_PER_MATCH * sample)


def elo_blended_goal_means(
    stat_home_mean: float,
    stat_away_mean: float,
    league_home_mean: float,
    league_away_mean: float,
    home_elo: EloTeam,
    away_elo: EloTeam,
    elo_scope: EloScope,
) -> tuple[float, float, float]:
    rating_scale = max(1.0, elo_scope.rating_scale)
    rating_diff = (home_elo.rating + elo_scope.home_advantage) - away_elo.rating
    normalized_diff = max(-1.5, min(1.5, rating_diff / rating_scale))
    log_shift = max(-0.85, min(0.85, ELO_GOAL_LOG_COEFF * normalized_diff))

    elo_home_mean = max(0.05, league_home_mean * math.exp(log_shift))
    elo_away_mean = max(0.05, league_away_mean * math.exp(-log_shift))
    weight = elo_weight(home_elo.current_matches, away_elo.current_matches)

    # Geometric blend keeps goal means positive and treats Elo as a long-term prior,
    # strongest early in the season and gradually yielding to current-season evidence.
    blended_home = math.exp(
        (1.0 - weight) * math.log(max(0.05, stat_home_mean))
        + weight * math.log(elo_home_mean)
    )
    blended_away = math.exp(
        (1.0 - weight) * math.log(max(0.05, stat_away_mean))
        + weight * math.log(elo_away_mean)
    )
    return (
        min(5.5, max(0.05, blended_home)),
        min(5.5, max(0.05, blended_away)),
        weight,
    )


def simulate_league(
    entry: dict[str, Any],
    fixtures: list[dict[str, Any]],
    runs: int,
    elo_scope: EloScope,
) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    teams, completed_pairs, duplicate_pair = build_current_table(fixtures)
    if duplicate_pair or len(teams) < 8:
        return None

    team_keys = sorted(teams)
    if any(key not in elo_scope.teams for key in team_keys):
        return None

    # A regular double round-robin has exactly one ordered home fixture for every
    # pair of distinct teams. Reconstruct the unplayed schedule from that invariant.
    expected_total = len(team_keys) * (len(team_keys) - 1)
    if len(completed_pairs) >= expected_total:
        return None

    remaining = [
        (home, away)
        for home in team_keys
        for away in team_keys
        if home != away and (home, away) not in completed_pairs
    ]
    if len(completed_pairs) + len(remaining) != expected_total:
        return None

    history = LeagueHistory(fixtures)
    league_home_mean = history.mean(history.league_home.get("goals", []))
    league_away_mean = history.mean(history.league_away.get("goals", []))
    if league_home_mean is None or league_away_mean is None:
        return None

    fixture_models: list[tuple[str, str, list[int], list[int], bool, float]] = []
    mature = 0
    elo_weight_total = 0.0

    league_code = str(entry.get("league_code") or "")
    season = str(entry.get("app_season") or entry.get("target_app_season") or "")
    for home, away in remaining:
        expected = history.expected(home, away, "goals")
        if expected is None:
            return None
        stat_home_mean, stat_away_mean, home_sample, away_sample, league_sample = expected
        if league_sample < 20:
            return None

        is_mature = home_sample >= 3 and away_sample >= 3
        if is_mature:
            mature += 1

        home_mean, away_mean, weight = elo_blended_goal_means(
            stat_home_mean=stat_home_mean,
            stat_away_mean=stat_away_mean,
            league_home_mean=league_home_mean,
            league_away_mean=league_away_mean,
            home_elo=elo_scope.teams[home],
            away_elo=elo_scope.teams[away],
            elo_scope=elo_scope,
        )
        elo_weight_total += weight

        seed = f"{league_code}|{season}|{home}|{away}|{runs}|{MODEL_VERSION}"
        rng = random.Random(int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16], 16))
        home_cdf = distribution_cdf(home_mean, None, 14)
        away_cdf = distribution_cdf(away_mean, None, 14)
        home_draws = simulate_counts(rng, home_cdf, runs)
        away_draws = simulate_counts(rng, away_cdf, runs)
        fixture_models.append((home, away, home_draws, away_draws, is_mature, weight))

    points = {key: [teams[key].points] * runs for key in team_keys}
    goals_for = {key: [teams[key].goals_for] * runs for key in team_keys}
    goals_against = {key: [teams[key].goals_against] * runs for key in team_keys}

    for home, away, home_draws, away_draws, _, _ in fixture_models:
        hp = points[home]
        ap = points[away]
        hgf = goals_for[home]
        hga = goals_against[home]
        agf = goals_for[away]
        aga = goals_against[away]
        for idx in range(runs):
            hg = home_draws[idx]
            ag = away_draws[idx]
            hgf[idx] += hg
            hga[idx] += ag
            agf[idx] += ag
            aga[idx] += hg
            if hg > ag:
                hp[idx] += 3
            elif hg < ag:
                ap[idx] += 3
            else:
                hp[idx] += 1
                ap[idx] += 1

    position_counts = {key: [0] * len(team_keys) for key in team_keys}
    point_totals = {key: 0 for key in team_keys}

    for idx in range(runs):
        ranking = sorted(
            team_keys,
            key=lambda key: (
                -points[key][idx],
                -(goals_for[key][idx] - goals_against[key][idx]),
                -goals_for[key][idx],
                teams[key].name.casefold(),
            ),
        )
        for position, key in enumerate(ranking):
            position_counts[key][position] += 1
            point_totals[key] += points[key][idx]

    current_order = current_ranking(teams)
    current_positions = {key: idx + 1 for idx, key in enumerate(current_order)}

    team_rows: list[dict[str, Any]] = []
    for key in team_keys:
        counts = position_counts[key]
        title = counts[0] / runs
        top2 = sum(counts[: min(2, len(counts))]) / runs
        top4 = sum(counts[: min(4, len(counts))]) / runs
        expected_position = sum((idx + 1) * count for idx, count in enumerate(counts)) / runs
        expected_points = point_totals[key] / runs
        team = teams[key]
        team_rows.append(
            {
                "team_key": key,
                "team_name": team.name,
                "team_logo": team.logo,
                "elo_rating": elo_scope.teams[key].rating,
                "current_position": current_positions[key],
                "current_played": team.played,
                "current_points": team.points,
                "current_goal_difference": team.goal_difference,
                "current_goals_for": team.goals_for,
                "expected_position": expected_position,
                "expected_points": expected_points,
                "title_probability": title,
                "top2_probability": top2,
                "top4_probability": top4,
                "position_probabilities_json": json.dumps(
                    [count / runs for count in counts],
                    separators=(",", ":"),
                ),
            }
        )

    meta = {
        "league_code": league_code,
        "country": str(entry.get("country") or ""),
        "league_name": str(entry.get("league") or ""),
        "season": season,
        "team_count": len(team_keys),
        "current_completed_matches": len(completed_pairs),
        "remaining_matches": len(remaining),
        "simulation_runs": runs,
        "model_version": MODEL_VERSION,
        "elo_model_version": elo_scope.model_version,
        "average_elo_weight": elo_weight_total / max(1, len(remaining)),
        "tie_break_model": TIE_BREAK_MODEL,
        "mature_fixture_count": mature,
        "total_simulated_fixtures": len(remaining),
    }
    return meta, team_rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("db", type=Path)
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    args = parser.parse_args()
    runs = max(1_000, min(50_000, int(args.runs)))

    if not args.db.is_file():
        raise SystemExit(f"Prepared DB missing: {args.db}")

    index = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    generated_at_ms = int(time.time() * 1000)

    con = sqlite3.connect(args.db)
    try:
        con.executescript(
            """
            DROP TABLE IF EXISTS prepared_league_team_simulations;
            DROP TABLE IF EXISTS prepared_league_simulation_meta;

            CREATE TABLE prepared_league_simulation_meta (
                league_code TEXT NOT NULL,
                country TEXT NOT NULL,
                league_name TEXT NOT NULL,
                season TEXT NOT NULL,
                team_count INTEGER NOT NULL,
                current_completed_matches INTEGER NOT NULL,
                remaining_matches INTEGER NOT NULL,
                simulation_runs INTEGER NOT NULL,
                model_version TEXT NOT NULL,
                elo_model_version TEXT NOT NULL,
                average_elo_weight REAL NOT NULL,
                tie_break_model TEXT NOT NULL,
                mature_fixture_count INTEGER NOT NULL,
                total_simulated_fixtures INTEGER NOT NULL,
                generated_at_ms INTEGER NOT NULL,
                PRIMARY KEY (league_code, season)
            );

            CREATE TABLE prepared_league_team_simulations (
                league_code TEXT NOT NULL,
                season TEXT NOT NULL,
                team_key TEXT NOT NULL,
                team_name TEXT NOT NULL,
                team_logo TEXT,
                elo_rating REAL NOT NULL,
                current_position INTEGER NOT NULL,
                current_played INTEGER NOT NULL,
                current_points INTEGER NOT NULL,
                current_goal_difference INTEGER NOT NULL,
                current_goals_for INTEGER NOT NULL,
                expected_position REAL NOT NULL,
                expected_points REAL NOT NULL,
                title_probability REAL NOT NULL,
                top2_probability REAL NOT NULL,
                top4_probability REAL NOT NULL,
                position_probabilities_json TEXT NOT NULL,
                generated_at_ms INTEGER NOT NULL,
                PRIMARY KEY (league_code, season, team_key)
            );
            CREATE INDEX idx_prepared_league_team_title
              ON prepared_league_team_simulations(league_code, season, title_probability DESC);
            """
        )

        league_count = 0
        team_count = 0
        skipped: list[str] = []
        e0_rows: list[dict[str, Any]] = []

        for entry in current_entries(index):
            league_code = str(entry.get("league_code") or "")
            season = str(entry.get("app_season") or entry.get("target_app_season") or "")
            elo_scope = load_elo_scope(con, league_code, season)
            if elo_scope is None:
                skipped.append(league_code + ":elo")
                continue

            payload = load_fixture_payload(entry)
            if payload is None:
                skipped.append(league_code + ":payload")
                continue
            fixtures = [
                row for row in (payload.get("fixtures") or [])
                if isinstance(row, dict)
            ]
            result = simulate_league(entry, fixtures, runs, elo_scope)
            if result is None:
                skipped.append(league_code + ":contract")
                continue
            meta, rows = result

            con.execute(
                """
                INSERT INTO prepared_league_simulation_meta(
                    league_code,country,league_name,season,team_count,
                    current_completed_matches,remaining_matches,simulation_runs,
                    model_version,elo_model_version,average_elo_weight,
                    tie_break_model,mature_fixture_count,total_simulated_fixtures,
                    generated_at_ms
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    meta["league_code"], meta["country"], meta["league_name"], meta["season"],
                    meta["team_count"], meta["current_completed_matches"], meta["remaining_matches"],
                    meta["simulation_runs"], meta["model_version"], meta["elo_model_version"],
                    meta["average_elo_weight"], meta["tie_break_model"],
                    meta["mature_fixture_count"], meta["total_simulated_fixtures"], generated_at_ms,
                ),
            )
            con.executemany(
                """
                INSERT INTO prepared_league_team_simulations(
                    league_code,season,team_key,team_name,team_logo,elo_rating,
                    current_position,current_played,current_points,
                    current_goal_difference,current_goals_for,
                    expected_position,expected_points,title_probability,
                    top2_probability,top4_probability,position_probabilities_json,
                    generated_at_ms
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        meta["league_code"], meta["season"], row["team_key"], row["team_name"], row["team_logo"],
                        row["elo_rating"], row["current_position"], row["current_played"], row["current_points"],
                        row["current_goal_difference"], row["current_goals_for"],
                        row["expected_position"], row["expected_points"], row["title_probability"],
                        row["top2_probability"], row["top4_probability"], row["position_probabilities_json"],
                        generated_at_ms,
                    )
                    for row in rows
                ],
            )
            if meta["league_code"] == "E0":
                e0_rows = rows
            league_count += 1
            team_count += len(rows)

        con.commit()
        quick = con.execute("PRAGMA quick_check").fetchone()
        if not quick or quick[0] != "ok":
            raise SystemExit(f"Prepared DB quick_check failed after league simulation: {quick}")
        if league_count <= 0 or team_count <= 0:
            raise SystemExit("No eligible league simulations were materialized")

        print(
            "PREPARED_LEAGUE_SIMULATION_OK",
            f"model={MODEL_VERSION}",
            f"elo={ELO_MODEL_VERSION}",
            f"runs={runs}",
            f"leagues={league_count}",
            f"teams={team_count}",
            "skipped=" + (",".join(skipped) if skipped else "none"),
        )
        if e0_rows:
            top = sorted(e0_rows, key=lambda row: row["title_probability"], reverse=True)[:10]
            print(
                "LEAGUE_E0_TITLE_TOP",
                " | ".join(
                    f'{row["team_name"]}={row["title_probability"] * 100.0:.1f}%'
                    f'(Elo={row["elo_rating"]:.0f},expPts={row["expected_points"]:.1f})'
                    for row in top
                ),
            )
    finally:
        con.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
