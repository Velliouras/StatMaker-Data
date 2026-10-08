#!/usr/bin/env python3
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import random
import sqlite3
import statistics
import time
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = ROOT / "data/statmaker/domestic_enriched/index.json"
DEFAULT_RUNS = 10_000
# Keep MODEL_VERSION stable: prepared_simulations is consumed by the existing Hybrid/Betting
# evidence path and must remain unchanged while Match Explorer is validated independently.
MODEL_VERSION = "monte-carlo-v1"

# UI-only single-match simulation contract. This uses the same canonical prepared_team_elo
# signal and blend curve as League Simulation, without changing prepared_simulations.
MATCH_MODEL_VERSION = "match-monte-carlo-v2-elo"
ELO_MODEL_VERSION = "team-elo-v1"
ELO_GOAL_LOG_COEFF = 0.55
ELO_WEIGHT_EARLY = 0.65
ELO_WEIGHT_FLOOR = 0.35
ELO_WEIGHT_DECAY_PER_MATCH = 0.03

FINISHED_STATUSES = {"FT", "AET", "PEN"}
METRICS = ("goals", "shots", "sot", "corners", "cards", "yellow_cards")
STAT_LABELS = {
    "shots": "Total Shots",
    "sot": "Shots on Goal",
    "corners": "Corner Kicks",
    "yellow": "Yellow Cards",
    "red": "Red Cards",
}


def norm(value: Any) -> str:
    raw = unicodedata.normalize("NFKD", str(value or ""))
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
    return "".join(ch.lower() for ch in raw if ch.isalnum())


def finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def stat_map(raw_side: dict[str, Any] | None) -> dict[str, float]:
    out: dict[str, float] = {}
    if not isinstance(raw_side, dict):
        return out
    for item in raw_side.get("statistics") or []:
        if not isinstance(item, dict):
            continue
        label = str(item.get("type") or "")
        value = finite(item.get("value"))
        if value is not None:
            out[label] = value
    return out


def raw_sides(fixture: dict[str, Any]) -> tuple[dict[str, float], dict[str, float]]:
    home = norm(fixture.get("home_team"))
    away = norm(fixture.get("away_team"))
    found: dict[str, dict[str, float]] = {}
    for side in fixture.get("raw_statistics") or []:
        if not isinstance(side, dict):
            continue
        team = norm((side.get("team") or {}).get("name"))
        if team:
            found[team] = stat_map(side)
    return found.get(home, {}), found.get(away, {})


def metric_values(fixture: dict[str, Any]) -> tuple[dict[str, float], dict[str, float]]:
    home_stats, away_stats = raw_sides(fixture)
    home_goals = finite(fixture.get("home_goals"))
    away_goals = finite(fixture.get("away_goals"))
    if home_goals is None:
        home_goals = finite((fixture.get("goals") or {}).get("home"))
    if away_goals is None:
        away_goals = finite((fixture.get("goals") or {}).get("away"))

    def side(stats: dict[str, float], goals: float | None) -> dict[str, float]:
        yellow = stats.get(STAT_LABELS["yellow"])
        red = stats.get(STAT_LABELS["red"])
        cards = None
        if yellow is not None or red is not None:
            cards = (yellow or 0.0) + (red or 0.0)
        values = {
            "goals": goals,
            "shots": stats.get(STAT_LABELS["shots"]),
            "sot": stats.get(STAT_LABELS["sot"]),
            "corners": stats.get(STAT_LABELS["corners"]),
            "cards": cards,
            "yellow_cards": yellow,
        }
        return {k: float(v) for k, v in values.items() if v is not None and v >= 0.0}

    return side(home_stats, home_goals), side(away_stats, away_goals)


