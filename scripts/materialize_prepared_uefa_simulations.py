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
from collections import Counter
from datetime import datetime
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

try:
    from . import uefa_club_strengths as club_strengths
except ImportError:
    import uefa_club_strengths as club_strengths

try:
    from .simulation_precision import MATCH_SIMULATION_RUNS, SEASON_SIMULATION_RUNS
except ImportError:
    from simulation_precision import MATCH_SIMULATION_RUNS, SEASON_SIMULATION_RUNS

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "uefa_simulation_2026_27.json"

MODEL_VERSION = "uefa-club-monte-carlo-v1"
MATCH_MODEL_VERSION = "uefa-match-monte-carlo-v1"
STRENGTH_MODEL = "uefa-seeding-elo-v1"
# Wire IDs stay compatible with installed APK1.0.2; the actual algorithm is separately versioned.
STRENGTH_ALGORITHM = club_strengths.ALGORITHM
TIE_BREAK_MODEL = "uefa-sporting-criteria-1-8-unobserved-ties-random-v2"
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
    away_gf: int = 0
    wins: int = 0
    away_wins: int = 0

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
    a.away_gf += ag
    a.ga += hg
    if hg > ag:
        h.points += 3
        h.wins += 1
    elif hg < ag:
        a.points += 3
        a.wins += 1
        a.away_wins += 1
    else:
        h.points += 1
        a.points += 1


def ranking(table: dict[str, TableRow], opponents=None, tie_order=None) -> list[str]:
    """UEFA Article18 criteria1-8. Unknown future fair-play/coefficient ties are marginalised.

    Fair-play totals include future cards/officials and are not available in this snapshot.
    Never silently substitute alphabetical order or use a coefficient ahead of unknown fair-play.
    Each simulation assigns an exchangeable random order only after all eight sporting criteria.
    """
    opponents = opponents or {t: () for t in table}
    tie_order = tie_order or {t: 0 for t in table}
    def key(team):
        r = table[team]
        rivals = [table[t] for t in opponents.get(team, ())]
        return (-r.points, -r.gd, -r.gf, -r.away_gf, -r.wins, -r.away_wins,
                -sum(t.points for t in rivals), -sum(t.gd for t in rivals),
                -sum(t.gf for t in rivals), tie_order[team])
    return sorted(table, key=key)


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
    def __init__(self, ratings: dict[str, float], base_home: float, base_away: float, profiles=None):
        self.ratings = ratings
        self.base_home = base_home
        self.base_away = base_away
        self.profiles = profiles or {}
        self.cache = {}

    def means(self, home, away, neutral=False):
        if neutral:
            base = (self.base_home + self.base_away)/2
            shift = max(-.85, min(.85, GOAL_LOG_COEFF * max(-1.5, min(1.5,
                (self.ratings[home]-self.ratings[away])/RATING_SCALE))))
            hm, am = base*math.exp(shift), base*math.exp(-shift)
        else:
            hm, am = goal_means(home, away, self.ratings, self.base_home, self.base_away)
        h = self.profiles.get(home, {})
        a = self.profiles.get(away, {})
        hm *= math.sqrt(h.get("attack", 1.0) * a.get("defense", 1.0))
        am *= math.sqrt(a.get("attack", 1.0) * h.get("defense", 1.0))
        return max(.15, min(4.5, hm)), max(.15, min(4.5, am))

    def draw(self, home, away, rng, neutral=False, extra_time=False):
        key = (home, away, neutral, extra_time)
        cdfs = self.cache.get(key)
        if cdfs is None:
            hm, am = self.means(home, away, neutral)
            divisor = 3 if extra_time else 1
            cdfs = (poisson_cdf(hm/divisor), poisson_cdf(am/divisor))
            self.cache[key] = cdfs
        return draw_from_cdf(rng, cdfs[0]), draw_from_cdf(rng, cdfs[1])

    def neutral_win_probability(self, a, b):
        # Compatibility helper; shoot-outs use an exchangeable coin without keeper-level evidence.
        return 1/(1+10**((self.ratings[b]-self.ratings[a])/RATING_SCALE))

    def draw_neutral(self, a, b, rng):
        return self.draw(a, b, rng, neutral=True)


