#!/usr/bin/env python3
"""UAT Betting V2: direct fixture->historical matchup model->priced forecast.

No legacy prepared_simulations, certification, holdout approval, selection
quotas, implied-probability features or calls to external APIs. Fitted
Poisson match-up counts use the existing production_models.fit_count_model.
This job adds an independent table: existing Match/League Simulation and PROD
recommendations retain their own tables and behaviour.
"""
from __future__ import annotations

import argparse
import bisect
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import random
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from betting_v2.production_models import fit_count_model, rates

METRIC_COLUMNS = {
    "GOALS_FT": ("home_goals", "away_goals"),
    "GOALS_1H": ("hthg", "htag"),
    "GOALS_2H": ("home_goals", "away_goals"),
    "CORNERS": ("HC", "AC"),
    "SHOTS": ("HS", "AS"),
    "SOT": ("HST", "AST"),
    "YELLOW_CARDS": ("HY", "AY"),
    "FOULS": ("HF", "AF"),
}
MODEL = "betting-v2-matchup-poisson-fit-v1"
RUNS = 10_000


def name(value):
    return "".join(ch for ch in str(value or "").casefold() if ch.isalnum())


def kickoff(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def metric_values(row, metric):
    if metric == "GOALS_FT":
        raw = (row.get("home_goals"), row.get("away_goals"))
    elif metric == "GOALS_2H":
        try:
            raw = (int(row["home_goals"]) - int(row["hthg"]),
                   int(row["away_goals"]) - int(row["htag"]))
        except (ValueError, TypeError, KeyError):
            return None
    elif metric == "GOALS_1H":
        raw = (row.get("hthg"), row.get("htag"))
    else:
        keys = METRIC_COLUMNS[metric]
        stats = row.get("normalized_stats") or {}
        raw = (stats.get(keys[0]), stats.get(keys[1]))
    try:
        vals = tuple(float(v) for v in raw)
        if all(v >= 0 and math.isfinite(v) for v in vals):
            return vals
    except (TypeError, ValueError):
        pass
    return None


def load_actual_results():
    index = json.loads((ROOT / "data/statmaker/domestic_enriched/index.json").read_text())
    leagues = defaultdict(dict)
    for league in index.get("leagues", []):
        relative = league.get("output_path") or ""
        if not relative.startswith("data/statmaker/domestic_enriched/"):
            continue
        path = ROOT / relative
        if not path.is_file():
            continue
        data = json.loads(path.read_text())
        code = str(league.get("league_code") or "").upper()
        for match in data.get("matches", []):
            if match.get("status") != "FT":
                continue
            home = name(match.get("home_team"))
            away = name(match.get("away_team"))
            date = kickoff(match.get("date_utc"))
            if not home or not away or home == away or date <= 0:
                continue
            identifier = str(match.get("fixture_id") or "")
            key = identifier or f"{date}|{home}|{away}"
            leagues[code][key] = dict(match, _home=home, _away=away, _date=date)
    # UEFA clubs also have actual completed matches in the current season and
    # real team-specific support history. No synthetic competitor or results.
    current_path = ROOT / "data/statmaker/uefa_current_stats.json"
    support_path = ROOT / "data/statmaker/uefa_team_support_history.json"
    if current_path.is_file() and support_path.is_file():
        current = json.loads(current_path.read_text())
        support = json.loads(support_path.read_text())
        by_team = support.get("participants", [])
        for comp in current.get("competitions", []):
            code = str(comp.get("leagueCode") or "").upper()
            members = {name(v) for v in comp.get("participants", [])}
            if code not in {"CL", "EL", "CONF", "UECL"} or not members:
                continue
            def include(raw, when, home_key, away_key, hg_key, ag_key, stats_key):
                h, a = name(raw.get(home_key)), name(raw.get(away_key))
                if not h or not a or h == a:
                    return
                date = kickoff(raw.get(when))
                try:
                    hg, ag = int(raw[hg_key]), int(raw[ag_key])
                except (TypeError, KeyError, ValueError):
                    return
                if date <= 0 or hg < 0 or ag < 0:
                    return
                key = f"{date}|{h}|{a}"
                row = {
                    "status": "FT", "_date": date, "_home": h, "_away": a,
                    "home_goals": hg, "away_goals": ag,
                    "normalized_stats": raw.get(stats_key) or {},
                }
                leagues[code][key] = row
            for fixture in comp.get("fixtures", []):
                if fixture.get("status") == "FT":
                    include(fixture, "kickoff", "homeTeam", "awayTeam",
                            "homeGoals", "awayGoals", "normalizedStats")
            for participant in by_team:
                aliases = {name(v) for v in [
                    participant.get("providerTeam"), participant.get("canonicalTeam"),
                    *participant.get("aliases", [])
                ]}
                if not members.intersection(aliases):
                    continue
                for fixture in participant.get("matches", []):
                    include(fixture, "date", "homeTeam", "awayTeam",
                            "homeGoals", "awayGoals", "normalizedStats")
    return {k: sorted(v.values(), key=lambda x: x["_date"]) for k, v in leagues.items()}


def average(values, default):
    return sum(values[-8:]) / len(values[-8:]) if values else default


class MatchupLeague:
    """Chronological strength, venue, opponent-adjusted attack and form."""
    def __init__(self, matches, cutoff):
        self.history = defaultdict(lambda: defaultdict(list))
        self.elo = defaultdict(lambda: 1500.0)
        self.train = defaultdict(list)
        self.count = defaultdict(int)
        self.xg = defaultdict(list)
        self.matches = 0
        for row in matches:
            if row["_date"] >= cutoff:
                break
            h, a = row["_home"], row["_away"]
            targets = {m: metric_values(row, m) for m in METRIC_COLUMNS}
            features = {m: self.features(h, a, m) for m in targets}
            for metric, target in targets.items():
                if target is not None and features[metric] is not None:
                    self.train[metric].append({"features": features[metric], "target": target})
            hg = targets.get("GOALS_FT")
            if hg is not None:
                gap = self.elo[h] - self.elo[a] + 65
                expected = 1.0 / (1 + 10 ** (-gap / 400))
                result = 1.0 if hg[0] > hg[1] else 0.5 if hg[0] == hg[1] else 0.0
                change = 20 * (result - expected)
                self.elo[h] += change
                self.elo[a] -= change
            for metric, values in targets.items():
                if values is None:
                    continue
                self.history[h][metric].append((values[0], values[1], True))
                self.history[a][metric].append((values[1], values[0], False))
            # Optional observed xG is an explicit feature, not a requirement.
            stats = row.get("normalized_stats") or {}
            try:
                hx, ax = float(stats["HxG"]), float(stats["AxG"])
                if math.isfinite(hx) and math.isfinite(ax) and hx >= 0 and ax >= 0:
                    self.xg[h].append((hx, ax))
                    self.xg[a].append((ax, hx))
            except (TypeError, ValueError, KeyError):
                pass
            self.matches += 1
        self.models = {}
        for metric, rows in self.train.items():
            if rows:
                try:
                    self.models[metric] = fit_count_model(
                        rows, {"regularization": 0.7, "maximumFitIterations": 200}, []
                    )
                except (ValueError, FloatingPointError, ArithmeticError):
                    pass

    def features(self, home, away, metric):
        h = self.history[home][metric]
        a = self.history[away][metric]
        # No invented form for a team with no actual observations.
        if not h or not a:
            return None
        hf = average([x[0] for x in h], 1)
        ha = average([x[1] for x in h], 1)
        af = average([x[0] for x in a], 1)
        aa = average([x[1] for x in a], 1)
        h_home = [x for x in h if x[2]]
        a_away = [x for x in a if not x[2]]
        hh = average([x[0] for x in h_home], hf)
        ad = average([x[1] for x in a_away], aa)
        ag = average([x[0] for x in a_away], af)
        hd = average([x[1] for x in h_home], ha)
        goal_h = self.history[home]["GOALS_FT"]
        goal_a = self.history[away]["GOALS_FT"]
        home_goal_form = average([x[0] - x[1] for x in goal_h], 0)
        away_goal_form = average([x[0] - x[1] for x in goal_a], 0)
        hx = self.xg[home]
        ax = self.xg[away]
        # Missing provider xG never becomes a fabricated xG estimate:
        # the observed flags distinguish unavailable data from actual zero.
        return [
            (self.elo[home] - self.elo[away] + 65) / 400.0,
            hf, aa, af, ha, hh, ad, ag, hd,
            home_goal_form, away_goal_form,
            min(len(h), 12) / 12.0, min(len(a), 12) / 12.0,
            average([v[0] for v in hx], 0.0), average([v[1] for v in ax], 0.0),
            float(bool(hx)), float(bool(ax)),
        ]

    def predict(self, home, away, metric):
        model = self.models.get(metric)
        x = self.features(home, away, metric)
        if model is None or x is None:
            return None
        try:
            home_rate, away_rate = rates(model, x)
        except (OverflowError, ValueError, ArithmeticError):
            return None
        ceiling = {"GOALS_FT": 12, "GOALS_1H": 8, "GOALS_2H": 8, "CORNERS": 32,
                   "SHOTS": 65, "SOT": 28, "YELLOW_CARDS": 16, "FOULS": 55}[metric]
        if any(not math.isfinite(v) or v <= 0 or v > ceiling for v in (home_rate, away_rate)):
            return None
        return (home_rate, away_rate,
                len(self.history[home][metric]), len(self.history[away][metric]),
                len(self.train[metric]))


def poisson_cdf(rate):
    p = math.exp(-rate)
    cdf = [p]
    for i in range(1, 160):
        p *= rate / i
        cdf.append(min(1.0, cdf[-1] + p))
        if cdf[-1] > 1 - 1e-11 and i >= rate + 8 * math.sqrt(rate):
            break
    return cdf


def samples(home_rate, away_rate, seed):
    rng = random.Random(int(hashlib.sha256(seed.encode()).hexdigest()[:16], 16))
    h, a = poisson_cdf(home_rate), poisson_cdf(away_rate)
    return [(bisect.bisect_left(h, rng.random()), bisect.bisect_left(a, rng.random()))
            for _ in range(RUNS)]


def quote_market(row, home, away):
    home_aliases = {name(row[k]) for k in ("home_team", "canonical_home_team", "provider_home_team")}
    away_aliases = {name(row[k]) for k in ("away_team", "canonical_away_team", "provider_away_team")}
    raw = name(row["market"]).upper()
    # Map named bookmaker markets to a SINGLE actual count family.
    targets = {
        "1X2": ("GOALS_FT", "RESULT"),
        "FULLTIME1X2": ("GOALS_FT", "RESULT"),
        "MATCHRESULT": ("GOALS_FT", "RESULT"),
        "FULLTIMERESULT3WAY": ("GOALS_FT", "RESULT"),
        "MATCHRESULT3WAY": ("GOALS_FT", "RESULT"),
        "MATCHWINNER": ("GOALS_FT", "RESULT"),
        "FULLTIMERESULT": ("GOALS_FT", "RESULT"),
        "DOUBLECHANCE": ("GOALS_FT", "DC"),
        "DC": ("GOALS_FT", "DC"),
        "DRAWNOBET": ("GOALS_FT", "DNB"),
        "BTTS": ("GOALS_FT", "BTTS"),
        "MATCHGOALS": ("GOALS_FT", "TOTAL"),
        "TEAMGOALS": ("GOALS_FT", "TEAM"),
        "FIRSTHALFGOALS": ("GOALS_1H", "TOTAL"),
        "MATCHFIRSTHALFGOALS": ("GOALS_1H", "TOTAL"),
        "SECONDHALFGOALS": ("GOALS_2H", "TOTAL"),
        "MATCHSECONDHALFGOALS": ("GOALS_2H", "TOTAL"),
        "MATCHCORNERS": ("CORNERS", "TOTAL"),
        "TEAMCORNERS": ("CORNERS", "TEAM"),
        "MATCHSHOTS": ("SHOTS", "TOTAL"),
        "TEAMSHOTS": ("SHOTS", "TEAM"),
        "MATCHSHOTSONTARGET": ("SOT", "TOTAL"),
        "TEAMSHOTSONTARGET": ("SOT", "TEAM"),
        "MATCHYELLOWCARDS": ("YELLOW_CARDS", "TOTAL"),
        "TEAMYELLOWCARDS": ("YELLOW_CARDS", "TEAM"),
        "MATCHFOULS": ("FOULS", "TOTAL"),
        "TEAMFOULS": ("FOULS", "TEAM"),
    }
    # No Asian/handicap, no unknown settlement contract.
    found = targets.get(raw)
    if not found:
        return None
    metric, scope = found
    text = str(row["selection"] or "").strip().casefold()
    if scope in ("TOTAL", "TEAM"):
        side = "OVER" if text.startswith("over") else "UNDER" if text.startswith("under") else ""
        if not side or row["line"] is None:
            return None
        line = float(row["line"])
        if line < 0 or line * 2 != int(line * 2):
            return None
        team_side = ""
        if scope == "TEAM":
            team = name(row["team"])
            if not team or (team in home_aliases) == (team in away_aliases):
                return None
            team_side = "HOME" if team in home_aliases else "AWAY"
        return metric, scope, side, team_side, line
    side = {
        "1": "HOME", "home": "HOME", "x": "DRAW", "draw": "DRAW",
        "2": "AWAY", "away": "AWAY",
        "1x": "HOME_OR_DRAW", "x2": "AWAY_OR_DRAW",
        "home or draw": "HOME_OR_DRAW", "draw or away": "AWAY_OR_DRAW",
        "home or away": "HOME_OR_AWAY", "12": "HOME_OR_AWAY",
        "yes": "YES", "no": "NO"
    }.get(text, "")
    if not side:
        if name(text) in home_aliases and name(text) not in away_aliases:
            side = "HOME"
        elif name(text) in away_aliases and name(text) not in home_aliases:
            side = "AWAY"
    allowed = {
        "RESULT": {"HOME", "DRAW", "AWAY"},
        "DC": {"HOME_OR_DRAW", "AWAY_OR_DRAW", "HOME_OR_AWAY"},
        "DNB": {"HOME", "AWAY"},
        "BTTS": {"YES", "NO"},
    }[scope]
    return (metric, scope, side, "", None) if side in allowed else None


def evaluate(counts, spec):
    _, scope, side, team_side, line = spec
    wins = pushes = 0
    for h, a in counts:
        if scope == "RESULT":
            win = side == ("HOME" if h > a else "AWAY" if a > h else "DRAW")
            push = False
        elif scope == "DC":
            win = (side == "HOME_OR_DRAW" and h >= a or
                   side == "AWAY_OR_DRAW" and a >= h or
                   side == "HOME_OR_AWAY" and h != a)
            push = False
        elif scope == "DNB":
            push = h == a
            win = h > a if side == "HOME" else a > h
        elif scope == "BTTS":
            push = False
            win = (h > 0 and a > 0) == (side == "YES")
        else:
            value = h if team_side == "HOME" else a if team_side == "AWAY" else h + a
            push = value == line
            win = value > line if side == "OVER" else value < line
        wins += bool(win and not push)
        pushes += bool(push)
    return wins / len(counts), pushes / len(counts)


def run(db_path):
    history = load_actual_results()
    now = datetime.now(timezone.utc).timestamp()
    counts = defaultdict(int)
    with sqlite3.connect(db_path) as con:
        con.row_factory = sqlite3.Row
        con.executescript("""
            DROP TABLE IF EXISTS prepared_betting_v2_forecasts;
            CREATE TABLE prepared_betting_v2_forecasts (
                competition_id TEXT NOT NULL,
                snapshot_version TEXT NOT NULL,
                match_key TEXT NOT NULL,
                market_ordinal INTEGER NOT NULL,
                market TEXT NOT NULL, selection TEXT NOT NULL,
                team TEXT, line REAL, odd REAL NOT NULL,
                win_probability REAL NOT NULL, push_probability REAL NOT NULL,
                prediction_model TEXT NOT NULL, metric_key TEXT NOT NULL,
                actual_runs INTEGER NOT NULL,
                home_history_sample INTEGER NOT NULL,
                away_history_sample INTEGER NOT NULL,
                league_training_sample INTEGER NOT NULL,
                PRIMARY KEY (competition_id,snapshot_version,match_key,market_ordinal)
            );
            CREATE INDEX idx_prepared_betting_v2_scope
              ON prepared_betting_v2_forecasts(competition_id,snapshot_version,match_key);
        """)
        matches = con.execute("""
            SELECT f.competition_id,f.snapshot_version,f.match_key,
                   f.league_code,f.date,f.kickoff,f.home_team,f.away_team,
                   f.canonical_home_team,f.canonical_away_team,
                   f.provider_home_team,f.provider_away_team,
                   q.ordinal,q.market,q.selection,q.team,q.line,q.odd
            FROM prepared_fixture_matches f
            JOIN prepared_fixture_markets q ON q.competition_id=f.competition_id
              AND q.snapshot_version=f.snapshot_version AND q.match_key=f.match_key
            WHERE f.usable_for_stats=1
            ORDER BY f.competition_id,f.snapshot_version,f.match_key,q.ordinal
        """).fetchall()
        models = {}
        draws = {}
        pending = []
        for row in matches:
            time = kickoff(row["kickoff"]) or kickoff(row["date"])
            # Fixture dates can be date-only; Android applies the exact
            # local kickoff cutoff at query time. Include today's quotes.
            if time < now - 86400:
                continue
            league = str(row["league_code"] or "").upper()
            # All quoted fixtures are future relative to this publication.
            # Train once per league using only results already completed now.
            model_key = league
            if model_key not in models:
                models[model_key] = MatchupLeague(history.get(league, []), now)
            m = models[model_key]
            # Prefer an exact observed team alias in historical fixtures.
            # This is identity matching, never a fallback model prediction.
            def history_name(keys):
                choices = [name(row[k]) for k in keys]
                return next((v for v in choices if v in m.history),
                            next((v for v in choices if v), ""))
            h = history_name(("canonical_home_team", "home_team", "provider_home_team"))
            a = history_name(("canonical_away_team", "away_team", "provider_away_team"))
            spec = quote_market(row, h, a)
            if not spec:
                counts["unsupported"] += 1
                continue
            metric = spec[0]
            key = (row["competition_id"], row["snapshot_version"], row["match_key"], metric)
            prediction = m.predict(h, a, metric)
            if prediction is None:
                counts["no_actual_history_or_fitted_model"] += 1
                continue
            hr, ar, hs, ass, training = prediction
            if key not in draws:
                draws[key] = samples(hr, ar, "|".join(map(str, key)) + "|" + MODEL)
            win, push = evaluate(draws[key], spec)
            if not (math.isfinite(win) and math.isfinite(push) and win + push <= 1.000001):
                continue
            pending.append((
                row["competition_id"], row["snapshot_version"], row["match_key"], row["ordinal"],
                row["market"], row["selection"], row["team"], row["line"], row["odd"],
                win, push, MODEL, metric, RUNS, hs, ass, training
            ))
        con.executemany("""
            INSERT INTO prepared_betting_v2_forecasts VALUES
            (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, pending)
        con.commit()
        counts["fixtures_from_quotes"] = len({(r[0], r[1], r[2]) for r in pending})
        counts["forecasts"] = len(pending)
    return dict(counts)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("prepared_db", type=Path)
    args = parser.parse_args()
    print("BETTING_V2_DIRECT_MODEL", json.dumps(run(args.prepared_db), sort_keys=True))