class LeagueHistory:
    def __init__(self, fixtures: list[dict[str, Any]]) -> None:
        self.league_home: dict[str, list[float]] = defaultdict(list)
        self.league_away: dict[str, list[float]] = defaultdict(list)
        self.team_all_for: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
        self.team_all_against: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
        self.team_home_for: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
        self.team_home_against: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
        self.team_away_for: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
        self.team_away_against: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))

        dated = sorted(
            (f for f in fixtures if str(f.get("status") or "").upper() in FINISHED_STATUSES),
            key=lambda f: str(f.get("date") or ""),
        )
        for fixture in dated:
            home_team = norm(fixture.get("home_team"))
            away_team = norm(fixture.get("away_team"))
            if not home_team or not away_team:
                continue
            home_values, away_values = metric_values(fixture)
            for metric in METRICS:
                hv = home_values.get(metric)
                av = away_values.get(metric)
                if hv is not None:
                    self.league_home[metric].append(hv)
                    self.team_all_for[home_team][metric].append(hv)
                    self.team_home_for[home_team][metric].append(hv)
                    self.team_all_against[away_team][metric].append(hv)
                    self.team_away_against[away_team][metric].append(hv)
                if av is not None:
                    self.league_away[metric].append(av)
                    self.team_all_for[away_team][metric].append(av)
                    self.team_away_for[away_team][metric].append(av)
                    self.team_all_against[home_team][metric].append(av)
                    self.team_home_against[home_team][metric].append(av)

    @staticmethod
    def mean(values: list[float]) -> float | None:
        return statistics.fmean(values) if values else None

    @staticmethod
    def recent(values: list[float], n: int = 10) -> list[float]:
        return values[-n:] if len(values) > n else values

    @staticmethod
    def shrunk(values: list[float], baseline: float, strength: float = 6.0) -> float:
        sample = LeagueHistory.recent(values)
        if not sample:
            return baseline
        return (sum(sample) + baseline * strength) / (len(sample) + strength)

    def expected(self, home_team: str, away_team: str, metric: str) -> tuple[float, float, int, int, int] | None:
        home_baseline = self.mean(self.league_home.get(metric, []))
        away_baseline = self.mean(self.league_away.get(metric, []))
        if home_baseline is None or away_baseline is None:
            return None

        hf_side = self.team_home_for[home_team].get(metric, [])
        ha_side = self.team_home_against[home_team].get(metric, [])
        af_side = self.team_away_for[away_team].get(metric, [])
        aa_side = self.team_away_against[away_team].get(metric, [])

        hf = hf_side if len(hf_side) >= 3 else self.team_all_for[home_team].get(metric, [])
        ha = ha_side if len(ha_side) >= 3 else self.team_all_against[home_team].get(metric, [])
        af = af_side if len(af_side) >= 3 else self.team_all_for[away_team].get(metric, [])
        aa = aa_side if len(aa_side) >= 3 else self.team_all_against[away_team].get(metric, [])

        home_for = self.shrunk(hf, home_baseline)
        away_against = self.shrunk(aa, home_baseline)
        away_for = self.shrunk(af, away_baseline)
        home_against = self.shrunk(ha, away_baseline)

        expected_home = home_for * 0.50 + away_against * 0.35 + home_baseline * 0.15
        expected_away = away_for * 0.50 + home_against * 0.35 + away_baseline * 0.15

        bounds = {
            "goals": (0.05, 5.5),
            "shots": (1.0, 32.0),
            "sot": (0.2, 13.0),
            "corners": (0.2, 16.0),
            "cards": (0.0, 11.0),
            "yellow_cards": (0.0, 10.0),
        }
        lo, hi = bounds[metric]
        expected_home = min(hi, max(lo, expected_home))
        expected_away = min(hi, max(lo, expected_away))
        return (
            expected_home,
            expected_away,
            len(self.recent(hf)),
            len(self.recent(af)),
            len(self.league_home.get(metric, [])) + len(self.league_away.get(metric, [])),
        )

    def dispersion_size(self, metric: str) -> float | None:
        if metric == "goals":
            return None
        values = [*self.league_home.get(metric, []), *self.league_away.get(metric, [])]
        if len(values) < 20:
            return None
        mean = statistics.fmean(values)
        variance = statistics.pvariance(values)
        if mean <= 0.0 or variance <= mean * 1.05:
            return None
        size = (mean * mean) / max(variance - mean, 1e-9)
        return min(50.0, max(1.0, size))