def two_leg_winner(team_a, team_b, model, rng):
    # A is the seeded bracket path: first leg at B, return leg (and extra time) at A.
    first = model.draw(team_b, team_a, rng)
    second = model.draw(team_a, team_b, rng)
    a_goals, b_goals = first[1]+second[0], first[0]+second[1]
    if a_goals == b_goals:
        extra = model.draw(team_a, team_b, rng, extra_time=True)
        a_goals += extra[0]
        b_goals += extra[1]
    if a_goals != b_goals:
        return team_a if a_goals > b_goals else team_b
    return team_a if rng.random() < .5 else team_b


def final_winner(team_a, team_b, model, rng):
    a, b = model.draw(team_a, team_b, rng, neutral=True)
    if a == b:
        extra = model.draw(team_a, team_b, rng, neutral=True, extra_time=True)
        a += extra[0]
        b += extra[1]
    if a != b:
        return team_a if a > b else team_b
    return team_a if rng.random() < .5 else team_b


def quarterfinal_pairs(group_winners):
    # Index0/1 is a fixed half of Annex B. No redraw after the round of16.
    return [(group_winners[0][side], group_winners[3][side]) for side in (0, 1)] + [
            (group_winners[1][side], group_winners[2][side]) for side in (0, 1)]


def semifinal_pairs(quarter_winners):
    return [(quarter_winners[0], quarter_winners[2]),
            (quarter_winners[1], quarter_winners[3])]


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
    strength_inputs=None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    alias_map, pot_by_team, _ = participant_maps(competition)
    del alias_map
    if strength_inputs is None:
        config = json.loads(CONFIG_PATH.read_text())
        snapshot, mapped, _ = club_strengths.load_catalog(ROOT)
        profiles = club_strengths.xg_profiles(config, ROOT, goal_means)
        strength_inputs = (snapshot, mapped, profiles)
    snapshot, mapped, profiles = strength_inputs
    ratings = {t: float(mapped[t]["elo"]) for t in pot_by_team}
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
        # ClubElo already includes results through its published day. Never count them twice.
        if f.date > snapshot["throughDate"]:
            update_ratings(ratings, f)

    if len(completed) >= 20:
        base_home = sum(int(f.home_goals) for f in completed) / len(completed)
        base_away = sum(int(f.away_goals) for f in completed) / len(completed)
    else:
        base_home = DEFAULT_HOME_GOALS
        base_away = DEFAULT_AWAY_GOALS
    base_home = max(0.9, min(2.2, base_home))
    base_away = max(0.8, min(2.0, base_away))
    model = ScoreModel(ratings, base_home, base_away, profiles)
    opponents = {t: [] for t in teams}
    for f in fixtures:
        opponents[f.home].append(f.away)
        opponents[f.away].append(f.home)

    current_order = ranking(base_table, opponents)
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
    league_rng = random.Random(seed)
    tie_rng = random.Random(seed ^ 0x544945)
    rng = random.Random(seed ^ 0x4B4F)

    for _ in range(runs):
        table = {
            team: TableRow(
                played=base_table[team].played,
                points=base_table[team].points,
                gf=base_table[team].gf,
                ga=base_table[team].ga,
                away_gf=base_table[team].away_gf,
                wins=base_table[team].wins,
                away_wins=base_table[team].away_wins,
            )
            for team in teams
        }
        for f in future:
            hg, ag = model.draw(f.home, f.away, league_rng)
            apply_result(table, f.home, f.away, hg, ag)

        order = ranking(table, opponents, {t: tie_rng.random() for t in teams})
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
        for seeded, round_opponents in zip(r16_seed_groups, r16_opponent_groups):
            group_winners = []
            # The play-off winners already occupy fixed bracket halves. Only draw the seed pair.
            seeds = list(seeded)
            rng.shuffle(seeds)
            for a, b in zip(seeds, round_opponents):
                winner = two_leg_winner(a, b, model, rng)
                group_winners.append(winner)
                counters[winner]["qf"] += 1
            r16_group_winners.append(group_winners)

        qf_pairs = quarterfinal_pairs(r16_group_winners)

        sf_entrants: list[str] = []
        for a, b in qf_pairs:
            winner = two_leg_winner(a, b, model, rng)
            sf_entrants.append(winner)
            counters[winner]["sf"] += 1

        # Opposite halves keep league seeds1 and2 apart until the final.
        finalists: list[str] = []
        for a, b in semifinal_pairs(sf_entrants):
            winner = two_leg_winner(a, b, model, rng)
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


