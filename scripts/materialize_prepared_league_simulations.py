#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sqlite3
import time
from bisect import bisect_left
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
MODEL_VERSION = "league-season-monte-carlo-v3-all-domestic-elo"
ELO_MODEL_VERSION = "team-elo-v1"
TIE_BREAK_MODEL = "POINTS_GD_GF"

ELO_GOAL_LOG_COEFF = 0.55
ELO_WEIGHT_EARLY = 0.65
ELO_WEIGHT_FLOOR = 0.35
ELO_WEIGHT_DECAY_PER_MATCH = 0.03

ORDERED = "ordered"
UNORDERED = "unordered"


@dataclass(frozen=True)
class StageSpec:
    size: int
    mode: str
    meetings: int


@dataclass(frozen=True)
class LeagueRule:
    regular_mode: str
    regular_meetings: int
    split_groups: tuple[StageSpec, ...] = ()
    format_label: str = "Regular league"
    reset_split_goals: bool = False


# Every current_target domestic league in the repository has an explicit competition rule.
# ORDERED n => n home meetings for each ordered team pair.
# UNORDERED n => n total meetings per unordered pair, with home venue balanced synthetically
# when the future official draw is not yet determined.
RULES: dict[str, LeagueRule] = {
    "AUT2": LeagueRule(ORDERED, 1, format_label="Double round-robin"),
    "AUT": LeagueRule(
        ORDERED, 1,
        (StageSpec(6, ORDERED, 1), StageSpec(6, ORDERED, 1)),
        "22 rounds + Championship/Qualification split",
    ),
    "B1": LeagueRule(ORDERED, 1, format_label="34-round double round-robin"),
    "CRO": LeagueRule(ORDERED, 2, format_label="Four-round league"),
    "CYP": LeagueRule(
        ORDERED, 1,
        (StageSpec(6, ORDERED, 1), StageSpec(8, UNORDERED, 1)),
        "26 rounds + 6/8 split",
    ),
    "EGY": LeagueRule(
        UNORDERED, 1,
        (StageSpec(6, UNORDERED, 1), StageSpec(14, UNORDERED, 1)),
        "Single round + top-6 championship group",
    ),
    "E0": LeagueRule(ORDERED, 1, format_label="Double round-robin"),
    "E1": LeagueRule(ORDERED, 1, format_label="Double round-robin"),
    "E2": LeagueRule(ORDERED, 1, format_label="Double round-robin"),
    "E3": LeagueRule(ORDERED, 1, format_label="Double round-robin"),
    "EC": LeagueRule(ORDERED, 1, format_label="Double round-robin"),
    "F1": LeagueRule(ORDERED, 1, format_label="Double round-robin"),
    "F2": LeagueRule(ORDERED, 1, format_label="Double round-robin"),
    "D1": LeagueRule(ORDERED, 1, format_label="Double round-robin"),
    "D2": LeagueRule(ORDERED, 1, format_label="Double round-robin"),
    "G1": LeagueRule(
        ORDERED, 1,
        (StageSpec(4, ORDERED, 1), StageSpec(4, ORDERED, 1), StageSpec(6, ORDERED, 1)),
        "26 rounds + 4/4/6 split",
    ),
    "ISR": LeagueRule(
        ORDERED, 1,
        (StageSpec(6, ORDERED, 1), StageSpec(8, UNORDERED, 1)),
        "26 rounds + 6/8 split",
    ),
    "I1": LeagueRule(ORDERED, 1, format_label="Double round-robin"),
    "I2": LeagueRule(ORDERED, 1, format_label="Double round-robin"),
    "JPN": LeagueRule(ORDERED, 1, format_label="Double round-robin"),
    "N1": LeagueRule(ORDERED, 1, format_label="Double round-robin"),
    "P1": LeagueRule(ORDERED, 1, format_label="Double round-robin"),
    "SAU": LeagueRule(ORDERED, 1, format_label="34-round double round-robin"),
    "SC0": LeagueRule(
        UNORDERED, 3,
        (StageSpec(6, UNORDERED, 1), StageSpec(6, UNORDERED, 1)),
        "33 rounds + top/bottom six split",
    ),
    "SC1": LeagueRule(ORDERED, 2, format_label="Four-round league"),
    "SC2": LeagueRule(ORDERED, 2, format_label="Four-round league"),
    "SC3": LeagueRule(ORDERED, 2, format_label="Four-round league"),
    "RSA": LeagueRule(ORDERED, 1, format_label="Double round-robin"),
    "SP1": LeagueRule(ORDERED, 1, format_label="Double round-robin"),
    "SP2": LeagueRule(ORDERED, 1, format_label="Double round-robin"),
    "T1": LeagueRule(ORDERED, 1, format_label="Double round-robin"),
    "UKR": LeagueRule(ORDERED, 1, format_label="Double round-robin"),

    # Cache-backed active leagues. These scopes use already-published current-season
    # fixture caches and therefore require zero additional API-Football requests.
    "BRA": LeagueRule(ORDERED, 1, format_label="Double round-robin"),
    "BRA2": LeagueRule(ORDERED, 1, format_label="Double round-robin"),
    "CHL": LeagueRule(ORDERED, 1, format_label="Double round-robin"),
    "CHN": LeagueRule(ORDERED, 1, format_label="Double round-robin"),
    "EST": LeagueRule(ORDERED, 2, format_label="Four-round league"),
    "HUN": LeagueRule(UNORDERED, 3, format_label="33-round triple round-robin"),
    "IRL": LeagueRule(ORDERED, 2, format_label="Four-round league"),
    "LVA": LeagueRule(ORDERED, 2, format_label="Four-round league"),
    "LTU": LeagueRule(ORDERED, 2, format_label="Four-round league"),
    "NOR": LeagueRule(ORDERED, 1, format_label="Double round-robin"),
    "NOR2": LeagueRule(ORDERED, 1, format_label="Double round-robin"),
    "POL": LeagueRule(ORDERED, 1, format_label="Double round-robin"),
    "RUS": LeagueRule(ORDERED, 1, format_label="Double round-robin"),
    "SWE": LeagueRule(ORDERED, 1, format_label="Double round-robin"),
    "SWE2": LeagueRule(ORDERED, 1, format_label="Double round-robin"),
    "SVN": LeagueRule(ORDERED, 2, format_label="Four-round league"),
    "UAE": LeagueRule(ORDERED, 1, format_label="Double round-robin"),

    # Second cache-backed expansion. All of these use already-published current-season
    # fixture caches and require zero additional API-Football requests.
    "BGR": LeagueRule(
        ORDERED, 1,
        (StageSpec(4, ORDERED, 1), StageSpec(4, ORDERED, 1), StageSpec(6, UNORDERED, 1)),
        "26 rounds + 4/4/6 split",
    ),
    "CZE": LeagueRule(
        ORDERED, 1,
        (StageSpec(6, UNORDERED, 1), StageSpec(4, UNORDERED, 0), StageSpec(6, UNORDERED, 1)),
        "30 rounds + 6/4/6 final phase",
    ),
    "DNK": LeagueRule(
        ORDERED, 1,
        (StageSpec(6, ORDERED, 1), StageSpec(6, ORDERED, 1)),
        "22 rounds + 6/6 split",
    ),
    "FIN": LeagueRule(
        ORDERED, 1,
        (StageSpec(6, ORDERED, 1), StageSpec(6, UNORDERED, 1)),
        "22 rounds + championship/relegation series",
    ),
    "FIN2": LeagueRule(UNORDERED, 3, format_label="27-round triple round-robin"),
    "ISL": LeagueRule(
        ORDERED, 1,
        (StageSpec(6, UNORDERED, 1), StageSpec(6, UNORDERED, 1)),
        "22 rounds + top/bottom six split",
    ),
    "ROM": LeagueRule(
        ORDERED, 1,
        (StageSpec(6, ORDERED, 1), StageSpec(10, UNORDERED, 1)),
        "30 rounds + 6/10 split with halved points",
        reset_split_goals=True,
    ),
    "SRB": LeagueRule(
        ORDERED, 1,
        (StageSpec(6, UNORDERED, 1), StageSpec(8, UNORDERED, 1)),
        "26 rounds + 6/8 split",
    ),
    "SVK": LeagueRule(
        ORDERED, 1,
        (StageSpec(6, ORDERED, 1), StageSpec(6, ORDERED, 1)),
        "22 rounds + 6/6 split",
    ),
    "KOR": LeagueRule(
        UNORDERED, 3,
        (StageSpec(6, UNORDERED, 1), StageSpec(6, UNORDERED, 1)),
        "33 rounds + Final A/Final B",
    ),
    "SWZ": LeagueRule(
        UNORDERED, 3,
        (StageSpec(6, UNORDERED, 1), StageSpec(6, UNORDERED, 1)),
        "33 rounds + championship/relegation groups",
    ),

    # Final cache-backed expansion. Complex competitions are routed through
    # simulate_special_league; ECU/MAR use the native stage engine.
    "ECU": LeagueRule(
        ORDERED, 1,
        (StageSpec(6, ORDERED, 1), StageSpec(6, ORDERED, 1), StageSpec(4, ORDERED, 1)),
        "30 rounds + 6/6/4 final phase",
    ),
    "MAR": LeagueRule(ORDERED, 1, format_label="30-round double round-robin"),
    "ARG": LeagueRule(UNORDERED, 1, format_label="Apertura/Clausura zones + annual champion"),
    "COL": LeagueRule(UNORDERED, 1, format_label="Clausura + quadrangulares + final"),
    "MEX": LeagueRule(UNORDERED, 1, format_label="Apertura + top-8 Liguilla"),
    "PER": LeagueRule(UNORDERED, 1, format_label="Apertura/Clausura + national playoffs"),
    "USA": LeagueRule(UNORDERED, 1, format_label="MLS conferences + Audi MLS Cup Playoffs"),
    "URU": LeagueRule(UNORDERED, 1, format_label="Apertura/Intermedio/Clausura + championship playoff"),
}