def distribution_cdf(mean: float, size: float | None, max_count: int) -> list[float]:
    mean = max(0.0001, mean)
    pmf: list[float] = []
    if size is None:
        p = math.exp(-mean)
        pmf.append(p)
        for count in range(1, max_count + 1):
            p *= mean / count
            pmf.append(p)
    else:
        success = size / (size + mean)
        failure = 1.0 - success
        p = success ** size
        pmf.append(p)
        for count in range(1, max_count + 1):
            p *= ((count - 1 + size) / count) * failure
            pmf.append(p)

    total = sum(pmf)
    if total <= 0.0:
        return [1.0]
    cdf: list[float] = []
    running = 0.0
    for value in pmf:
        running += value / total
        cdf.append(min(1.0, running))
    cdf[-1] = 1.0
    return cdf


def simulate_counts(rng: random.Random, cdf: list[float], runs: int) -> list[int]:
    return [bisect.bisect_left(cdf, rng.random()) for _ in range(runs)]


def metric_for_submarket(submarket: str) -> str | None:
    key = submarket.upper()
    if any(token in key for token in ("FIRST_HALF", "SECOND_HALF", "HT_", "_1H_", "_2H_")):
        return None
    if "SOT" in key or "SHOTS_ON_TARGET" in key:
        return "sot"
    if "SHOT" in key:
        return "shots"
    if "CORNER" in key:
        return "corners"
    if "YELLOW_CARD" in key:
        return "yellow_cards"
    if "CARD" in key:
        if "RED_CARD" in key:
            return None
        return "cards"
    if key in {
        "FULL_TIME_MATCH_TOTAL",
        "MATCH_GOALS_TOTAL",
        "HOME_TEAM_TOTAL",
        "AWAY_TEAM_TOTAL",
        "TEAM_TOTAL",
    }:
        return "goals"
    if "GOAL" in key or key.startswith("RESULT_") or key == "BTTS":
        return "goals"
    return None


def choose_team_array(
    row: sqlite3.Row,
    match: dict[str, Any],
    home: list[int],
    away: list[int],
) -> list[int] | None:
    submarket = str(row["identity_sub_market_key"] or "").upper()
    team_side = str(row["identity_team_side"] or "").upper()
    if submarket.startswith("HOME_TEAM_") or team_side == "HOME":
        return home
    if submarket.startswith("AWAY_TEAM_") or team_side == "AWAY":
        return away

    selected_team = norm(row["selection_team"])
    if selected_team:
        if selected_team == norm(match.get("homeTeam")):
            return home
        if selected_team == norm(match.get("awayTeam")):
            return away

    if "TEAM_" in submarket:
        return None
    return [h + a for h, a in zip(home, away)]


def evaluate_selection(
    row: sqlite3.Row,
    match: dict[str, Any],
    home: list[int],
    away: list[int],
) -> tuple[float, float] | None:
    side = str(row["identity_selection_side"] or "").upper()
    submarket = str(row["identity_sub_market_key"] or "").upper()
    line = finite(row["identity_line"])
    if line is None:
        line = finite(row["selection_line"])
    runs = len(home)
    if runs <= 0:
        return None

    if submarket == "BTTS":
        yes = sum(1 for h, a in zip(home, away) if h > 0 and a > 0)
        p = yes / runs
        if side == "YES":
            return p, 0.0
        if side == "NO":
            return 1.0 - p, 0.0
        return None

    result_like = (
        submarket in {
            "RESULT_1X2", "RESULT_DOUBLE_CHANCE",
            "CORNER_RESULT_1X2", "SHOTS_RESULT_1X2", "SOT_RESULT_1X2"
        }
    )
    if result_like:
        home_wins = sum(1 for h, a in zip(home, away) if h > a)
        draws = sum(1 for h, a in zip(home, away) if h == a)
        away_wins = runs - home_wins - draws
        counts = {
            "HOME": home_wins,
            "DRAW": draws,
            "AWAY": away_wins,
            "HOME_OR_DRAW": home_wins + draws,
            "AWAY_OR_DRAW": away_wins + draws,
            "HOME_OR_AWAY": home_wins + away_wins,
        }
        if side not in counts:
            return None
        return counts[side] / runs, 0.0

    if side not in {"OVER", "UNDER"} or line is None:
        return None
    values = choose_team_array(row, match, home, away)
    if values is None:
        return None

    wins = 0
    pushes = 0
    for value in values:
        if side == "OVER":
            if value > line:
                wins += 1
            elif value == line:
                pushes += 1
        else:
            if value < line:
                wins += 1
            elif value == line:
                pushes += 1
    return wins / runs, pushes / runs