def materialize_match_simulations(
    con: sqlite3.Connection, config: dict[str, Any], runs: int = MATCH_SIMULATION_RUNS,
    now_ms: int | None = None,
) -> dict[str, int]:
    """Expose the existing UEFA ScoreModel per fixture; no new ratings/API calls.

    These are explorer-only rows. Legacy betting simulations, issued picks and
    domestic forecasts are untouched. Resolve identities through explicit aliases
    and reject fixtures that predate their published rating snapshot.
    """
    required = {"prepared_match_simulations", "prepared_fixture_matches",
                "prepared_uefa_simulation_meta", "prepared_uefa_team_simulations"}
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not required <= tables:
        raise ValueError("UEFA match simulation requires the complete prepared snapshot")
    runs = max(10_000, min(50_000, runs))
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    con.execute("DELETE FROM prepared_match_simulations WHERE simulation_model=?",
                (MATCH_MODEL_VERSION,))
    counts: dict[str, int] = {}
    for competition in config.get("competitions", []):
        cid = competition["competitionId"]
        meta_rows = con.execute(
            "SELECT season,base_home_goals,base_away_goals,completed_league_matches,"
            "generated_at_ms FROM prepared_uefa_simulation_meta "
            "WHERE competition_id=? AND model_version=? AND strength_model=?",
            (cid, MODEL_VERSION, STRENGTH_MODEL),
        ).fetchall()
        aliases, _, _ = participant_maps(competition)
        counts[cid] = 0
        for season, base_home, base_away, completed, source_ms in meta_rows:
            team_rows = con.execute(
                "SELECT team_name,strength_rating,current_played,generated_at_ms "
                "FROM prepared_uefa_team_simulations WHERE competition_id=? AND season=?",
                (cid, season),
            ).fetchall()
            ratings = {name: float(rating) for name, rating, _, _ in team_rows}
            played = {name: int(n) for name, _, n, _ in team_rows}
            if len(ratings) != 36 or not all(math.isfinite(r) for r in ratings.values()):
                raise ValueError(f"Incomplete UEFA strength snapshot: {cid}/{season}")
            if not all(math.isfinite(v) and v > 0 for v in (base_home, base_away)):
                raise ValueError(f"Invalid UEFA scoring environment: {cid}/{season}")
            rating_ms = max(int(source_ms), *(int(r[3]) for r in team_rows))
            profiles = {}
            if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='prepared_uefa_strength_evidence'").fetchone():
                profiles = {r[0]: {"attack": r[1], "defense": r[2]} for r in con.execute(
                    "SELECT team_name,xg_attack,xg_defense FROM prepared_uefa_strength_evidence "
                    "WHERE competition_id=? AND season=?", (cid, season))}
            model = ScoreModel(ratings, base_home, base_away, profiles)
            fixtures = con.execute(
                "SELECT fm.snapshot_version,fm.match_key,fm.kickoff,fm.home_team,fm.away_team "
                "FROM prepared_fixture_matches fm "
                "WHERE fm.competition_id=? AND fm.season=? AND NOT EXISTS ("
                "SELECT 1 FROM prepared_match_simulations ms "
                "WHERE ms.competition_id=fm.competition_id "
                "AND ms.snapshot_version=fm.snapshot_version AND ms.match_key=fm.match_key)",
                (cid, season),
            ).fetchall()
            for version, key, kickoff, raw_home, raw_away in fixtures:
                home, away = aliases.get(norm(raw_home)), aliases.get(norm(raw_away))
                if home not in ratings or away not in ratings or home == away:
                    continue  # Never guess a team's identity or seed an unknown team.
                try:
                    instant = datetime.fromisoformat(str(kickoff).replace("Z", "+00:00"))
                    if instant.tzinfo is None:
                        continue
                    kickoff_ms = int(instant.timestamp() * 1000)
                except (ValueError, TypeError):
                    continue
                if kickoff_ms < max(now_ms, rating_ms):
                    continue
                seed = f"{cid}|{version}|{key}|{MATCH_MODEL_VERSION}|{rating_ms}|{runs}"
                rng = random.Random(int(hashlib.sha256(seed.encode()).hexdigest()[:16], 16))
                scores = Counter(model.draw(home, away, rng) for _ in range(runs))
                home_wins = sum(n for (h, a), n in scores.items() if h > a)
                draws = sum(n for (h, a), n in scores.items() if h == a)
                home_mean, away_mean = model.means(home, away)
                distribution = json.dumps([
                    {"homeGoals": h, "awayGoals": a, "probability": n / runs}
                    for (h, a), n in sorted(scores.items())
                ], separators=(",", ":"))
                con.execute(
                    "INSERT INTO prepared_match_simulations("
                    "competition_id,snapshot_version,match_key,simulation_runs,simulation_model,"
                    "elo_model_version,home_elo_rating,away_elo_rating,elo_weight,"
                    "home_win_probability,draw_probability,away_win_probability,"
                    "expected_home_goals,expected_away_goals,score_distribution_json,"
                    "history_home_sample,history_away_sample,history_league_sample,generated_at_ms"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (cid, version, key, runs, MATCH_MODEL_VERSION, STRENGTH_MODEL,
                     ratings[home], ratings[away], 1.0, home_wins / runs, draws / runs,
                     (runs - home_wins - draws) / runs, home_mean, away_mean, distribution,
                     played[home], played[away], int(completed), now_ms),
                )
                counts[cid] += 1
    return counts