SIMULATION_CACHE_CURRENT_CODES = {
    "BRA", "BRA2", "CHL", "CHN", "EST", "HUN", "IRL", "LVA", "LTU",
    "NOR", "NOR2", "POL", "RUS", "SWE", "SWE2", "SVN", "UAE",
    "BGR", "CZE", "DNK", "FIN", "FIN2", "ISL", "ROM", "SRB", "SVK",
    "KOR", "SWZ",
    "ARG", "COL", "ECU", "MEX", "MAR", "PER", "USA", "URU",
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

# Semantic meaning of each post-regular-season group. These keys are persisted for the
# Android UAT UI so split leagues are not presented like ordinary round-robin leagues.
SPLIT_GROUP_KEYS: dict[str, tuple[str, ...]] = {
    "AUT": ("aut_meistergruppe", "aut_qualifikationsgruppe"),
    "CYP": ("cyp_top6", "cyp_bottom8"),
    "EGY": ("egy_title_top6", "egy_survival_group"),
    "G1": ("g1_playoffs_1_4", "g1_playoffs_5_8", "g1_playouts_9_14"),
    "ISR": ("isr_championship_top6", "isr_lower_bottom8"),
    "SC0": ("sc0_top6", "sc0_bottom6"),
    "BGR": ("bgr_title_top4", "bgr_europe_5_8", "bgr_relegation_9_14"),
    "CZE": ("cze_title_top6", "cze_placement_7_10", "cze_relegation_11_16"),
    "DNK": ("dnk_championship_top6", "dnk_relegation_bottom6"),
    "FIN": ("fin_championship_top6", "fin_relegation_bottom6"),
    "ISL": ("isl_championship_top6", "isl_relegation_bottom6"),
    "ROM": ("rom_playoff_top6", "rom_playout_bottom10"),
    "SRB": ("srb_playoff_top6", "srb_playout_bottom8"),
    "SVK": ("svk_championship_top6", "svk_relegation_bottom6"),
    "KOR": ("kor_final_a", "kor_final_b"),
    "SWZ": ("swz_championship_top6", "swz_relegation_bottom6"),
    "ECU": ("ecu_title_top6", "ecu_sudamericana_7_12", "ecu_relegation_13_16"),
}

# Competition-specific point carry rules for the second phase.
# Greece 5-8 starts with half the regular-season points, rounded up.
SPLIT_POINT_RULES: dict[str, tuple[tuple[int, bool], ...]] = {
    "G1": ((1, False), (2, True), (1, False)),
    "ROM": ((2, True), (2, True)),
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


def fixture_date(row: dict[str, Any]) -> str:
    return str(row.get("date") or row.get("date_utc") or "")[:10]


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
    all_current = [
        row for row in (index.get("leagues") or [])
        if is_simulation_current_entry(row)
        and (ROOT / str(row.get("cache_path") or "")).is_file()
        and int(row.get("completed_fixtures") or 0) >= 10
    ]
    missing_rules = sorted({
        str(row.get("league_code") or "")
        for row in all_current
        if str(row.get("league_code") or "") not in RULES
    })
    if missing_rules:
        raise SystemExit(f"Missing League Simulation rules for active leagues: {missing_rules}")
    return sorted(
        all_current,
        key=lambda row: (str(row.get("country") or ""), str(row.get("league_code") or "")),
    )


def load_fixture_payload(entry: dict[str, Any]) -> dict[str, Any] | None:
    path = ROOT / str(entry.get("cache_path") or "")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def current_season_fixtures(entry: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
    start_date = str(entry.get("target_season_start") or "")
    end_date = str(entry.get("target_season_end") or "")
    rows: list[dict[str, Any]] = []
    for row in (payload.get("fixtures") or []):
        if not isinstance(row, dict):
            continue
        if str(row.get("status") or "").upper() not in FINISHED_STATUSES:
            continue
        if fixture_goals(row) is None:
            continue
        date = fixture_date(row)
        if start_date and date and date < start_date:
            continue
        if end_date and date and date > end_date:
            continue
        rows.append(row)
    rows.sort(key=lambda row: (fixture_date(row), int(row.get("fixture_id") or 0)))
    return rows


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


def build_table(fixtures: list[dict[str, Any]]) -> dict[str, TeamStanding]:
    teams: dict[str, TeamStanding] = {}
    for fixture in fixtures:
        home_name = str(fixture.get("home_team") or "").strip()
        away_name = str(fixture.get("away_team") or "").strip()
        home_key = norm(home_name)
        away_key = norm(away_name)
        if not home_key or not away_key or home_key == away_key:
            continue
        goals = fixture_goals(fixture)
        if goals is None:
            continue

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
    return teams


def ranking_for_values(
    team_keys: list[str],
    points: dict[str, int],
    goals_for: dict[str, int],
    goals_against: dict[str, int],
    names: dict[str, str],
) -> list[str]:
    return sorted(
        team_keys,
        key=lambda key: (
            -points[key],
            -(goals_for[key] - goals_against[key]),
            -goals_for[key],
            names[key].casefold(),
        ),
    )


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


def stage_total(team_count: int, mode: str, meetings: int) -> int:
    if mode == ORDERED:
        return team_count * (team_count - 1) * meetings
    if mode == UNORDERED:
        return team_count * (team_count - 1) // 2 * meetings
    raise ValueError(f"Unknown stage mode: {mode}")


def count_pairs(fixtures: list[dict[str, Any]]) -> tuple[
    dict[tuple[str, str], int],
    dict[tuple[str, str], int],
    dict[str, int],
]:
    ordered: dict[tuple[str, str], int] = {}
    unordered: dict[tuple[str, str], int] = {}
    home_games: dict[str, int] = {}
    for fixture in fixtures:
        home = norm(fixture.get("home_team"))
        away = norm(fixture.get("away_team"))
        if not home or not away or home == away:
            continue
        ordered[(home, away)] = ordered.get((home, away), 0) + 1
        pair = tuple(sorted((home, away)))
        unordered[pair] = unordered.get(pair, 0) + 1
        home_games[home] = home_games.get(home, 0) + 1
    return ordered, unordered, home_games


def choose_home(
    a: str,
    b: str,
    ordered_counts: dict[tuple[str, str], int],
    projected_home_games: dict[str, int],
) -> tuple[str, str]:
    a_home = ordered_counts.get((a, b), 0)
    b_home = ordered_counts.get((b, a), 0)
    if a_home < b_home:
        return a, b
    if b_home < a_home:
        return b, a
    a_total = projected_home_games.get(a, 0)
    b_total = projected_home_games.get(b, 0)
    if a_total < b_total:
        return a, b
    if b_total < a_total:
        return b, a
    return (a, b) if a < b else (b, a)


def remaining_stage_schedule(
    team_keys: list[str],
    mode: str,
    meetings: int,
    completed: list[dict[str, Any]],
) -> list[tuple[str, str]]:
    ordered_counts, unordered_counts, home_games = count_pairs(completed)
    schedule: list[tuple[str, str]] = []

    if mode == ORDERED:
        for home in sorted(team_keys):
            for away in sorted(team_keys):
                if home == away:
                    continue
                missing = max(0, meetings - ordered_counts.get((home, away), 0))
                for _ in range(missing):
                    schedule.append((home, away))
                    ordered_counts[(home, away)] = ordered_counts.get((home, away), 0) + 1
                    home_games[home] = home_games.get(home, 0) + 1
        return schedule

    if mode == UNORDERED:
        keys = sorted(team_keys)
        for i, a in enumerate(keys):
            for b in keys[i + 1:]:
                pair = tuple(sorted((a, b)))
                missing = max(0, meetings - unordered_counts.get(pair, 0))
                for _ in range(missing):
                    home, away = choose_home(a, b, ordered_counts, home_games)
                    schedule.append((home, away))
                    ordered_counts[(home, away)] = ordered_counts.get((home, away), 0) + 1
                    unordered_counts[pair] = unordered_counts.get(pair, 0) + 1
                    home_games[home] = home_games.get(home, 0) + 1
        return schedule

    raise ValueError(f"Unknown stage mode: {mode}")


def full_hypothetical_stage_schedule(
    team_keys: list[str],
    spec: StageSpec,
    rng: random.Random,
) -> list[tuple[str, str]]:
    keys = sorted(team_keys)
    if spec.mode == ORDERED:
        return [
            (home, away)
            for home in keys
            for away in keys
            if home != away
            for _ in range(spec.meetings)
        ]

    schedule: list[tuple[str, str]] = []
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            for _ in range(spec.meetings):
                if rng.random() < 0.5:
                    schedule.append((a, b))
                else:
                    schedule.append((b, a))
    return schedule


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


class MatchModel:
    def __init__(
        self,
        history: LeagueHistory,
        elo_scope: EloScope,
        league_code: str,
        season: str,
    ) -> None:
        self.history = history
        self.elo_scope = elo_scope
        self.league_code = league_code
        self.season = season
        self.league_home_mean = history.mean(history.league_home.get("goals", []))
        self.league_away_mean = history.mean(history.league_away.get("goals", []))
        self.cache: dict[tuple[str, str], tuple[list[float], list[float], bool, float]] = {}

    def cdfs(self, home: str, away: str) -> tuple[list[float], list[float], bool, float] | None:
        key = (home, away)
        if key in self.cache:
            return self.cache[key]
        if self.league_home_mean is None or self.league_away_mean is None:
            return None
        expected = self.history.expected(home, away, "goals")
        if expected is None:
            return None
        stat_home_mean, stat_away_mean, home_sample, away_sample, league_sample = expected
        if league_sample < 20:
            return None
        if home not in self.elo_scope.teams or away not in self.elo_scope.teams:
            return None
        home_mean, away_mean, weight = elo_blended_goal_means(
            stat_home_mean=stat_home_mean,
            stat_away_mean=stat_away_mean,
            league_home_mean=self.league_home_mean,
            league_away_mean=self.league_away_mean,
            home_elo=self.elo_scope.teams[home],
            away_elo=self.elo_scope.teams[away],
            elo_scope=self.elo_scope,
        )
        value = (
            distribution_cdf(home_mean, None, 14),
            distribution_cdf(away_mean, None, 14),
            home_sample >= 3 and away_sample >= 3,
            weight,
        )
        self.cache[key] = value
        return value

    def draw(self, home: str, away: str, rng: random.Random) -> tuple[int, int] | None:
        model = self.cdfs(home, away)
        if model is None:
            return None
        home_cdf, away_cdf, _, _ = model
        hg = min(len(home_cdf) - 1, bisect_left(home_cdf, rng.random()))
        ag = min(len(away_cdf) - 1, bisect_left(away_cdf, rng.random()))
        return hg, ag


def apply_score(
    home: str,
    away: str,
    hg: int,
    ag: int,
    points: dict[str, int],
    goals_for: dict[str, int],
    goals_against: dict[str, int],
) -> None:
    goals_for[home] += hg
    goals_against[home] += ag
    goals_for[away] += ag
    goals_against[away] += hg
    if hg > ag:
        points[home] += 3
    elif hg < ag:
        points[away] += 3
    else:
        points[home] += 1
        points[away] += 1


def transformed_group_points(
    league_code: str,
    group_index: int,
    value: int,
) -> int:
    rules = SPLIT_POINT_RULES.get(league_code)
    if not rules or group_index >= len(rules):
        return value
    divisor, round_up = rules[group_index]
    if divisor <= 1:
        return value
    if round_up:
        return (value + divisor - 1) // divisor
    return value // divisor


def apply_points_only(home: str, away: str, hg: int, ag: int, points: dict[str, int]) -> None:
    if hg > ag:
        points[home] += 3
    elif hg < ag:
        points[away] += 3
    else:
        points[home] += 1
        points[away] += 1



SPECIAL_SIMULATION_CODES = {"ARG", "COL", "MEX", "PER", "USA", "URU"}

ARG_ZONE_A_LABELS = (
    "Platense", "Defensa Y Justicia", "Central Cordoba de Santiago", "Lanus",
    "Deportivo Riestra", "Talleres Cordoba", "Boca Juniors", "Estudiantes L.P.",
    "Instituto Cordoba", "Gimnasia M.", "San Lorenzo", "Independiente",
    "Newells Old Boys", "Union Santa Fe", "Velez Sarsfield",
)
ARG_ZONE_B_LABELS = (
    "Argentinos JRS", "Aldosivi", "Atletico Tucuman", "Banfield",
    "Barracas Central", "Belgrano Cordoba", "River Plate", "Gimnasia L.P.",
    "Estudiantes de Rio Cuarto", "Independ. Rivadavia", "Huracan",
    "Racing Club", "Rosario Central", "Sarmiento Junin", "Tigre",
)

MLS_EAST_LABELS = (
    "Atlanta United FC", "Charlotte", "Chicago Fire", "FC Cincinnati",
    "Columbus Crew", "DC United", "Inter Miami", "Montreal Impact",
    "Nashville SC", "New England Revolution", "New York City FC",
    "Orlando City SC", "Philadelphia Union", "New York Red Bulls", "Toronto FC",
)
MLS_WEST_LABELS = (
    "Austin", "Colorado Rapids", "FC Dallas", "Houston Dynamo", "Los Angeles FC",
    "Los Angeles Galaxy", "Minnesota United FC", "Portland Timbers",
    "Real Salt Lake", "St. Louis City", "San Diego", "San Jose Earthquakes",
    "Seattle Sounders", "Sporting Kansas City", "Vancouver Whitecaps",
)

EXTRA_CURRENT_TEAMS: dict[str, tuple[str, ...]] = {
    "MAR": ("FAR Rabat",),
}


def resolve_team_label(team_keys: list[str], label: str) -> str | None:
    target = norm(label)
    if target in team_keys:
        return target
    candidates = [
        key for key in team_keys
        if target in key or key in target
    ]
    if len(candidates) == 1:
        return candidates[0]
    return None


def resolve_group(team_keys: list[str], labels: tuple[str, ...], group_name: str) -> list[str]:
    resolved: list[str] = []
    for label in labels:
        key = resolve_team_label(team_keys, label)
        if key is None:
            raise RuntimeError(f"{group_name}: cannot resolve team label {label!r}")
        if key in resolved:
            raise RuntimeError(f"{group_name}: duplicate resolved team {key}")
        resolved.append(key)
    if len(resolved) != len(labels):
        raise RuntimeError(f"{group_name}: incomplete group resolution")
    return resolved


def add_extra_current_teams(
    table: dict[str, TeamStanding],
    league_code: str,
    elo_scope: EloScope,
) -> None:
    for name in EXTRA_CURRENT_TEAMS.get(league_code, ()):
        key = norm(name)
        if key and key in elo_scope.teams and key not in table:
            table[key] = TeamStanding(key=key, name=name)


def combine_tables(*tables: dict[str, TeamStanding]) -> dict[str, TeamStanding]:
    out: dict[str, TeamStanding] = {}
    for table in tables:
        for key, team in table.items():
            row = out.setdefault(
                key,
                TeamStanding(
                    key=key,
                    name=team.name,
                    logo=team.logo,
                ),
            )
            if not row.logo and team.logo:
                row.logo = team.logo
            row.played += team.played
            row.points += team.points
            row.goals_for += team.goals_for
            row.goals_against += team.goals_against
    return out


def seed_rank_map(order: list[str]) -> dict[str, int]:
    return {key: idx + 1 for idx, key in enumerate(order)}


def resolve_decisive_draw(
    first: str,
    second: str,
    elo_scope: EloScope,
    rng: random.Random,
    home_advantage: bool = False,
) -> str:
    first_elo = elo_scope.teams[first].rating + (elo_scope.home_advantage if home_advantage else 0.0)
    second_elo = elo_scope.teams[second].rating
    exponent = (second_elo - first_elo) / max(1.0, elo_scope.rating_scale)
    probability_first = 1.0 / (1.0 + 10.0 ** exponent)
    return first if rng.random() < probability_first else second


def single_match_winner(
    home: str,
    away: str,
    match_model: MatchModel,
    elo_scope: EloScope,
    rng: random.Random,
) -> str:
    score = match_model.draw(home, away, rng)
    if score is None:
        raise RuntimeError(f"Cannot model knockout match {home} v {away}")
    hg, ag = score
    if hg > ag:
        return home
    if ag > hg:
        return away
    return resolve_decisive_draw(home, away, elo_scope, rng, home_advantage=True)


def neutral_match_winner(
    first: str,
    second: str,
    match_model: MatchModel,
    elo_scope: EloScope,
    rng: random.Random,
) -> str:
    first_score = match_model.draw(first, second, rng)
    second_score = match_model.draw(second, first, rng)
    if first_score is None or second_score is None:
        raise RuntimeError(f"Cannot model neutral match {first} v {second}")
    first_goals = first_score[0] + second_score[1]
    second_goals = first_score[1] + second_score[0]
    if first_goals > second_goals:
        return first
    if second_goals > first_goals:
        return second
    return resolve_decisive_draw(first, second, elo_scope, rng, home_advantage=False)


def two_leg_winner(
    first: str,
    second: str,
    match_model: MatchModel,
    elo_scope: EloScope,
    rng: random.Random,
    higher_seed: str | None = None,
) -> str:
    leg1 = match_model.draw(first, second, rng)
    leg2 = match_model.draw(second, first, rng)
    if leg1 is None or leg2 is None:
        raise RuntimeError(f"Cannot model two-leg tie {first} v {second}")
    first_goals = leg1[0] + leg2[1]
    second_goals = leg1[1] + leg2[0]
    if first_goals > second_goals:
        return first
    if second_goals > first_goals:
        return second
    if higher_seed in (first, second):
        return higher_seed
    return resolve_decisive_draw(first, second, elo_scope, rng, home_advantage=False)


def best_of_three_winner(
    higher: str,
    lower: str,
    match_model: MatchModel,
    elo_scope: EloScope,
    rng: random.Random,
) -> str:
    wins = {higher: 0, lower: 0}
    for home, away in ((higher, lower), (lower, higher), (higher, lower)):
        winner = single_match_winner(home, away, match_model, elo_scope, rng)
        wins[winner] += 1
        if wins[winner] >= 2:
            return winner
    return higher if wins[higher] > wins[lower] else lower


def synthetic_cross_group_schedule(
    group_a: list[str],
    group_b: list[str],
    completed: list[dict[str, Any]],
    target_cross_games_per_team: int,
) -> list[tuple[str, str]]:
    ordered_counts, _, home_games = count_pairs(completed)
    completed_pairs: set[tuple[str, str]] = set()
    cross_counts = {key: 0 for key in [*group_a, *group_b]}
    set_a = set(group_a)
    set_b = set(group_b)
    for fixture in completed:
        home = norm(fixture.get("home_team"))
        away = norm(fixture.get("away_team"))
        if not home or not away:
            continue
        if (home in set_a and away in set_b) or (home in set_b and away in set_a):
            pair = tuple(sorted((home, away)))
            completed_pairs.add(pair)
            cross_counts[home] = cross_counts.get(home, 0) + 1
            cross_counts[away] = cross_counts.get(away, 0) + 1

    deficits = {
        key: max(0, target_cross_games_per_team - cross_counts.get(key, 0))
        for key in [*group_a, *group_b]
    }
    planned_pairs = set(completed_pairs)
    schedule: list[tuple[str, str]] = []

    while sum(deficits[key] for key in group_a) > 0:
        a_candidates = [key for key in group_a if deficits[key] > 0]
        b_candidates = [key for key in group_b if deficits[key] > 0]
        if not a_candidates or not b_candidates:
            break
        a = sorted(a_candidates, key=lambda key: (-deficits[key], key))[0]
        options = [
            b for b in b_candidates
            if tuple(sorted((a, b))) not in planned_pairs
        ]
        if not options:
            options = b_candidates
        b = sorted(options, key=lambda key: (-deficits[key], key))[0]
        home, away = choose_home(a, b, ordered_counts, home_games)
        schedule.append((home, away))
        pair = tuple(sorted((a, b)))
        planned_pairs.add(pair)
        ordered_counts[(home, away)] = ordered_counts.get((home, away), 0) + 1
        home_games[home] = home_games.get(home, 0) + 1
        deficits[a] -= 1
        deficits[b] -= 1

    if any(value != 0 for value in deficits.values()):
        raise RuntimeError(
            "Cannot synthesize cross-group schedule "
            f"target={target_cross_games_per_team} deficits={deficits}"
        )
    return schedule


def apply_simulated_schedule(
    schedule: list[tuple[str, str]],
    match_model: MatchModel,
    rng: random.Random,
    points: dict[str, int],
    goals_for: dict[str, int],
    goals_against: dict[str, int],
) -> None:
    for home, away in schedule:
        score = match_model.draw(home, away, rng)
        if score is None:
            raise RuntimeError(f"Cannot model scheduled match {home} v {away}")
        apply_score(
            home, away, score[0], score[1],
            points, goals_for, goals_against,
        )


def special_meta_and_rows(
    entry: dict[str, Any],
    elo_scope: EloScope,
    current_table: dict[str, TeamStanding],
    position_counts: dict[str, list[int]],
    point_totals: dict[str, int],
    title_counts: dict[str, int],
    split_group_keys: tuple[str, ...],
    split_group_counts: dict[str, list[int]],
    runs: int,
    format_label: str,
    regular_stage_target_matches: int,
    remaining_matches: int,
    average_elo_weight: float,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    team_keys = sorted(current_table)
    current_order = current_ranking(current_table)
    current_positions = {key: idx + 1 for idx, key in enumerate(current_order)}
    team_rows: list[dict[str, Any]] = []
    for key in team_keys:
        counts = position_counts[key]
        team = current_table[key]
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
                "expected_position": sum(
                    (idx + 1) * count for idx, count in enumerate(counts)
                ) / runs,
                "expected_points": point_totals[key] / runs,
                "title_probability": title_counts[key] / runs,
                "top2_probability": sum(counts[: min(2, len(counts))]) / runs,
                "top4_probability": sum(counts[: min(4, len(counts))]) / runs,
                "position_probabilities_json": json.dumps(
                    [count / runs for count in counts],
                    separators=(",", ":"),
                ),
                "split_group_probabilities_json": json.dumps(
                    [count / runs for count in split_group_counts[key]],
                    separators=(",", ":"),
                ),
            }
        )

    league_code = str(entry.get("league_code") or "")
    season = str(entry.get("app_season") or entry.get("target_app_season") or "")
    meta = {
        "league_code": league_code,
        "country": str(entry.get("country") or ""),
        "league_name": str(entry.get("league") or ""),
        "season": season,
        "team_count": len(team_keys),
        "current_completed_matches": sum(team.played for team in current_table.values()) // 2,
        "remaining_matches": remaining_matches,
        "simulation_runs": runs,
        "model_version": MODEL_VERSION,
        "elo_model_version": elo_scope.model_version,
        "average_elo_weight": average_elo_weight,
        "tie_break_model": TIE_BREAK_MODEL,
        "mature_fixture_count": 0,
        "total_simulated_fixtures": remaining_matches,
        "format_label": format_label,
        "regular_stage_target_matches": regular_stage_target_matches,
        "split_group_count": len(split_group_keys),
        "split_group_keys_json": json.dumps(split_group_keys, separators=(",", ":")),
    }
    return meta, team_rows


def schedule_average_elo_weight(
    schedule: list[tuple[str, str]],
    elo_scope: EloScope,
) -> float:
    weights = [
        elo_weight(
            elo_scope.teams[home].current_matches,
            elo_scope.teams[away].current_matches,
        )
        for home, away in schedule
        if home in elo_scope.teams and away in elo_scope.teams
    ]
    return sum(weights) / max(1, len(weights)) if weights else ELO_WEIGHT_FLOOR


def simulate_special_league(
    entry: dict[str, Any],
    fixtures: list[dict[str, Any]],
    runs: int,
    elo_scope: EloScope,
) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    league_code = str(entry.get("league_code") or "")
    season = str(entry.get("app_season") or entry.get("target_app_season") or "")
    history = LeagueHistory(fixtures)
    match_model = MatchModel(history, elo_scope, league_code, season)
    rng = random.Random(
        int(hashlib.sha256(
            f"{league_code}|{season}|special|{runs}|{MODEL_VERSION}".encode("utf-8")
        ).hexdigest()[:16], 16)
    )

    if league_code == "ARG":
        if len(fixtures) < 255:
            return None
        apertura_regular = fixtures[:240]
        clausura_finished = fixtures[255:]
        apertura_table = build_table(apertura_regular)
        clausura_table = build_table(clausura_finished)
        team_keys = sorted(set(apertura_table) | set(clausura_table))
        if len(team_keys) != 30 or any(key not in elo_scope.teams for key in team_keys):
            return None
        zone_a = resolve_group(team_keys, ARG_ZONE_A_LABELS, "ARG Zone A")
        zone_b = resolve_group(team_keys, ARG_ZONE_B_LABELS, "ARG Zone B")
        if set(zone_a) | set(zone_b) != set(team_keys):
            raise RuntimeError("ARG zones do not cover all 30 teams")

        zone_a_set = set(zone_a)
        zone_b_set = set(zone_b)
        zone_a_finished = [
            row for row in clausura_finished
            if norm(row.get("home_team")) in zone_a_set
            and norm(row.get("away_team")) in zone_a_set
        ]
        zone_b_finished = [
            row for row in clausura_finished
            if norm(row.get("home_team")) in zone_b_set
            and norm(row.get("away_team")) in zone_b_set
        ]
        remaining = [
            *remaining_stage_schedule(zone_a, UNORDERED, 1, zone_a_finished),
            *remaining_stage_schedule(zone_b, UNORDERED, 1, zone_b_finished),
            *synthetic_cross_group_schedule(zone_a, zone_b, clausura_finished, 2),
        ]
        current_table = combine_tables(apertura_table, clausura_table)
        base_points = {key: current_table[key].points for key in team_keys}
        base_gf = {key: current_table[key].goals_for for key in team_keys}
        base_ga = {key: current_table[key].goals_against for key in team_keys}
        names = {key: current_table[key].name for key in team_keys}

        position_counts = {key: [0] * len(team_keys) for key in team_keys}
        point_totals = {key: 0 for key in team_keys}
        title_counts = {key: 0 for key in team_keys}
        split_keys = ("arg_clausura_zone_a", "arg_clausura_zone_b")
        split_counts = {key: [0, 0] for key in team_keys}
        for key in zone_a:
            split_counts[key][0] = runs
        for key in zone_b:
            split_counts[key][1] = runs

        for _ in range(runs):
            points = dict(base_points)
            gf = dict(base_gf)
            ga = dict(base_ga)
            apply_simulated_schedule(remaining, match_model, rng, points, gf, ga)
            order = ranking_for_values(team_keys, points, gf, ga, names)
            title_counts[order[0]] += 1
            for pos, key in enumerate(order):
                position_counts[key][pos] += 1
                point_totals[key] += points[key]

        return special_meta_and_rows(
            entry, elo_scope, current_table, position_counts, point_totals,
            title_counts, split_keys, split_counts, runs,
            "Apertura/Clausura zones + Campeón de Liga annual table",
            480, len(remaining), schedule_average_elo_weight(remaining, elo_scope),
        )

    if league_code == "COL":
        if len(fixtures) <= 204:
            return None
        current_phase = fixtures[204:]
        current_table = build_table(current_phase)
        team_keys = sorted(current_table)
        if len(team_keys) != 20 or any(key not in elo_scope.teams for key in team_keys):
            return None
        remaining = remaining_stage_schedule(team_keys, UNORDERED, 1, current_phase)
        names = {key: current_table[key].name for key in team_keys}
        position_counts = {key: [0] * len(team_keys) for key in team_keys}
        point_totals = {key: 0 for key in team_keys}
        title_counts = {key: 0 for key in team_keys}
        split_keys = ("col_quadrangular_a", "col_quadrangular_b")
        split_counts = {key: [0, 0] for key in team_keys}

        for _ in range(runs):
            points = {key: current_table[key].points for key in team_keys}
            gf = {key: current_table[key].goals_for for key in team_keys}
            ga = {key: current_table[key].goals_against for key in team_keys}
            apply_simulated_schedule(remaining, match_model, rng, points, gf, ga)
            regular_order = ranking_for_values(team_keys, points, gf, ga, names)
            top8 = regular_order[:8]
            pool = list(top8[2:])
            rng.shuffle(pool)
            group_a = [top8[0], *pool[:3]]
            group_b = [top8[1], *pool[3:]]
            for key in group_a:
                split_counts[key][0] += 1
            for key in group_b:
                split_counts[key][1] += 1

            winners: list[str] = []
            for group in (group_a, group_b):
                gp = {key: 0 for key in group}
                ggf = {key: 0 for key in group}
                gga = {key: 0 for key in group}
                schedule = full_hypothetical_stage_schedule(
                    group, StageSpec(4, ORDERED, 1), rng
                )
                apply_simulated_schedule(schedule, match_model, rng, gp, ggf, gga)
                winners.append(
                    ranking_for_values(group, gp, ggf, gga, names)[0]
                )
            finalist_a, finalist_b = winners
            champion = two_leg_winner(
                finalist_a, finalist_b, match_model, elo_scope, rng
            )
            title_counts[champion] += 1
            for pos, key in enumerate(regular_order):
                position_counts[key][pos] += 1
                point_totals[key] += points[key]

        return special_meta_and_rows(
            entry, elo_scope, current_table, position_counts, point_totals,
            title_counts, split_keys, split_counts, runs,
            "19-match Clausura + two semifinal quadrangulares + two-leg final",
            190, len(remaining) + 26,
            schedule_average_elo_weight(remaining, elo_scope),
        )

    if league_code == "MEX":
        current_table = build_table(fixtures)
        team_keys = sorted(current_table)
        if len(team_keys) != 18 or any(key not in elo_scope.teams for key in team_keys):
            return None
        remaining = remaining_stage_schedule(team_keys, UNORDERED, 1, fixtures)
        names = {key: current_table[key].name for key in team_keys}
        position_counts = {key: [0] * len(team_keys) for key in team_keys}
        point_totals = {key: 0 for key in team_keys}
        title_counts = {key: 0 for key in team_keys}
        split_keys = ("mex_liguilla_top8",)
        split_counts = {key: [0] for key in team_keys}

        for _ in range(runs):
            points = {key: current_table[key].points for key in team_keys}
            gf = {key: current_table[key].goals_for for key in team_keys}
            ga = {key: current_table[key].goals_against for key in team_keys}
            apply_simulated_schedule(remaining, match_model, rng, points, gf, ga)
            regular_order = ranking_for_values(team_keys, points, gf, ga, names)
            seeds = seed_rank_map(regular_order)
            top8 = regular_order[:8]
            for key in top8:
                split_counts[key][0] += 1

            quarter_pairs = [
                (top8[0], top8[7]),
                (top8[1], top8[6]),
                (top8[2], top8[5]),
                (top8[3], top8[4]),
            ]
            semifinalists = []
            for higher, lower in quarter_pairs:
                semifinalists.append(
                    two_leg_winner(
                        lower, higher, match_model, elo_scope, rng,
                        higher_seed=higher,
                    )
                )
            semifinalists.sort(key=lambda key: seeds[key])
            semifinal_pairs = [
                (semifinalists[0], semifinalists[-1]),
                (semifinalists[1], semifinalists[-2]),
            ]
            finalists = []
            for higher, lower in semifinal_pairs:
                finalists.append(
                    two_leg_winner(
                        lower, higher, match_model, elo_scope, rng,
                        higher_seed=higher,
                    )
                )
            finalists.sort(key=lambda key: seeds[key])
            champion = two_leg_winner(
                finalists[1], finalists[0], match_model, elo_scope, rng
            )
            title_counts[champion] += 1
            for pos, key in enumerate(regular_order):
                position_counts[key][pos] += 1
                point_totals[key] += points[key]

        return special_meta_and_rows(
            entry, elo_scope, current_table, position_counts, point_totals,
            title_counts, split_keys, split_counts, runs,
            "17-match Apertura + top-8 two-leg Liguilla",
            153, len(remaining) + 14,
            schedule_average_elo_weight(remaining, elo_scope),
        )

    if league_code == "PER":
        if len(fixtures) <= 153:
            return None
        apertura = fixtures[:153]
        clausura_finished = fixtures[153:]
        apertura_table = build_table(apertura)
        clausura_table = build_table(clausura_finished)
        team_keys = sorted(set(apertura_table) | set(clausura_table))
        if len(team_keys) != 18 or any(key not in elo_scope.teams for key in team_keys):
            return None
        remaining = remaining_stage_schedule(team_keys, UNORDERED, 1, clausura_finished)
        current_table = combine_tables(apertura_table, clausura_table)
        names = {key: current_table[key].name for key in team_keys}
        apertura_order = current_ranking(apertura_table)
        apertura_winner = apertura_order[0]
        position_counts = {key: [0] * len(team_keys) for key in team_keys}
        point_totals = {key: 0 for key in team_keys}
        title_counts = {key: 0 for key in team_keys}
        split_keys = ("per_national_playoffs",)
        split_counts = {key: [0] for key in team_keys}

        for _ in range(runs):
            clausura_points = {
                key: clausura_table.get(key, TeamStanding(key, names[key])).points
                for key in team_keys
            }
            clausura_gf = {
                key: clausura_table.get(key, TeamStanding(key, names[key])).goals_for
                for key in team_keys
            }
            clausura_ga = {
                key: clausura_table.get(key, TeamStanding(key, names[key])).goals_against
                for key in team_keys
            }
            apply_simulated_schedule(
                remaining, match_model, rng,
                clausura_points, clausura_gf, clausura_ga,
            )
            clausura_order = ranking_for_values(
                team_keys, clausura_points, clausura_gf, clausura_ga, names
            )
            clausura_winner = clausura_order[0]

            annual_points = {
                key: apertura_table[key].points + clausura_points[key]
                for key in team_keys
            }
            annual_gf = {
                key: apertura_table[key].goals_for + clausura_gf[key]
                for key in team_keys
            }
            annual_ga = {
                key: apertura_table[key].goals_against + clausura_ga[key]
                for key in team_keys
            }
            annual_order = ranking_for_values(
                team_keys, annual_points, annual_gf, annual_ga, names
            )
            annual_rank = seed_rank_map(annual_order)

            if apertura_winner == clausura_winner:
                champion = apertura_winner
                qualifiers = {annual_order[1], annual_order[2], annual_order[3]}
            else:
                qualifiers = {annual_order[0], annual_order[1]}
                if annual_rank[apertura_winner] <= 8:
                    qualifiers.add(apertura_winner)
                if annual_rank[clausura_winner] <= 8:
                    qualifiers.add(clausura_winner)
                q = sorted(qualifiers, key=lambda key: annual_rank[key])
                if len(q) >= 4:
                    sf1 = two_leg_winner(
                        q[-1], q[0], match_model, elo_scope, rng
                    )
                    sf2 = two_leg_winner(
                        q[-2], q[1], match_model, elo_scope, rng
                    )
                    finalists = sorted((sf1, sf2), key=lambda key: annual_rank[key])
                    champion = two_leg_winner(
                        finalists[1], finalists[0], match_model, elo_scope, rng
                    )
                elif len(q) == 3:
                    direct = q[0]
                    semi = two_leg_winner(
                        q[2], q[1], match_model, elo_scope, rng
                    )
                    finalists = sorted((direct, semi), key=lambda key: annual_rank[key])
                    champion = two_leg_winner(
                        finalists[1], finalists[0], match_model, elo_scope, rng
                    )
                elif len(q) == 2:
                    champion = two_leg_winner(
                        q[1], q[0], match_model, elo_scope, rng
                    )
                else:
                    champion = q[0]

            for key in qualifiers:
                split_counts[key][0] += 1
            title_counts[champion] += 1
            for pos, key in enumerate(annual_order):
                position_counts[key][pos] += 1
                point_totals[key] += annual_points[key]

        return special_meta_and_rows(
            entry, elo_scope, current_table, position_counts, point_totals,
            title_counts, split_keys, split_counts, runs,
            "Apertura + Clausura + national two-leg Play-Offs",
            306, len(remaining) + 6,
            schedule_average_elo_weight(remaining, elo_scope),
        )

    if league_code == "URU":
        if len(fixtures) <= 177:
            return None
        apertura = fixtures[:120]
        intermedio_group = fixtures[120:176]
        clausura_finished = fixtures[177:]
        apertura_table = build_table(apertura)
        annual_base = build_table([*apertura, *intermedio_group])
        clausura_table = build_table(clausura_finished)
        team_keys = sorted(set(annual_base) | set(clausura_table))
        if len(team_keys) != 16 or any(key not in elo_scope.teams for key in team_keys):
            return None
        remaining = remaining_stage_schedule(team_keys, UNORDERED, 1, clausura_finished)
        current_table = combine_tables(annual_base, clausura_table)
        names = {key: current_table[key].name for key in team_keys}
        apertura_winner = current_ranking(apertura_table)[0]
        position_counts = {key: [0] * len(team_keys) for key in team_keys}
        point_totals = {key: 0 for key in team_keys}
        title_counts = {key: 0 for key in team_keys}
        split_keys = ("uru_championship_playoff",)
        split_counts = {key: [0] for key in team_keys}

        for _ in range(runs):
            clausura_points = {
                key: clausura_table.get(key, TeamStanding(key, names[key])).points
                for key in team_keys
            }
            clausura_gf = {
                key: clausura_table.get(key, TeamStanding(key, names[key])).goals_for
                for key in team_keys
            }
            clausura_ga = {
                key: clausura_table.get(key, TeamStanding(key, names[key])).goals_against
                for key in team_keys
            }
            apply_simulated_schedule(
                remaining, match_model, rng,
                clausura_points, clausura_gf, clausura_ga,
            )
            clausura_order = ranking_for_values(
                team_keys, clausura_points, clausura_gf, clausura_ga, names
            )
            clausura_winner = clausura_order[0]
            annual_points = {
                key: annual_base[key].points + clausura_points[key]
                for key in team_keys
            }
            annual_gf = {
                key: annual_base[key].goals_for + clausura_gf[key]
                for key in team_keys
            }
            annual_ga = {
                key: annual_base[key].goals_against + clausura_ga[key]
                for key in team_keys
            }
            annual_order = ranking_for_values(
                team_keys, annual_points, annual_gf, annual_ga, names
            )
            annual_leader = annual_order[0]

            if apertura_winner == clausura_winner:
                semifinal_winner = apertura_winner
            else:
                semifinal_winner = neutral_match_winner(
                    apertura_winner, clausura_winner,
                    match_model, elo_scope, rng,
                )
            if semifinal_winner == annual_leader:
                champion = semifinal_winner
            else:
                champion = two_leg_winner(
                    semifinal_winner, annual_leader,
                    match_model, elo_scope, rng,
                )
            for key in {apertura_winner, clausura_winner, annual_leader}:
                split_counts[key][0] += 1
            title_counts[champion] += 1
            for pos, key in enumerate(annual_order):
                position_counts[key][pos] += 1
                point_totals[key] += annual_points[key]

        return special_meta_and_rows(
            entry, elo_scope, current_table, position_counts, point_totals,
            title_counts, split_keys, split_counts, runs,
            "Apertura + Intermedio + Clausura + championship playoff",
            296, len(remaining) + 3,
            schedule_average_elo_weight(remaining, elo_scope),
        )

    if league_code == "USA":
        current_table = build_table(fixtures)
        team_keys = sorted(current_table)
        if len(team_keys) != 30 or any(key not in elo_scope.teams for key in team_keys):
            return None
        east = resolve_group(team_keys, MLS_EAST_LABELS, "MLS East")
        west = resolve_group(team_keys, MLS_WEST_LABELS, "MLS West")
        set_east = set(east)
        set_west = set(west)
        east_completed = [
            row for row in fixtures
            if norm(row.get("home_team")) in set_east
            and norm(row.get("away_team")) in set_east
        ]
        west_completed = [
            row for row in fixtures
            if norm(row.get("home_team")) in set_west
            and norm(row.get("away_team")) in set_west
        ]
        remaining = [
            *remaining_stage_schedule(east, ORDERED, 1, east_completed),
            *remaining_stage_schedule(west, ORDERED, 1, west_completed),
            *synthetic_cross_group_schedule(east, west, fixtures, 6),
        ]
        names = {key: current_table[key].name for key in team_keys}
        position_counts = {key: [0] * len(team_keys) for key in team_keys}
        point_totals = {key: 0 for key in team_keys}
        title_counts = {key: 0 for key in team_keys}
        split_keys = ("usa_eastern_conference", "usa_western_conference")
        split_counts = {key: [0, 0] for key in team_keys}
        for key in east:
            split_counts[key][0] = runs
        for key in west:
            split_counts[key][1] = runs

        for _ in range(runs):
            points = {key: current_table[key].points for key in team_keys}
            gf = {key: current_table[key].goals_for for key in team_keys}
            ga = {key: current_table[key].goals_against for key in team_keys}
            apply_simulated_schedule(remaining, match_model, rng, points, gf, ga)
            east_order = ranking_for_values(east, points, gf, ga, names)
            west_order = ranking_for_values(west, points, gf, ga, names)
            east_seed = seed_rank_map(east_order)
            west_seed = seed_rank_map(west_order)

            def conference_champion(
                order: list[str],
                seed_map: dict[str, int],
            ) -> str:
                wildcard = single_match_winner(
                    order[7], order[8], match_model, elo_scope, rng
                )
                round_one_pairs = [
                    (order[0], wildcard),
                    (order[1], order[6]),
                    (order[2], order[5]),
                    (order[3], order[4]),
                ]
                r1 = [
                    best_of_three_winner(higher, lower, match_model, elo_scope, rng)
                    for higher, lower in round_one_pairs
                ]
                semi1_home = min((r1[0], r1[3]), key=lambda key: seed_map[key])
                semi1_away = max((r1[0], r1[3]), key=lambda key: seed_map[key])
                semi2_home = min((r1[1], r1[2]), key=lambda key: seed_map[key])
                semi2_away = max((r1[1], r1[2]), key=lambda key: seed_map[key])
                semi1 = single_match_winner(
                    semi1_home, semi1_away, match_model, elo_scope, rng
                )
                semi2 = single_match_winner(
                    semi2_home, semi2_away, match_model, elo_scope, rng
                )
                higher = min((semi1, semi2), key=lambda key: seed_map[key])
                lower = max((semi1, semi2), key=lambda key: seed_map[key])
                return single_match_winner(
                    higher, lower, match_model, elo_scope, rng
                )

            east_champion = conference_champion(east_order, east_seed)
            west_champion = conference_champion(west_order, west_seed)
            if points[east_champion] > points[west_champion]:
                home, away = east_champion, west_champion
            elif points[west_champion] > points[east_champion]:
                home, away = west_champion, east_champion
            else:
                home, away = sorted((east_champion, west_champion))
            champion = single_match_winner(
                home, away, match_model, elo_scope, rng
            )
            title_counts[champion] += 1
            global_order = ranking_for_values(
                team_keys, points, gf, ga, names
            )
            for pos, key in enumerate(global_order):
                position_counts[key][pos] += 1
                point_totals[key] += points[key]

        return special_meta_and_rows(
            entry, elo_scope, current_table, position_counts, point_totals,
            title_counts, split_keys, split_counts, runs,
            "34-match conference season + 18-team Audi MLS Cup Playoffs",
            510, len(remaining) + 33,
            schedule_average_elo_weight(remaining, elo_scope),
        )

    return None


def simulate_league(
    entry: dict[str, Any],
    fixtures: list[dict[str, Any]],
    runs: int,
    elo_scope: EloScope,
) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    league_code = str(entry.get("league_code") or "")
    season = str(entry.get("app_season") or entry.get("target_app_season") or "")
    rule = RULES[league_code]

    if league_code in SPECIAL_SIMULATION_CODES:
        return simulate_special_league(entry, fixtures, runs, elo_scope)

    current_table = build_table(fixtures)
    add_extra_current_teams(current_table, league_code, elo_scope)
    if len(current_table) < 8 or len(fixtures) < 10:
        return None
    team_keys = sorted(current_table)
    if any(key not in elo_scope.teams for key in team_keys):
        return None

    regular_target = stage_total(len(team_keys), rule.regular_mode, rule.regular_meetings)
    regular_finished = fixtures[: min(len(fixtures), regular_target)]
    post_regular_finished = fixtures[regular_target:] if len(fixtures) > regular_target else []
    regular_table = build_table(regular_finished)
    add_extra_current_teams(regular_table, league_code, elo_scope)
    if set(regular_table) != set(team_keys):
        return None

    history = LeagueHistory(fixtures)
    match_model = MatchModel(history, elo_scope, league_code, season)
    regular_remaining = remaining_stage_schedule(
        team_keys,
        rule.regular_mode,
        rule.regular_meetings,
        regular_finished,
    )

    # Ensure every future regular pairing can be modeled before allocating large run arrays.
    model_weights: list[float] = []
    mature_future = 0
    for home, away in regular_remaining:
        model = match_model.cdfs(home, away)
        if model is None:
            return None
        _, _, mature, weight = model
        mature_future += 1 if mature else 0
        model_weights.append(weight)

    regular_complete = len(regular_finished) >= regular_target
    current_group_keys: list[list[str]] | None = None
    current_group_specs: list[StageSpec] | None = None
    split_remaining_fixed: list[list[tuple[str, str]]] | None = None
    split_base_points: dict[str, int] | None = None

    if rule.split_groups and regular_complete:
        reg_order = current_ranking(regular_table)
        current_group_keys = []
        current_group_specs = list(rule.split_groups)
        split_remaining_fixed = []
        cursor = 0
        for spec in rule.split_groups:
            group = reg_order[cursor: cursor + spec.size]
            cursor += spec.size
            current_group_keys.append(group)
            relevant_finished = [
                fixture for fixture in post_regular_finished
                if norm(fixture.get("home_team")) in group
                and norm(fixture.get("away_team")) in group
            ]
            split_remaining_fixed.append(
                remaining_stage_schedule(group, spec.mode, spec.meetings, relevant_finished)
            )
            for home, away in split_remaining_fixed[-1]:
                model = match_model.cdfs(home, away)
                if model is None:
                    return None
                _, _, mature, weight = model
                mature_future += 1 if mature else 0
                model_weights.append(weight)

        # Reconstruct second-phase points from the end of the regular season so competition
        # specific carry rules are respected even after real split matches have already been played.
        split_base_points = {}
        for group_index, group in enumerate(current_group_keys):
            for key in group:
                split_base_points[key] = transformed_group_points(
                    league_code,
                    group_index,
                    regular_table[key].points,
                )
            relevant_finished = [
                fixture for fixture in post_regular_finished
                if norm(fixture.get("home_team")) in group
                and norm(fixture.get("away_team")) in group
            ]
            for fixture in relevant_finished:
                goals = fixture_goals(fixture)
                if goals is None:
                    continue
                apply_points_only(
                    norm(fixture.get("home_team")),
                    norm(fixture.get("away_team")),
                    goals[0],
                    goals[1],
                    split_base_points,
                )

    names = {key: current_table[key].name for key in team_keys}
    position_counts = {key: [0] * len(team_keys) for key in team_keys}
    point_totals = {key: 0 for key in team_keys}
    split_group_keys = SPLIT_GROUP_KEYS.get(league_code, ())
    if rule.split_groups and len(split_group_keys) != len(rule.split_groups):
        raise RuntimeError(
            f"{league_code}: split group semantics mismatch "
            f"keys={len(split_group_keys)} stages={len(rule.split_groups)}"
        )
    split_group_counts = {
        key: [0] * len(split_group_keys)
        for key in team_keys
    }

    # Fixed regular-stage Monte Carlo draws are generated in vectors for speed.
    regular_models: list[tuple[str, str, list[int], list[int]]] = []
    for occurrence, (home, away) in enumerate(regular_remaining):
        model = match_model.cdfs(home, away)
        if model is None:
            return None
        home_cdf, away_cdf, _, _ = model
        seed = f"{league_code}|{season}|regular|{home}|{away}|{occurrence}|{runs}|{MODEL_VERSION}"
        rng = random.Random(int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16], 16))
        regular_models.append((
            home,
            away,
            simulate_counts(rng, home_cdf, runs),
            simulate_counts(rng, away_cdf, runs),
        ))

    base_table = current_table if regular_complete else regular_table
    split_rng = random.Random(
        int(hashlib.sha256(
            f"{league_code}|{season}|split|{runs}|{MODEL_VERSION}".encode("utf-8")
        ).hexdigest()[:16], 16)
    )

    for run_idx in range(runs):
        points = (
            dict(split_base_points)
            if split_base_points is not None
            else {key: base_table[key].points for key in team_keys}
        )
        goals_for = {key: base_table[key].goals_for for key in team_keys}
        goals_against = {key: base_table[key].goals_against for key in team_keys}

        # Before the split, simulate the rest of the regular phase from its true state.
        if not regular_complete:
            for home, away, home_draws, away_draws in regular_models:
                apply_score(
                    home, away, home_draws[run_idx], away_draws[run_idx],
                    points, goals_for, goals_against,
                )

        if not rule.split_groups:
            final_order = ranking_for_values(team_keys, points, goals_for, goals_against, names)
        else:
            if regular_complete:
                assert current_group_keys is not None
                assert current_group_specs is not None
                assert split_remaining_fixed is not None
                groups = current_group_keys
                specs = current_group_specs
                schedules = split_remaining_fixed
            else:
                regular_order = ranking_for_values(team_keys, points, goals_for, goals_against, names)
                groups = []
                specs = list(rule.split_groups)
                cursor = 0
                for spec in specs:
                    groups.append(regular_order[cursor: cursor + spec.size])
                    cursor += spec.size
                for group_index, group in enumerate(groups):
                    for key in group:
                        points[key] = transformed_group_points(
                            league_code,
                            group_index,
                            points[key],
                        )
                if rule.reset_split_goals:
                    for key in team_keys:
                        goals_for[key] = 0
                        goals_against[key] = 0
                schedules = [
                    full_hypothetical_stage_schedule(group, spec, split_rng)
                    for group, spec in zip(groups, specs)
                ]

            for group_index, group in enumerate(groups):
                for key in group:
                    split_group_counts[key][group_index] += 1

            final_order = []
            for group, schedule in zip(groups, schedules):
                for home, away in schedule:
                    score = match_model.draw(home, away, split_rng)
                    if score is None:
                        return None
                    apply_score(
                        home, away, score[0], score[1],
                        points, goals_for, goals_against,
                    )
                final_order.extend(
                    ranking_for_values(group, points, goals_for, goals_against, names)
                )

        for position, key in enumerate(final_order):
            position_counts[key][position] += 1
            point_totals[key] += points[key]

    current_order = current_ranking(current_table)
    current_positions = {key: idx + 1 for idx, key in enumerate(current_order)}

    team_rows: list[dict[str, Any]] = []
    for key in team_keys:
        counts = position_counts[key]
        team = current_table[key]
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
                "expected_position": sum(
                    (idx + 1) * count for idx, count in enumerate(counts)
                ) / runs,
                "expected_points": point_totals[key] / runs,
                "title_probability": counts[0] / runs,
                "top2_probability": sum(counts[: min(2, len(counts))]) / runs,
                "top4_probability": sum(counts[: min(4, len(counts))]) / runs,
                "position_probabilities_json": json.dumps(
                    [count / runs for count in counts],
                    separators=(",", ":"),
                ),
                "split_group_probabilities_json": json.dumps(
                    [count / runs for count in split_group_counts[key]],
                    separators=(",", ":"),
                ),
            }
        )

    future_count = len(regular_remaining)
    if split_remaining_fixed is not None:
        future_count += sum(len(rows) for rows in split_remaining_fixed)
    elif rule.split_groups:
        # Pre-split count varies by group membership only in identity, not in size.
        future_count += sum(stage_total(spec.size, spec.mode, spec.meetings) for spec in rule.split_groups)

    meta = {
        "league_code": league_code,
        "country": str(entry.get("country") or ""),
        "league_name": str(entry.get("league") or ""),
        "season": season,
        "team_count": len(team_keys),
        "current_completed_matches": len(fixtures),
        "remaining_matches": future_count,
        "simulation_runs": runs,
        "model_version": MODEL_VERSION,
        "elo_model_version": elo_scope.model_version,
        "average_elo_weight": sum(model_weights) / max(1, len(model_weights)),
        "tie_break_model": TIE_BREAK_MODEL,
        "mature_fixture_count": mature_future,
        "total_simulated_fixtures": future_count,
        "format_label": rule.format_label,
        "regular_stage_target_matches": regular_target,
        "split_group_count": len(rule.split_groups),
        "split_group_keys_json": json.dumps(split_group_keys, separators=(",", ":")),
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
    expected_entries = current_entries(index)
    expected_codes = {str(entry.get("league_code") or "") for entry in expected_entries}

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
                format_label TEXT NOT NULL,
                regular_stage_target_matches INTEGER NOT NULL,
                split_group_count INTEGER NOT NULL,
                split_group_keys_json TEXT NOT NULL,
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
                split_group_probabilities_json TEXT NOT NULL,
                generated_at_ms INTEGER NOT NULL,
                PRIMARY KEY (league_code, season, team_key)
            );
            CREATE INDEX idx_prepared_league_team_title
              ON prepared_league_team_simulations(league_code, season, title_probability DESC);
            """
        )

        materialized_codes: set[str] = set()
        team_count = 0
        e0_rows: list[dict[str, Any]] = []

        for entry in expected_entries:
            league_code = str(entry.get("league_code") or "")
            season = str(entry.get("app_season") or entry.get("target_app_season") or "")
            elo_scope = load_elo_scope(con, league_code, season)
            if elo_scope is None:
                raise SystemExit(f"{league_code}: missing Elo scope")

            payload = load_fixture_payload(entry)
            if payload is None:
                raise SystemExit(f"{league_code}: missing fixture payload")
            fixtures = current_season_fixtures(entry, payload)
            result = simulate_league(entry, fixtures, runs, elo_scope)
            if result is None:
                raise SystemExit(f"{league_code}: league simulation contract failed")
            meta, rows = result

            con.execute(
                """
                INSERT INTO prepared_league_simulation_meta(
                    league_code,country,league_name,season,team_count,
                    current_completed_matches,remaining_matches,simulation_runs,
                    model_version,elo_model_version,average_elo_weight,
                    tie_break_model,mature_fixture_count,total_simulated_fixtures,
                    format_label,regular_stage_target_matches,split_group_count,
                    split_group_keys_json,generated_at_ms
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    meta["league_code"], meta["country"], meta["league_name"], meta["season"],
                    meta["team_count"], meta["current_completed_matches"], meta["remaining_matches"],
                    meta["simulation_runs"], meta["model_version"], meta["elo_model_version"],
                    meta["average_elo_weight"], meta["tie_break_model"],
                    meta["mature_fixture_count"], meta["total_simulated_fixtures"],
                    meta["format_label"], meta["regular_stage_target_matches"],
                    meta["split_group_count"], meta["split_group_keys_json"], generated_at_ms,
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
                    split_group_probabilities_json,generated_at_ms
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        meta["league_code"], meta["season"], row["team_key"], row["team_name"], row["team_logo"],
                        row["elo_rating"], row["current_position"], row["current_played"], row["current_points"],
                        row["current_goal_difference"], row["current_goals_for"],
                        row["expected_position"], row["expected_points"], row["title_probability"],
                        row["top2_probability"], row["top4_probability"], row["position_probabilities_json"],
                        row["split_group_probabilities_json"], generated_at_ms,
                    )
                    for row in rows
                ],
            )
            materialized_codes.add(league_code)
            team_count += len(rows)
            if league_code == "E0":
                e0_rows = rows

        if materialized_codes != expected_codes:
            raise SystemExit(
                "Not all active domestic leagues materialized: "
                f"missing={sorted(expected_codes-materialized_codes)} "
                f"extra={sorted(materialized_codes-expected_codes)}"
            )

        con.commit()
        quick = con.execute("PRAGMA quick_check").fetchone()
        if not quick or quick[0] != "ok":
            raise SystemExit(f"Prepared DB quick_check failed after league simulation: {quick}")

        print(
            "PREPARED_LEAGUE_SIMULATION_OK",
            f"model={MODEL_VERSION}",
            f"elo={ELO_MODEL_VERSION}",
            f"runs={runs}",
            f"leagues={len(materialized_codes)}",
            f"teams={team_count}",
            "codes=" + ",".join(sorted(materialized_codes)),
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