def choose_index_entry(index: dict[str, Any], league_code: str, season: str) -> dict[str, Any] | None:
    rows = [x for x in index.get("leagues", []) if str(x.get("league_code") or "") == league_code]
    if not rows:
        return None

    def valid(row: dict[str, Any]) -> bool:
        path = ROOT / str(row.get("cache_path") or "")
        return path.is_file() and int(row.get("completed_fixtures") or 0) > 0

    exact = [x for x in rows if str(x.get("app_season") or "") == season and valid(x)]
    if exact:
        return max(exact, key=lambda x: int(x.get("completed_fixtures") or 0))
    current = [x for x in rows if str(x.get("stats_role") or "") == "current_target" and valid(x)]
    if current:
        return max(current, key=lambda x: int(x.get("completed_fixtures") or 0))
    fallback = [x for x in rows if valid(x)]
    return max(fallback, key=lambda x: int(x.get("completed_fixtures") or 0)) if fallback else None


def load_history(entry: dict[str, Any], cache: dict[str, LeagueHistory]) -> LeagueHistory | None:
    path = ROOT / str(entry.get("cache_path") or "")
    key = str(path)
    if key in cache:
        return cache[key]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    history = LeagueHistory(payload.get("fixtures") or [])
    cache[key] = history
    return history


def load_match_elo(
    con: sqlite3.Connection,
    league_code: str,
    season: str,
    home_team: str,
    away_team: str,
) -> tuple[float, int, float, int, float, float] | None:
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
    if meta is None or str(meta["model_version"] or "") != ELO_MODEL_VERSION:
        return None

    teams = {
        str(row["team_key"]): row
        for row in con.execute(
            """
            SELECT team_key,elo_rating,current_matches
            FROM prepared_team_elo
            WHERE league_code=? AND season=? AND team_key IN (?,?)
            """,
            (league_code, season, home_team, away_team),
        )
    }
    home = teams.get(home_team)
    away = teams.get(away_team)
    if home is None or away is None:
        return None
    return (
        float(home["elo_rating"]),
        int(home["current_matches"]),
        float(away["elo_rating"]),
        int(away["current_matches"]),
        float(meta["rating_scale"]),
        float(meta["home_advantage"]),
    )


def match_elo_weight(home_matches: int, away_matches: int) -> float:
    sample = min(home_matches, away_matches, 10)
    return max(ELO_WEIGHT_FLOOR, ELO_WEIGHT_EARLY - ELO_WEIGHT_DECAY_PER_MATCH * sample)