CACHED_UEFA_TABLES = {"champions_league": ("CL", 2), "europa_league": ("EL", 3),
                      "conference_league": ("UECL", 848)}


def cached_competition(competition: dict[str, Any], season: int, root: Path = ROOT):
    """One source for current table, completed results and remaining schedule; no API."""
    cid = competition["competitionId"]
    code, league_id = CACHED_UEFA_TABLES[cid]
    source = json.loads((root / "data/statmaker/league_tables" / (code + ".json")).read_text())
    if (source.get("contract") != "statmaker-league-tables-v1" or source.get("leagueCode") != code
            or source.get("country") != "UEFA" or int(source.get("season") or 0) != season
            or int(source.get("apiFootballLeagueId") or 0) != league_id):
        raise ValueError(f"{cid}: wrong cached competition/season")
    raw = source.get("fixtures") or []
    ids = [int(f.get("fixture_id") or 0) for f in raw]
    if len(set(ids)) != len(ids) or any(i <= 0 for i in ids):
        raise ValueError(f"{cid}: duplicate or missing fixture identity")
    response = []
    for f in raw:
        status = str(f.get("status") or "").upper()
        if status in FINISHED and any(type(f.get(k)) is not int or f[k] < 0
                                     for k in ("home_goals", "away_goals")):
            raise ValueError(f"{cid}: invalid finished goals")
        response.append({
            "fixture": {"id": f["fixture_id"], "date": f["date_utc"], "status": {"short": status}},
            "league": {"round": f.get("round", "")},
            "teams": {"home": {"name": f["home_team"], "logo": f.get("home_logo")},
                      "away": {"name": f["away_team"], "logo": f.get("away_logo")}},
            "goals": {"home": f.get("home_goals"), "away": f.get("away_goals")}})
    fixtures, logos = parse_fixtures({"response": response}, competition)
    aliases, pots, _ = participant_maps(competition)
    # Explicit provider standing spellings; participant identity is also checked against all results.
    standing_aliases = {
        "champions_league": {"Sabah FC": "Sabah"},
        "europa_league": {"Ferencvarosi TC": "Ferencváros"},
    }
    aliases.update({norm(a): t for a, t in standing_aliases.get(cid, {}).items()})
    counts = Counter(t for f in fixtures for t in (f.home, f.away))
    if (len(fixtures) != int(competition["expectedLeaguePhaseFixtures"])
            or set(counts) != set(pots)
            or any(n != int(competition["leagueMatchesPerTeam"]) for n in counts.values())):
        raise ValueError(f"{cid}: incomplete league-phase schedule")
    table = {t: TableRow() for t in pots}
    finished = {}
    for f in fixtures:
        if f.status in FINISHED:
            apply_result(table, f.home, f.away, f.home_goals, f.away_goals)
            finished[f.fixture_id] = (f.home_goals, f.away_goals)
    stats_path = root / "data/statmaker/uefa_current_stats.json"
    if stats_path.is_file():
        stats = json.loads(stats_path.read_text())
        if int(stats.get("season") or 0) == season:
            for comp in stats.get("competitions") or []:
                if comp.get("competitionId") == cid:
                    for f in comp.get("fixtures") or []:
                        if f.get("status") in FINISHED and finished.get(int(f["fixtureId"])) != (f["homeGoals"], f["awayGoals"]):
                            raise ValueError(f"{cid}: tables are behind published UEFA results")
    ranks = {}
    for row in source.get("standings") or []:
        team = resolve_team(str(row.get("team") or ""), aliases, list(pots))
        if team is None or team in ranks:
            raise ValueError(f"{cid}: unknown/duplicate standing: {row.get('team')}")
        t = table[team]
        if (t.played, t.points, t.gf, t.ga) != tuple(int(row.get(k, -1))
                    for k in ("played", "points", "goalsFor", "goalsAgainst")):
            raise ValueError(f"{cid}/{team}: standings disagree with completed fixtures")
        ranks[team] = int(row["rank"])
    if set(ranks) != set(pots) or set(ranks.values()) != set(range(1, 37)):
        raise ValueError(f"{cid}: incomplete standings")
    return fixtures, logos, ranks, league_id


