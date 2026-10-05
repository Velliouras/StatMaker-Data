#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import sqlite3
import time
import unicodedata
import urllib.parse
import urllib.request
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "uefa_simulation_2026_27.json"

MODEL_VERSION = "uefa-club-monte-carlo-v1"
STRENGTH_MODEL = "uefa-seeding-elo-v1"
TIE_BREAK_MODEL = "points-goal-difference-goals-for"
API_BASE = "https://v3.football.api-sports.io"
FINISHED = {"FT", "AET", "PEN"}
RATING_SCALE = 400.0
HOME_ADVANTAGE = 55.0
K_FACTOR = 24.0
GOAL_LOG_COEFF = 0.55
DEFAULT_HOME_GOALS = 1.55
DEFAULT_AWAY_GOALS = 1.30


def norm(value: Any) -> str:
    text = unicodedata.normalize("NFD", str(value or ""))
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn").lower()
    return "".join(ch for ch in text if ch.isalnum())


def api_get(api_key: str, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    url = f"{API_BASE}/{endpoint}?{query}"
    req = urllib.request.Request(url, headers={"x-apisports-key": api_key})
    with urllib.request.urlopen(req, timeout=45) as response:
        payload = json.loads(response.read().decode("utf-8"))
    errors = payload.get("errors")
    if errors:
        raise RuntimeError(f"API-Football {endpoint} error: {errors}")
    return payload


def response_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("response") or []
    return [row for row in rows if isinstance(row, dict)]


def find_league_id(api_key: str, competition: dict[str, Any], season: int) -> int:
    del season  # league IDs are stable; season is applied only on the fixtures request.
    competition_id = str(competition.get("competitionId") or "")
    wanted = str(competition["name"])

    preferred_names = {
        "champions_league": {
            "uefachampionsleague": 4.0,
            "championsleague": 3.0,
        },
        "europa_league": {
            "uefaeuropaleague": 4.0,
            "europaleague": 3.0,
        },
        "conference_league": {
            "uefaconferenceleague": 4.0,
            "uefaeuropaconferenceleague": 4.0,
            "conferenceleague": 3.0,
            "europaconferenceleague": 3.0,
        },
    }
    blocked_tokens = (
        "ofc", "afc", "caf", "concacaf", "conmebol",
        "women", "womens", "youth", "u19", "u20", "u21",
    )

    candidates: list[tuple[float, int, str, str]] = []
    seen_ids: set[int] = set()
    for term in competition.get("apiSearchTerms") or [wanted]:
        # API-Football does not allow search+season in the same leagues request.
        payload = api_get(api_key, "leagues", {"search": term})
        for row in response_items(payload):
            league = row.get("league") if isinstance(row.get("league"), dict) else {}
            country = row.get("country") if isinstance(row.get("country"), dict) else {}
            league_id = league.get("id")
            name = str(league.get("name") or "").strip()
            country_name = str(country.get("name") or "").strip()
            if league_id is None or not name:
                continue
            league_id = int(league_id)
            if league_id in seen_ids:
                continue
            seen_ids.add(league_id)

            key = norm(name)
            if any(token in key for token in blocked_tokens):
                continue
            if competition_id == "champions_league" and (
                "europa" in key or "conference" in key
            ):
                continue
            if competition_id == "europa_league" and (
                "europa" not in key or "conference" in key
            ):
                continue
            if competition_id == "conference_league" and "conference" not in key:
                continue

            preferred = preferred_names.get(competition_id, {}).get(key, 0.0)
            similarity = SequenceMatcher(None, norm(wanted), key).ratio()
            # Exact UEFA / canonical competition naming dominates fuzzy similarity.
            score = preferred + similarity
            candidates.append((score, league_id, name, country_name))

    if not candidates:
        raise RuntimeError(
            f"{wanted}: no safe UEFA competition candidate returned by API-Football"
        )
    candidates.sort(reverse=True)
    score, league_id, name, country = candidates[0]
    if score < 1.65:
        detail = " | ".join(
            f"{candidate_name}#{candidate_id}:{candidate_score:.3f}"
            for candidate_score, candidate_id, candidate_name, _ in candidates[:8]
        )
        raise RuntimeError(
            f"{wanted}: unsafe API league match {name} ({country}) "
            f"score={score:.3f}; candidates={detail}"
        )
    print("UEFA_API_LEAGUE", wanted, league_id, name, country, f"score={score:.3f}")
    return league_id


def participant_maps(competition: dict[str, Any]) -> tuple[dict[str, str], dict[str, int], dict[str, float]]:
    alias_to_canonical: dict[str, str] = {}
    pot_by_team: dict[str, int] = {}
    rating_by_team: dict[str, float] = {}
    bases = [float(v) for v in competition.get("potBaseRatings") or []]
    for pot_index, teams in enumerate(competition.get("pots") or [], start=1):
        base = bases[pot_index - 1]
        for team in teams:
            canonical = str(team)
            alias_to_canonical[norm(canonical)] = canonical
            pot_by_team[canonical] = pot_index
            rating_by_team[canonical] = base
    for alias, canonical in (competition.get("aliases") or {}).items():
        if canonical in pot_by_team:
            alias_to_canonical[norm(alias)] = canonical
    return alias_to_canonical, pot_by_team, rating_by_team


def resolve_team(name: str, alias_to_canonical: dict[str, str], canonicals: list[str]) -> str | None:
    key = norm(name)
    direct = alias_to_canonical.get(key)
    if direct:
        return direct
    scored = sorted(
        ((SequenceMatcher(None, key, norm(team)).ratio(), team) for team in canonicals),
        reverse=True,
    )
    if not scored or scored[0][0] < 0.86:
        return None
    if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.05:
        return None
    return scored[0][1]


@dataclass
class Fixture:
    fixture_id: int
    date: str
    status: str
    round_name: str
    home: str
    away: str
    home_goals: int | None
    away_goals: int | None
    home_logo: str | None
    away_logo: str | None


@dataclass
class TableRow:
    played: int = 0
    points: int = 0
    gf: int = 0
    ga: int = 0

    @property
    def gd(self) -> int:
        return self.gf - self.ga


def parse_fixtures(
    payload: dict[str, Any],
    competition: dict[str, Any],
) -> tuple[list[Fixture], dict[str, str]]:
    alias_map, pot_by_team, _ = participant_maps(competition)
    canonicals = list(pot_by_team)
    start = str(competition["phaseStart"])
    end = str(competition["phaseEnd"])
    rows: list[Fixture] = []
    logos: dict[str, str] = {}
    unresolved: set[str] = set()

    for item in response_items(payload):
        fixture = item.get("fixture") if isinstance(item.get("fixture"), dict) else {}
        league = item.get("league") if isinstance(item.get("league"), dict) else {}
        teams = item.get("teams") if isinstance(item.get("teams"), dict) else {}
        goals = item.get("goals") if isinstance(item.get("goals"), dict) else {}
        date = str(fixture.get("date") or "")[:10]
        if not date or date < start or date > end:
            continue
        home_obj = teams.get("home") if isinstance(teams.get("home"), dict) else {}
        away_obj = teams.get("away") if isinstance(teams.get("away"), dict) else {}
        home_raw = str(home_obj.get("name") or "").strip()
        away_raw = str(away_obj.get("name") or "").strip()
        home = resolve_team(home_raw, alias_map, canonicals)
        away = resolve_team(away_raw, alias_map, canonicals)
        if home is None or away is None:
            if home is None and home_raw:
                unresolved.add(home_raw)
            if away is None and away_raw:
                unresolved.add(away_raw)
            continue
        round_name = str(league.get("round") or "")
        # Date window plus final-36 identity is authoritative enough to exclude qualifying rounds.
        status = str((fixture.get("status") or {}).get("short") or "")
        hg = goals.get("home")
        ag = goals.get("away")
        rows.append(
            Fixture(
                fixture_id=int(fixture.get("id") or 0),
                date=date,
                status=status,
                round_name=round_name,
                home=home,
                away=away,
                home_goals=None if hg is None else int(hg),
                away_goals=None if ag is None else int(ag),
                home_logo=str(home_obj.get("logo") or "") or None,
                away_logo=str(away_obj.get("logo") or "") or None,
            )
        )
        if home_obj.get("logo"):
            logos[home] = str(home_obj["logo"])
        if away_obj.get("logo"):
            logos[away] = str(away_obj["logo"])

    if unresolved:
        raise RuntimeError(
            f"{competition['name']}: unresolved league-phase participant names: {sorted(unresolved)}"
        )
    rows.sort(key=lambda f: (f.date, f.fixture_id))
    return rows, logos


def apply_result(table: dict[str, TableRow], home: str, away: str, hg: int, ag: int) -> None:
    h = table[home]
    a = table[away]
    h.played += 1
    a.played += 1
    h.gf += hg
    h.ga += ag
    a.gf += ag
    a.ga += hg
    if hg > ag:
        h.points += 3
    elif hg < ag:
        a.points += 3
    else:
        h.points += 1
        a.points += 1


def ranking(table: dict[str, TableRow]) -> list[str]:
    return sorted(
        table,
        key=lambda team: (
            -table[team].points,
            -table[team].gd,
            -table[team].gf,
            team.casefold(),
        ),
    )


def elo_expected(home_rating: float, away_rating: float, home_advantage: float = HOME_ADVANTAGE) -> float:
    exponent = (away_rating - (home_rating + home_advantage)) / RATING_SCALE
    return 1.0 / (1.0 + 10.0 ** exponent)


def update_ratings(ratings: dict[str, float], fixture: Fixture) -> None:
    if fixture.home_goals is None or fixture.away_goals is None:
        return
    expected = elo_expected(ratings[fixture.home], ratings[fixture.away])
    actual = 1.0 if fixture.home_goals > fixture.away_goals else 0.0 if fixture.home_goals < fixture.away_goals else 0.5
    margin = abs(fixture.home_goals - fixture.away_goals)
    mult = 1.0 if margin <= 1 else 1.0 + min(0.45, 0.10 * (margin - 1))
    delta = K_FACTOR * mult * (actual - expected)
    ratings[fixture.home] += delta
    ratings[fixture.away] -= delta


def goal_means(home: str, away: str, ratings: dict[str, float], base_home: float, base_away: float) -> tuple[float, float]:
    diff = (ratings[home] + HOME_ADVANTAGE - ratings[away]) / RATING_SCALE
    diff = max(-1.5, min(1.5, diff))
    shift = max(-0.85, min(0.85, GOAL_LOG_COEFF * diff))
    return (
        max(0.15, min(4.50, base_home * math.exp(shift))),
        max(0.15, min(4.50, base_away * math.exp(-shift))),
    )


def poisson_cdf(mean: float, max_count: int = 12) -> list[float]:
    p = math.exp(-mean)
    total = p
    cdf = [total]
    for k in range(1, max_count + 1):
        p *= mean / k
        total += p
        cdf.append(min(1.0, total))
    cdf[-1] = 1.0
    return cdf


def draw_from_cdf(rng: random.Random, cdf: list[float]) -> int:
    value = rng.random()
    for idx, edge in enumerate(cdf):
        if value <= edge:
            return idx
    return len(cdf) - 1


class ScoreModel:
    def __init__(self, ratings: dict[str, float], base_home: float, base_away: float):
        self.ratings = ratings
        self.base_home = base_home
        self.base_away = base_away
        self.cache: dict[tuple[str, str], tuple[list[float], list[float]]] = {}

    def draw(self, home: str, away: str, rng: random.Random) -> tuple[int, int]:
        key = (home, away)
        cdfs = self.cache.get(key)
        if cdfs is None:
            hm, am = goal_means(home, away, self.ratings, self.base_home, self.base_away)
            cdfs = (poisson_cdf(hm), poisson_cdf(am))
            self.cache[key] = cdfs
        return draw_from_cdf(rng, cdfs[0]), draw_from_cdf(rng, cdfs[1])

    def neutral_win_probability(self, team_a: str, team_b: str) -> float:
        exponent = (self.ratings[team_b] - self.ratings[team_a]) / RATING_SCALE
        return 1.0 / (1.0 + 10.0 ** exponent)

    def draw_neutral(self, team_a: str, team_b: str, rng: random.Random) -> tuple[int, int]:
        # Final venue is neutral: use the mean scoring environment and no home advantage.
        base = max(0.15, (self.base_home + self.base_away) / 2.0)
        diff = (self.ratings[team_a] - self.ratings[team_b]) / RATING_SCALE
        diff = max(-1.5, min(1.5, diff))
        shift = max(-0.85, min(0.85, GOAL_LOG_COEFF * diff))
        a_cdf = poisson_cdf(max(0.15, min(4.50, base * math.exp(shift))))
        b_cdf = poisson_cdf(max(0.15, min(4.50, base * math.exp(-shift))))
        return draw_from_cdf(rng, a_cdf), draw_from_cdf(rng, b_cdf)


def two_leg_winner(team_a: str, team_b: str, model: ScoreModel, rng: random.Random) -> str:
    # One home leg each; away goals do not apply.
    a_home = model.draw(team_a, team_b, rng)
    b_home = model.draw(team_b, team_a, rng)
    a_goals = a_home[0] + b_home[1]
    b_goals = a_home[1] + b_home[0]
    if a_goals > b_goals:
        return team_a
    if b_goals > a_goals:
        return team_b
    return team_a if rng.random() < model.neutral_win_probability(team_a, team_b) else team_b


def final_winner(team_a: str, team_b: str, model: ScoreModel, rng: random.Random) -> str:
    # UEFA final is one match at a neutral venue.
    a, b = model.draw_neutral(team_a, team_b, rng)
    if a > b:
        return team_a
    if b > a:
        return team_b
    p = model.neutral_win_probability(team_a, team_b)
    return team_a if rng.random() < p else team_b


def random_two_ties(seed_pair: list[str], unseed_pair: list[str], rng: random.Random) -> list[tuple[str, str]]:
    left = seed_pair[:]
    right = unseed_pair[:]
    rng.shuffle(left)
    rng.shuffle(right)
    return [(left[0], right[0]), (left[1], right[1])]


def simulate_competition(
    competition: dict[str, Any],
    fixtures: list[Fixture],
    logos: dict[str, str],
    runs: int,
    api_league_id: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    alias_map, pot_by_team, ratings = participant_maps(competition)
    del alias_map
    teams = list(pot_by_team)
    expected_fixtures = int(competition["expectedLeaguePhaseFixtures"])
    if len(fixtures) != expected_fixtures:
        raise RuntimeError(
            f"{competition['name']}: expected {expected_fixtures} league-phase fixtures, got {len(fixtures)}"
        )
    if len(teams) != 36:
        raise RuntimeError(f"{competition['name']}: expected 36 configured teams, got {len(teams)}")

    completed = [
        f for f in fixtures
        if f.status.upper() in FINISHED and f.home_goals is not None and f.away_goals is not None
    ]
    future = [f for f in fixtures if f not in completed]

    base_table = {team: TableRow() for team in teams}
    for f in completed:
        apply_result(base_table, f.home, f.away, int(f.home_goals), int(f.away_goals))
        update_ratings(ratings, f)

    if len(completed) >= 20:
        base_home = sum(int(f.home_goals) for f in completed) / len(completed)
        base_away = sum(int(f.away_goals) for f in completed) / len(completed)
    else:
        base_home = DEFAULT_HOME_GOALS
        base_away = DEFAULT_AWAY_GOALS
    base_home = max(0.9, min(2.2, base_home))
    base_away = max(0.8, min(2.0, base_away))
    model = ScoreModel(ratings, base_home, base_away)

    current_order = ranking(base_table)
    current_pos = {team: idx + 1 for idx, team in enumerate(current_order)}

    counters = {
        team: {
            "top8": 0, "playoff": 0, "r16": 0, "qf": 0,
            "sf": 0, "final": 0, "winner": 0, "position_total": 0,
        }
        for team in teams
    }

    seed_material = f"{competition['competitionId']}|{competition['season'] if 'season' in competition else '2026'}|{runs}|{MODEL_VERSION}"
    seed = int(hashlib.sha256(seed_material.encode("utf-8")).hexdigest()[:16], 16)
    rng = random.Random(seed)

    for _ in range(runs):
        table = {
            team: TableRow(
                played=base_table[team].played,
                points=base_table[team].points,
                gf=base_table[team].gf,
                ga=base_table[team].ga,
            )
            for team in teams
        }
        for f in future:
            hg, ag = model.draw(f.home, f.away, rng)
            apply_result(table, f.home, f.away, hg, ag)

        order = ranking(table)
        for idx, team in enumerate(order):
            counters[team]["position_total"] += idx + 1
        for team in order[:8]:
            counters[team]["top8"] += 1
            counters[team]["r16"] += 1
        for team in order[8:24]:
            counters[team]["playoff"] += 1

        # UEFA paired-seeding playoff draw:
        # 9/10 v 23/24, 11/12 v 21/22, 13/14 v 19/20, 15/16 v 17/18.
        playoff_groups = [
            (order[8:10], order[22:24]),
            (order[10:12], order[20:22]),
            (order[12:14], order[18:20]),
            (order[14:16], order[16:18]),
        ]
        playoff_winners: list[list[str]] = []
        for seeded, unseeded in playoff_groups:
            group_winners = []
            for a, b in random_two_ties(list(seeded), list(unseeded), rng):
                winner = two_leg_winner(a, b, model, rng)
                group_winners.append(winner)
                counters[winner]["r16"] += 1
            playoff_winners.append(group_winners)

        # Round-of-16 paired seeding: 1/2 face winners from the 15-18 path,
        # 3/4 from 13-20, 5/6 from 11-22, 7/8 from 9-24.
        r16_seed_groups = [order[0:2], order[2:4], order[4:6], order[6:8]]
        r16_opponent_groups = [
            playoff_winners[3], playoff_winners[2],
            playoff_winners[1], playoff_winners[0],
        ]
        r16_group_winners: list[list[str]] = []
        for seeded, opponents in zip(r16_seed_groups, r16_opponent_groups):
            group_winners = []
            for a, b in random_two_ties(list(seeded), list(opponents), rng):
                winner = two_leg_winner(a, b, model, rng)
                group_winners.append(winner)
                counters[winner]["qf"] += 1
            r16_group_winners.append(group_winners)

        # Preserve the four seeded bracket paths. Cross the 1/2 path with 7/8
        # and the 3/4 path with 5/6, matching the fixed-path concept in Annex B.
        qf_pairs: list[tuple[str, str]] = []
        for left_idx, right_idx in ((0, 3), (1, 2)):
            left = r16_group_winners[left_idx][:]
            right = r16_group_winners[right_idx][:]
            rng.shuffle(left)
            rng.shuffle(right)
            qf_pairs.extend([(left[0], right[0]), (left[1], right[1])])

        sf_entrants: list[str] = []
        for a, b in qf_pairs:
            winner = two_leg_winner(a, b, model, rng)
            sf_entrants.append(winner)
            counters[winner]["sf"] += 1

        # The first two quarter-finals form one semi-final side, the other two the other side.
        finalists: list[str] = []
        for i, j in ((0, 1), (2, 3)):
            winner = two_leg_winner(sf_entrants[i], sf_entrants[j], model, rng)
            finalists.append(winner)
            counters[winner]["final"] += 1

        champion = final_winner(finalists[0], finalists[1], model, rng)
        counters[champion]["winner"] += 1

    rows = []
    for team in teams:
        row = base_table[team]
        count = counters[team]
        rows.append({
            "team_key": norm(team),
            "team_name": team,
            "team_logo": logos.get(team),
            "pot": pot_by_team[team],
            "strength_rating": ratings[team],
            "current_position": current_pos[team],
            "current_played": row.played,
            "current_points": row.points,
            "current_goal_difference": row.gd,
            "current_goals_for": row.gf,
            "expected_league_position": count["position_total"] / runs,
            "top8_probability": count["top8"] / runs,
            "playoff_probability": count["playoff"] / runs,
            "round16_probability": count["r16"] / runs,
            "quarterfinal_probability": count["qf"] / runs,
            "semifinal_probability": count["sf"] / runs,
            "final_probability": count["final"] / runs,
            "winner_probability": count["winner"] / runs,
        })

    rows.sort(key=lambda r: (-r["winner_probability"], r["expected_league_position"], r["team_name"]))
    meta = {
        "competition_id": competition["competitionId"],
        "league_code": competition["leagueCode"],
        "competition_name": competition["name"],
        "season": "2026-2027",
        "team_count": len(teams),
        "league_matches_per_team": int(competition["leagueMatchesPerTeam"]),
        "completed_league_matches": len(completed),
        "remaining_league_matches": len(future),
        "simulation_runs": runs,
        "model_version": MODEL_VERSION,
        "strength_model": STRENGTH_MODEL,
        "tie_break_model": TIE_BREAK_MODEL,
        "api_league_id": api_league_id,
        "base_home_goals": base_home,
        "base_away_goals": base_away,
    }
    return meta, rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("db", type=Path)
    parser.add_argument("--runs", type=int, default=10_000)
    args = parser.parse_args()
    runs = max(1_000, min(50_000, int(args.runs)))

    api_key = os.getenv("API_FOOTBALL_KEY", "").strip()
    if not api_key:
        raise SystemExit("API_FOOTBALL_KEY is required for UEFA simulation materialization")
    if not args.db.is_file():
        raise SystemExit(f"Prepared DB missing: {args.db}")

    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    season = int(config.get("season") or 2026)
    results: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []

    for competition in config.get("competitions") or []:
        league_id = find_league_id(api_key, competition, season)
        fixture_payload = api_get(api_key, "fixtures", {"league": league_id, "season": season})
        fixtures, logos = parse_fixtures(fixture_payload, competition)
        result = simulate_competition(competition, fixtures, logos, runs, league_id)
        results.append(result)

    generated_at_ms = int(time.time() * 1000)
    con = sqlite3.connect(args.db)
    try:
        con.executescript(
            """
            DROP TABLE IF EXISTS prepared_uefa_team_simulations;
            DROP TABLE IF EXISTS prepared_uefa_simulation_meta;

            CREATE TABLE prepared_uefa_simulation_meta (
                competition_id TEXT NOT NULL,
                league_code TEXT NOT NULL,
                competition_name TEXT NOT NULL,
                season TEXT NOT NULL,
                team_count INTEGER NOT NULL,
                league_matches_per_team INTEGER NOT NULL,
                completed_league_matches INTEGER NOT NULL,
                remaining_league_matches INTEGER NOT NULL,
                simulation_runs INTEGER NOT NULL,
                model_version TEXT NOT NULL,
                strength_model TEXT NOT NULL,
                tie_break_model TEXT NOT NULL,
                api_league_id INTEGER NOT NULL,
                base_home_goals REAL NOT NULL,
                base_away_goals REAL NOT NULL,
                generated_at_ms INTEGER NOT NULL,
                PRIMARY KEY (competition_id, season)
            );

            CREATE TABLE prepared_uefa_team_simulations (
                competition_id TEXT NOT NULL,
                season TEXT NOT NULL,
                team_key TEXT NOT NULL,
                team_name TEXT NOT NULL,
                team_logo TEXT,
                pot INTEGER NOT NULL,
                strength_rating REAL NOT NULL,
                current_position INTEGER NOT NULL,
                current_played INTEGER NOT NULL,
                current_points INTEGER NOT NULL,
                current_goal_difference INTEGER NOT NULL,
                current_goals_for INTEGER NOT NULL,
                expected_league_position REAL NOT NULL,
                top8_probability REAL NOT NULL,
                playoff_probability REAL NOT NULL,
                round16_probability REAL NOT NULL,
                quarterfinal_probability REAL NOT NULL,
                semifinal_probability REAL NOT NULL,
                final_probability REAL NOT NULL,
                winner_probability REAL NOT NULL,
                generated_at_ms INTEGER NOT NULL,
                PRIMARY KEY (competition_id, season, team_key)
            );
            CREATE INDEX idx_prepared_uefa_team_winner
              ON prepared_uefa_team_simulations(
                  competition_id, season, winner_probability DESC
              );
            """
        )

        team_rows = 0
        for meta, rows in results:
            con.execute(
                """
                INSERT INTO prepared_uefa_simulation_meta(
                    competition_id,league_code,competition_name,season,team_count,
                    league_matches_per_team,completed_league_matches,remaining_league_matches,
                    simulation_runs,model_version,strength_model,tie_break_model,
                    api_league_id,base_home_goals,base_away_goals,generated_at_ms
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    meta["competition_id"], meta["league_code"], meta["competition_name"],
                    meta["season"], meta["team_count"], meta["league_matches_per_team"],
                    meta["completed_league_matches"], meta["remaining_league_matches"],
                    meta["simulation_runs"], meta["model_version"], meta["strength_model"],
                    meta["tie_break_model"], meta["api_league_id"],
                    meta["base_home_goals"], meta["base_away_goals"], generated_at_ms,
                ),
            )
            con.executemany(
                """
                INSERT INTO prepared_uefa_team_simulations(
                    competition_id,season,team_key,team_name,team_logo,pot,strength_rating,
                    current_position,current_played,current_points,current_goal_difference,
                    current_goals_for,expected_league_position,top8_probability,
                    playoff_probability,round16_probability,quarterfinal_probability,
                    semifinal_probability,final_probability,winner_probability,generated_at_ms
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        meta["competition_id"], meta["season"], row["team_key"], row["team_name"],
                        row["team_logo"], row["pot"], row["strength_rating"],
                        row["current_position"], row["current_played"], row["current_points"],
                        row["current_goal_difference"], row["current_goals_for"],
                        row["expected_league_position"], row["top8_probability"],
                        row["playoff_probability"], row["round16_probability"],
                        row["quarterfinal_probability"], row["semifinal_probability"],
                        row["final_probability"], row["winner_probability"], generated_at_ms,
                    )
                    for row in rows
                ],
            )
            team_rows += len(rows)

        con.commit()
        quick = con.execute("PRAGMA quick_check").fetchone()
        if not quick or quick[0] != "ok":
            raise SystemExit(f"Prepared DB quick_check failed after UEFA simulation: {quick}")
        meta_count = int(con.execute("SELECT COUNT(*) FROM prepared_uefa_simulation_meta").fetchone()[0])
        if meta_count != len(results) or team_rows != len(results) * 36:
            raise SystemExit(
                f"UEFA simulation row contract failed: meta={meta_count} teams={team_rows}"
            )
    finally:
        con.close()

    print(
        "PREPARED_UEFA_SIMULATION_OK",
        f"model={MODEL_VERSION}",
        f"strength={STRENGTH_MODEL}",
        f"runs={runs}",
        f"competitions={len(results)}",
        f"teams={team_rows}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
