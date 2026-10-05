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
}

# Semantic meaning of each post-regular-season group. These keys are persisted for the
# Android UAT UI so split leagues are not presented like ordinary round-robin leagues.
SPLIT_GROUP_KEYS: dict[str, tuple[str, ...]] = {
    "AUT": ("championship_group", "qualification_group"),
    "CYP": ("championship_group", "relegation_group"),
    "EGY": ("championship_group", "relegation_group"),
    "G1": ("championship_group", "europe_group", "relegation_group"),
    "ISR": ("championship_group", "relegation_group"),
    "SC0": ("championship_group", "relegation_group"),
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
        if str(row.get("stats_role") or "") == "current_target"
        and str(row.get("lifecycle") or "") == "active"
        and (ROOT / str(row.get("cache_path") or "")).is_file()
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


def simulate_league(
    entry: dict[str, Any],
    fixtures: list[dict[str, Any]],
    runs: int,
    elo_scope: EloScope,
) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    league_code = str(entry.get("league_code") or "")
    season = str(entry.get("app_season") or entry.get("target_app_season") or "")
    rule = RULES[league_code]

    current_table = build_table(fixtures)
    if len(current_table) < 8 or len(fixtures) < 10:
        return None
    team_keys = sorted(current_table)
    if any(key not in elo_scope.teams for key in team_keys):
        return None

    regular_target = stage_total(len(team_keys), rule.regular_mode, rule.regular_meetings)
    regular_finished = fixtures[: min(len(fixtures), regular_target)]
    post_regular_finished = fixtures[regular_target:] if len(fixtures) > regular_target else []
    regular_table = build_table(regular_finished)
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
        points = {key: base_table[key].points for key in team_keys}
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