def cached_source_fingerprint(config: dict[str, Any], root: Path = ROOT) -> str:
    snapshot, mapped, _ = club_strengths.load_catalog(root)
    profiles = club_strengths.xg_profiles(config, root, goal_means)
    inputs = {"seasonRuns": SEASON_SIMULATION_RUNS, "matchRuns": MATCH_SIMULATION_RUNS, "algorithm": STRENGTH_ALGORITHM, "sourceAsOf": snapshot["sourceAsOf"],
              "ratings": mapped, "xgProfiles": profiles}
    for comp in config["competitions"]:
        fixtures, _, ranks, league_id = cached_competition(comp, int(config["season"]), root)
        inputs[comp["competitionId"]] = {"season": config["season"], "league": league_id,
            "fixtures": [vars(f) for f in fixtures], "ranks": ranks}
    return hashlib.sha256(json.dumps(inputs, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def verify_cached_simulations(con: sqlite3.Connection, config: dict[str, Any], root: Path = ROOT):
    verified = {}
    for comp in config["competitions"]:
        fixtures, _, ranks, _ = cached_competition(comp, int(config["season"]), root)
        cid = comp["competitionId"]
        completed = sum(f.status in FINISHED for f in fixtures)
        meta = con.execute("SELECT completed_league_matches,remaining_league_matches,simulation_runs "
            "FROM prepared_uefa_simulation_meta WHERE competition_id=? AND season=?",
            (cid, "2026-2027")).fetchall()
        if len(meta) != 1 or meta[0][:2] != (completed, len(fixtures)-completed) or meta[0][2] < 10000:
            raise ValueError(f"{cid}: stale simulation match counts")
        table = {t: TableRow() for t in ranks}
        for f in fixtures:
            if f.status in FINISHED:
                apply_result(table, f.home, f.away, f.home_goals, f.away_goals)
        rows = con.execute("SELECT team_name,current_position,current_played,current_points,"
            "current_goal_difference,current_goals_for FROM prepared_uefa_team_simulations "
            "WHERE competition_id=? AND season=?", (cid, "2026-2027")).fetchall()
        if len(rows) != 36 or {r[0] for r in rows} != set(ranks):
            raise ValueError(f"{cid}: incomplete simulation teams")
        for team, position, played, points, gd, gf in rows:
            t = table[team]
            if (position, played, points, gd, gf) != (ranks[team], t.played, t.points, t.gd, t.gf):
                raise ValueError(f"{cid}/{team}: stale simulation current standing")
        verified[cid] = {"completed": completed, "remaining": len(fixtures)-completed,
                         "teams": 36, "runs": meta[0][2]}
    return verified


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("db", type=Path)
    parser.add_argument("--runs", type=int, default=SEASON_SIMULATION_RUNS)
    parser.add_argument("--match-runs", type=int, default=MATCH_SIMULATION_RUNS)
    parser.add_argument("--league-only", action="store_true", help="Refresh season tables without changing match simulations")
    parser.add_argument("--matches-only", action="store_true",
                        help="Reuse published UEFA ratings and fixtures without API calls")
    parser.add_argument("--cached-league-tables", action="store_true",
                        help="Refresh UEFA from published results/schedule; zero provider calls")
    args = parser.parse_args()
    runs = max(1_000, min(50_000, int(args.runs)))
    match_runs = max(10_000, min(50_000, int(args.match_runs)))
    if args.league_only and args.matches_only:
        parser.error("--league-only and --matches-only are mutually exclusive")

    if args.matches_only:
        if not args.db.is_file():
            raise SystemExit(f"Prepared DB missing: {args.db}")
        con = sqlite3.connect(args.db)
        try:
            counts = materialize_match_simulations(
                con, json.loads(CONFIG_PATH.read_text(encoding="utf-8")), match_runs)
            con.commit()
        finally:
            con.close()
        print("PREPARED_UEFA_MATCH_SIMULATION_OK", counts)
        return 0

    if not args.db.is_file():
        raise SystemExit(f"Prepared DB missing: {args.db}")
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    season = int(config.get("season") or 2026)
    results: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    snapshot, mapped, _ = club_strengths.load_catalog(ROOT)
    profiles = club_strengths.xg_profiles(config, ROOT, goal_means)
    api_key = "" if args.cached_league_tables else os.getenv("API_FOOTBALL_KEY", "").strip()
    if not args.cached_league_tables and not api_key:
        raise SystemExit("API_FOOTBALL_KEY is required for UEFA simulation materialization")
    for competition in config.get("competitions") or []:
        if args.cached_league_tables:
            fixtures, logos, ranks, league_id = cached_competition(competition, season)
        else:
            league_id = find_league_id(api_key, competition, season)
            fixture_payload = api_get(api_key, "fixtures", {"league": league_id, "season": season})
            fixtures, logos = parse_fixtures(fixture_payload, competition)
            ranks = None
        meta, rows = simulate_competition(competition, fixtures, logos, runs, league_id, (snapshot, mapped, profiles))
        if ranks is not None:
            for row in rows:
                row["current_position"] = ranks[row["team_name"]]
        results.append((meta, rows))

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

        con.execute("DROP TABLE IF EXISTS prepared_uefa_strength_evidence")
        con.execute("CREATE TABLE prepared_uefa_strength_evidence(competition_id TEXT,season TEXT,"
                    "team_name TEXT,algorithm TEXT,source_name TEXT,source_country TEXT,"
                    "source_elo REAL,source_as_of TEXT,xg_attack REAL,xg_defense REAL,xg_samples INTEGER,"
                    "PRIMARY KEY(competition_id,season,team_name))")
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
            con.executemany("INSERT INTO prepared_uefa_strength_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?)", [
                (meta["competition_id"], meta["season"], r["team_name"], STRENGTH_ALGORITHM,
                 mapped[r["team_name"]]["name"], mapped[r["team_name"]]["country"],
                 mapped[r["team_name"]]["elo"], snapshot["sourceAsOf"],
                 profiles[r["team_name"]]["attack"], profiles[r["team_name"]]["defense"],
                 profiles[r["team_name"]]["samples"]) for r in rows])
            team_rows += len(rows)

        match_counts = {} if args.league_only else materialize_match_simulations(con, config, match_runs)
        if args.cached_league_tables:
            print('UEFA_CACHED_STANDINGS_VERIFIED', verify_cached_simulations(con, config))
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