def elo_blended_goal_means(
    stat_home_mean: float,
    stat_away_mean: float,
    league_home_mean: float,
    league_away_mean: float,
    home_elo: float,
    home_matches: int,
    away_elo: float,
    away_matches: int,
    rating_scale: float,
    home_advantage: float,
) -> tuple[float, float, float]:
    scale = max(1.0, rating_scale)
    rating_diff = (home_elo + home_advantage) - away_elo
    normalized_diff = max(-1.5, min(1.5, rating_diff / scale))
    log_shift = max(-0.85, min(0.85, ELO_GOAL_LOG_COEFF * normalized_diff))

    elo_home_mean = max(0.05, league_home_mean * math.exp(log_shift))
    elo_away_mean = max(0.05, league_away_mean * math.exp(-log_shift))
    weight = match_elo_weight(home_matches, away_matches)

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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("db", type=Path)
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    args = parser.parse_args()
    runs = max(1_000, min(50_000, int(args.runs)))

    if not args.db.is_file():
        raise SystemExit(f"Prepared DB missing: {args.db}")
    index = json.loads(INDEX_PATH.read_text(encoding="utf-8"))

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    try:
        quick = con.execute("PRAGMA quick_check").fetchone()
        if not quick or quick[0] != "ok":
            raise SystemExit(f"Prepared DB quick_check failed before simulation: {quick}")

        con.executescript(
            """
            DROP TABLE IF EXISTS prepared_match_explorer_simulations;
            DROP TABLE IF EXISTS prepared_match_simulations;
            DROP TABLE IF EXISTS prepared_simulations;
            CREATE TABLE prepared_simulations (
                competition_id TEXT NOT NULL,
                snapshot_version TEXT NOT NULL,
                selection_key TEXT NOT NULL,
                match_key TEXT NOT NULL,
                simulation_probability REAL NOT NULL,
                simulation_push_probability REAL NOT NULL DEFAULT 0,
                simulation_runs INTEGER NOT NULL,
                simulation_model TEXT NOT NULL,
                metric_key TEXT NOT NULL,
                expected_home_count REAL,
                expected_away_count REAL,
                history_home_sample INTEGER NOT NULL DEFAULT 0,
                history_away_sample INTEGER NOT NULL DEFAULT 0,
                history_league_sample INTEGER NOT NULL DEFAULT 0,
                generated_at_ms INTEGER NOT NULL,
                PRIMARY KEY (competition_id, snapshot_version, selection_key)
            );
            CREATE INDEX idx_prepared_simulations_match
              ON prepared_simulations(competition_id, snapshot_version, match_key);

            CREATE TABLE prepared_match_simulations (
                competition_id TEXT NOT NULL,
                snapshot_version TEXT NOT NULL,
                match_key TEXT NOT NULL,
                simulation_runs INTEGER NOT NULL,
                simulation_model TEXT NOT NULL,
                elo_model_version TEXT NOT NULL,
                home_elo_rating REAL NOT NULL,
                away_elo_rating REAL NOT NULL,
                elo_weight REAL NOT NULL,
                home_win_probability REAL NOT NULL,
                draw_probability REAL NOT NULL,
                away_win_probability REAL NOT NULL,
                expected_home_goals REAL NOT NULL,
                expected_away_goals REAL NOT NULL,
                score_distribution_json TEXT NOT NULL DEFAULT '[]',
                history_home_sample INTEGER NOT NULL DEFAULT 0,
                history_away_sample INTEGER NOT NULL DEFAULT 0,
                history_league_sample INTEGER NOT NULL DEFAULT 0,
                generated_at_ms INTEGER NOT NULL,
                PRIMARY KEY (competition_id, snapshot_version, match_key)
            );
            CREATE INDEX idx_prepared_match_simulations_scope
              ON prepared_match_simulations(competition_id, snapshot_version, match_key);

            CREATE TABLE prepared_match_explorer_simulations (
                competition_id TEXT NOT NULL,
                snapshot_version TEXT NOT NULL,
                selection_key TEXT NOT NULL,
                match_key TEXT NOT NULL,
                simulation_probability REAL NOT NULL,
                simulation_push_probability REAL NOT NULL DEFAULT 0,
                simulation_runs INTEGER NOT NULL,
                simulation_model TEXT NOT NULL,
                metric_key TEXT NOT NULL,
                expected_home_goals REAL NOT NULL,
                expected_away_goals REAL NOT NULL,
                elo_model_version TEXT NOT NULL,
                home_elo_rating REAL NOT NULL,
                away_elo_rating REAL NOT NULL,
                elo_weight REAL NOT NULL,
                generated_at_ms INTEGER NOT NULL,
                PRIMARY KEY (competition_id, snapshot_version, selection_key)
            );
            CREATE INDEX idx_prepared_match_explorer_scope
              ON prepared_match_explorer_simulations(
                  competition_id, snapshot_version, match_key, metric_key
              );
            """
        )

        matches: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in con.execute(
            "SELECT competition_id,snapshot_version,match_key,payload FROM prepared_matches"
        ):
            try:
                payload = json.loads(row["payload"])
            except Exception:
                continue
            matches[(row["competition_id"], row["snapshot_version"], row["match_key"])] = payload

        # Match Simulation must not depend on bookmaker coverage. The fixture index now
        # contains schedule-only upcoming domestic fixtures sourced from the existing
        # API-Football season request. Supplement prepared_matches with those rows so
        # they receive an Elo-backed match summary even when prepared_selections is empty.
        fixture_tables = {
            str(row[0])
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "prepared_fixture_matches" in fixture_tables:
            for row in con.execute(
                """
                SELECT competition_id,snapshot_version,match_key,
                       id,date,kickoff,league_code,country,competition,season,
                       home_team,away_team,home_team_logo,away_team_logo
                FROM prepared_fixture_matches
                WHERE usable_for_stats=1
                """
            ):
                key = (
                    row["competition_id"],
                    row["snapshot_version"],
                    row["match_key"],
                )
                matches.setdefault(
                    key,
                    {
                        "id": row["id"],
                        "date": row["date"],
                        "kickoff": row["kickoff"],
                        "leagueCode": row["league_code"],
                        "country": row["country"],
                        "competition": row["competition"],
                        "season": row["season"],
                        "homeTeam": row["home_team"],
                        "awayTeam": row["away_team"],
                        "homeTeamLogo": row["home_team_logo"],
                        "awayTeamLogo": row["away_team_logo"],
                    },
                )

        selection_rows = list(
            con.execute(
                """
                SELECT competition_id,snapshot_version,selection_key,match_key,
                       selection_team,selection_line,selection_odd,
                       bm_market_probability,
                       identity_sub_market_key,identity_team_side,identity_line,
                       identity_selection_side
                FROM prepared_selections
                WHERE selection_odd > 1.01
                  AND identity_sub_market_key IS NOT NULL
                  AND identity_selection_side IS NOT NULL
                """
            )
        )
        by_match: dict[tuple[str, str, str], list[sqlite3.Row]] = defaultdict(list)
        for row in selection_rows:
            by_match[(row["competition_id"], row["snapshot_version"], row["match_key"])].append(row)

        history_cache: dict[str, LeagueHistory] = {}
        generated_at_ms = int(time.time() * 1000)
        inserted = 0
        match_summaries = 0
        match_explorer_rows = 0
        simulated_matches = 0
        unsupported = 0

        for match_key, match in matches.items():
            rows = by_match.get(match_key, [])
            league_code = str(match.get("leagueCode") or "")
            season = str(match.get("season") or "")
            entry = choose_index_entry(index, league_code, season)
            if not entry:
                continue
            history = load_history(entry, history_cache)
            if history is None:
                continue

            home_team = norm(match.get("homeTeam"))
            away_team = norm(match.get("awayTeam"))
            if not home_team or not away_team:
                continue

            needed_metrics = {
                metric
                for row in rows
                if (metric := metric_for_submarket(str(row["identity_sub_market_key"] or ""))) is not None
            }
            # Match-level Simulation Explorer always needs goals so 1/X/2 is available
            # independently of whether a specific 1X2 betting row exists.
            needed_metrics.add("goals")

            seed_material = "|".join(match_key) + f"|{runs}|{MODEL_VERSION}"
            simulated: dict[str, tuple[list[int], list[int], tuple[float, float, int, int, int], str]] = {}

            for metric in sorted(needed_metrics):
                metric_seed = seed_material + "|" + metric
                rng = random.Random(
                    int(hashlib.sha256(metric_seed.encode("utf-8")).hexdigest()[:16], 16)
                )
                expected = history.expected(home_team, away_team, metric)
                if expected is None:
                    continue
                home_mean, away_mean, home_sample, away_sample, league_sample = expected
                # Simulation is an active engine input, so do not materialize league-only guesses.
                # Both teams need a minimum recent sample and the league distribution must be mature.
                if home_sample < 3 or away_sample < 3 or league_sample < 20:
                    continue
                size = history.dispersion_size(metric)
                max_count = {
                    "goals": 14,
                    "shots": 60,
                    "sot": 30,
                    "corners": 35,
                    "cards": 25,
                    "yellow_cards": 25,
                }[metric]
                home_cdf = distribution_cdf(home_mean, size, max_count)
                away_cdf = distribution_cdf(away_mean, size, max_count)
                home_draws = simulate_counts(rng, home_cdf, runs)
                away_draws = simulate_counts(rng, away_cdf, runs)
                model_kind = "POISSON" if size is None else f"NEGATIVE_BINOMIAL(k={size:.2f})"
                simulated[metric] = (
                    home_draws,
                    away_draws,
                    (home_mean, away_mean, home_sample, away_sample, league_sample),
                    model_kind,
                )

            if not simulated:
                continue
            simulated_matches += 1

            if "goals" in simulated:
                # Match Explorer gets an Elo-backed goal model in its own tables. The legacy
                # prepared_simulations rows below are intentionally left untouched so the
                # existing Hybrid/Betting engine receives exactly the same evidence as before.
                _, _, goal_expected, _ = simulated["goals"]
                stat_home_mean, stat_away_mean, home_sample, away_sample, league_sample = goal_expected
                elo = load_match_elo(
                    con, league_code, season, home_team, away_team
                )
                league_home_mean = history.mean(history.league_home.get("goals", []))
                league_away_mean = history.mean(history.league_away.get("goals", []))
                if elo is not None and league_home_mean is not None and league_away_mean is not None:
                    (
                        home_elo, home_elo_matches,
                        away_elo, away_elo_matches,
                        rating_scale, home_advantage,
                    ) = elo
                    home_mean, away_mean, elo_weight = elo_blended_goal_means(
                        stat_home_mean=stat_home_mean,
                        stat_away_mean=stat_away_mean,
                        league_home_mean=league_home_mean,
                        league_away_mean=league_away_mean,
                        home_elo=home_elo,
                        home_matches=home_elo_matches,
                        away_elo=away_elo,
                        away_matches=away_elo_matches,
                        rating_scale=rating_scale,
                        home_advantage=home_advantage,
                    )
                    elo_seed = seed_material + "|" + MATCH_MODEL_VERSION + "|goals"
                    elo_rng = random.Random(
                        int(hashlib.sha256(elo_seed.encode("utf-8")).hexdigest()[:16], 16)
                    )
                    elo_home_draws = simulate_counts(
                        elo_rng, distribution_cdf(home_mean, None, 14), runs
                    )
                    elo_away_draws = simulate_counts(
                        elo_rng, distribution_cdf(away_mean, None, 14), runs
                    )
                    home_wins = sum(
                        1 for h, a in zip(elo_home_draws, elo_away_draws) if h > a
                    )
                    draws = sum(
                        1 for h, a in zip(elo_home_draws, elo_away_draws) if h == a
                    )
                    away_wins = runs - home_wins - draws
                    score_counts: dict[tuple[int, int], int] = defaultdict(int)
                    for home_goals, away_goals in zip(elo_home_draws, elo_away_draws):
                        score_counts[(home_goals, away_goals)] += 1
                    score_distribution_json = json.dumps(
                        [
                            {
                                "homeGoals": home_goals,
                                "awayGoals": away_goals,
                                "probability": count / runs,
                            }
                            for (home_goals, away_goals), count in sorted(score_counts.items())
                        ],
                        separators=(",", ":"),
                    )
                    con.execute(
                        """
                        INSERT OR REPLACE INTO prepared_match_simulations(
                            competition_id,snapshot_version,match_key,
                            simulation_runs,simulation_model,
                            elo_model_version,home_elo_rating,away_elo_rating,elo_weight,
                            home_win_probability,draw_probability,away_win_probability,
                            expected_home_goals,expected_away_goals,score_distribution_json,
                            history_home_sample,history_away_sample,history_league_sample,
                            generated_at_ms
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            match_key[0], match_key[1], match_key[2],
                            runs, MATCH_MODEL_VERSION,
                            ELO_MODEL_VERSION, home_elo, away_elo, elo_weight,
                            home_wins / runs, draws / runs, away_wins / runs,
                            home_mean, away_mean, score_distribution_json,
                            home_sample, away_sample, league_sample,
                            generated_at_ms,
                        ),
                    )
                    match_summaries += 1

                    for row in rows:
                        if metric_for_submarket(
                            str(row["identity_sub_market_key"] or "")
                        ) != "goals":
                            continue
                        explorer_result = evaluate_selection(
                            row, match, elo_home_draws, elo_away_draws
                        )
                        if explorer_result is None:
                            continue
                        explorer_probability, explorer_push = explorer_result
                        con.execute(
                            """
                            INSERT OR REPLACE INTO prepared_match_explorer_simulations(
                                competition_id,snapshot_version,selection_key,match_key,
                                simulation_probability,simulation_push_probability,
                                simulation_runs,simulation_model,metric_key,
                                expected_home_goals,expected_away_goals,
                                elo_model_version,home_elo_rating,away_elo_rating,elo_weight,
                                generated_at_ms
                            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            """,
                            (
                                row["competition_id"], row["snapshot_version"],
                                row["selection_key"], row["match_key"],
                                explorer_probability, explorer_push,
                                runs, MATCH_MODEL_VERSION, "goals",
                                home_mean, away_mean,
                                ELO_MODEL_VERSION, home_elo, away_elo, elo_weight,
                                generated_at_ms,
                            ),
                        )
                        match_explorer_rows += 1

            for row in rows:
                metric = metric_for_submarket(str(row["identity_sub_market_key"] or ""))
                if metric is None or metric not in simulated:
                    unsupported += 1
                    continue
                home_draws, away_draws, expected, model_kind = simulated[metric]
                result = evaluate_selection(row, match, home_draws, away_draws)
                if result is None:
                    unsupported += 1
                    continue
                probability, push_probability = result
                home_mean, away_mean, home_sample, away_sample, league_sample = expected
                con.execute(
                    """
                    INSERT OR REPLACE INTO prepared_simulations(
                        competition_id,snapshot_version,selection_key,match_key,
                        simulation_probability,simulation_push_probability,simulation_runs,
                        simulation_model,metric_key,expected_home_count,expected_away_count,
                        history_home_sample,history_away_sample,history_league_sample,generated_at_ms
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        row["competition_id"], row["snapshot_version"], row["selection_key"], row["match_key"],
                        probability, push_probability, runs,
                        f"{MODEL_VERSION}:{model_kind}", metric, home_mean, away_mean,
                        home_sample, away_sample, league_sample, generated_at_ms,
                    ),
                )
                inserted += 1

        con.commit()
        quick = con.execute("PRAGMA quick_check").fetchone()
        if not quick or quick[0] != "ok":
            raise SystemExit(f"Prepared DB quick_check failed after simulation: {quick}")
        total = len(selection_rows)
        coverage = inserted / total if total else 0.0
        print(
            "PREPARED_SIMULATION_OK",
            f"runs={runs}",
            f"matches={simulated_matches}",
            f"match_summaries={match_summaries}",
            f"match_model={MATCH_MODEL_VERSION}",
            f"match_explorer_rows={match_explorer_rows}",
            f"rows={inserted}",
            f"eligible_rows={total}",
            f"coverage={coverage:.4f}",
            f"unsupported={unsupported}",
            f"history_files={len(history_cache)}",
        )
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
