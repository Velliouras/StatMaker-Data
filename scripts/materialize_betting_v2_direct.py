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
import re
import sqlite3
import sys
import unicodedata

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
    "CARDS": ("HY", "AY"),
    "FOULS": ("HF", "AF"),
}
MODEL = "betting-v2-matchup-poisson-fit-v1"
RUNS = 10_000
# Exact fixture/provider aliases used solely for cross-competition identity.
UEFA_ALIASES = {}


def name(value):
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    return "".join(ch for ch in text if ch.isalnum())


def league_code(value):
    code = str(value or "").upper()
    return "UECL" if code == "CONF" else code


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
    elif metric == "CARDS":
        stats = row.get("normalized_stats") or {}
        try:
            raw = (float(stats["HY"]) + float(stats["HR"]),
                   float(stats["AY"]) + float(stats["AR"]))
        except (TypeError, KeyError, ValueError):
            return None
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
    UEFA_ALIASES.clear()
    for league in index.get("leagues", []):
        relative = league.get("output_path") or ""
        if not relative.startswith("data/statmaker/domestic_enriched/"):
            continue
        path = ROOT / relative
        if not path.is_file():
            continue
        data = json.loads(path.read_text())
        code = league_code(league.get("league_code"))
        for match in data.get("matches", []):
            if match.get("status") != "FT":
                continue
            home = name(match.get("home_team"))
            away = name(match.get("away_team"))
            date = kickoff(match.get("date_utc"))
            if not home or not away or home == away or date <= 0:
                continue
            key = str(match.get("fixture_id") or f"{date}|{home}|{away}")
            leagues[code][key] = dict(match, _home=home, _away=away, _date=date)

    current_path = ROOT / "data/statmaker/uefa_current_stats.json"
    if not current_path.is_file():
        return {k: sorted(v.values(), key=lambda x: x["_date"]) for k, v in leagues.items()}
    current = json.loads(current_path.read_text())
    # The main support file contains observed shots/cards/corners; the separate
    # team-specific file supplies real games for teams outside domestic coverage.
    participants = []
    for filename in ("uefa_support_history.json", "uefa_team_support_history.json"):
        path = ROOT / "data/statmaker" / filename
        if path.is_file():
            participants.extend(json.loads(path.read_text()).get("participants", []))
    groups = []
    domestic_alias_path = ROOT / "mappings/domestic_team_aliases.json"
    if domestic_alias_path.is_file():
        for teams in json.loads(domestic_alias_path.read_text()).get("aliases", {}).values():
            for canonical, synonyms in teams.items():
                groups.append((name(canonical), {name(v) for v in [canonical, *synonyms]}))
    for participant in participants:
        canonical = name(participant.get("canonicalTeam") or participant.get("providerTeam"))
        synonyms = {name(v) for v in [
            participant.get("canonicalTeam"), participant.get("providerTeam"),
            *participant.get("aliases", [])
        ] if v}
        groups.append((canonical, synonyms))

    configs = []
    for filename in ("uefa_club_competitions.json", "uefa_simulation_2026_27.json"):
        path = ROOT / "config" / filename
        if path.is_file():
            configs.extend(json.loads(path.read_text()).get("competitions", []))
    exact_path = ROOT / "mappings/uefa_betting_team_aliases.json"
    exact = json.loads(exact_path.read_text()).get("aliases", {}) if exact_path.is_file() else {}
    stats_fields = {
        "HS": "homeShots", "AS": "awayShots",
        "HST": "homeShotsOnTarget", "AST": "awayShotsOnTarget",
        "HC": "homeCorners", "AC": "awayCorners",
        "HY": "homeYellowCards", "AY": "awayYellowCards",
        "HR": "homeRedCards", "AR": "awayRedCards",
        "HF": "homeFouls", "AF": "awayFouls",
        "HxG": "homeXg", "AxG": "awayXg",
    }
    domestic = dict(leagues)
    for comp in current.get("competitions", []):
        code = league_code(comp.get("leagueCode"))
        members = {name(v) for v in comp.get("participants", []) if name(v)}
        if code not in ("CL", "EL", "UECL") or not members:
            continue
        aliases = {v: v for v in members}
        ambiguous = set()

        def add_alias(key, target):
            if not key or target not in members or key in ambiguous:
                return
            previous = aliases.get(key)
            if previous is not None and previous != target:
                if key not in members:
                    aliases.pop(key, None)
                    ambiguous.add(key)
                return
            aliases[key] = target

        for config in configs:
            if league_code(config.get("leagueCode")) == code:
                for synonym, canonical in config.get("aliases", {}).items():
                    add_alias(name(synonym), name(canonical))
        for synonym, canonical in exact.get(code, {}).items():
            add_alias(name(synonym), name(canonical))
        for fixture in comp.get("fixtures", []):
            for side in ("Home", "Away"):
                canonical = name(fixture.get("homeTeam" if side == "Home" else "awayTeam"))
                provider = name(fixture.get("providerHomeTeam" if side == "Home" else "providerAwayTeam"))
                add_alias(provider, canonical)
        # Expand only explicit aliases belonging to one identified participant.
        # Short aliases such as Inter/LAR must not link an unrelated club.
        for _ in range(3):
            for canonical, synonyms in groups:
                targets = {aliases[v] for v in synonyms if v in aliases and
                           (len(v) >= 4 or v == canonical and canonical in members)}
                if len(targets) == 1:
                    target = next(iter(targets))
                    for synonym in synonyms:
                        if len(synonym) >= 4 or synonym == canonical and canonical in members:
                            add_alias(synonym, target)
        UEFA_ALIASES[code] = aliases

        def canonical_team(value, namespace):
            key = name(value)
            return aliases.get(key, namespace + ":" + key)

        unified = {}

        def include_row(row):
            # One physical result, including when both participant support
            # lists contain it. Prefer the complete domestic provider record.
            key = (datetime.fromtimestamp(row["_date"], timezone.utc).date().isoformat(),
                   row["_home"], row["_away"])
            unified.setdefault(key, row)

        for domestic_code, records in domestic.items():
            if domestic_code in ("CL", "EL", "UECL"):
                continue
            if not any(aliases.get(row["_home"]) in members or
                       aliases.get(row["_away"]) in members for row in records.values()):
                continue
            for row in records.values():
                item = dict(row)
                item["_home"] = canonical_team(row["_home"], domestic_code)
                item["_away"] = canonical_team(row["_away"], domestic_code)
                include_row(item)

        def include_actual(raw, when):
            home, away = raw.get("homeTeam"), raw.get("awayTeam")
            date = kickoff(raw.get(when))
            try:
                hg, ag = int(raw["homeGoals"]), int(raw["awayGoals"])
            except (TypeError, ValueError, KeyError):
                return
            if not name(home) or not name(away) or name(home) == name(away) or date <= 0 or hg < 0 or ag < 0:
                return
            stats = dict(raw.get("normalizedStats") or {})
            for key, field in stats_fields.items():
                if key not in stats and raw.get(field) is not None:
                    stats[key] = raw[field]
            item = dict(
                status="FT", _date=date,
                _home=canonical_team(home, "support"), _away=canonical_team(away, "support"),
                home_goals=hg, away_goals=ag, normalized_stats=stats
            )
            for key, fields in (("hthg", ("homeHalfGoals", "homeHalfTimeGoals")),
                                ("htag", ("awayHalfGoals", "awayHalfTimeGoals"))):
                item[key] = next((raw[v] for v in fields if raw.get(v) is not None), None)
            include_row(item)

        for fixture in comp.get("fixtures", []):
            if fixture.get("status") == "FT":
                include_actual(fixture, "kickoff")
        for participant in participants:
            canonical = name(participant.get("canonicalTeam") or participant.get("providerTeam"))
            synonyms = {name(v) for v in [
                participant.get("canonicalTeam"), participant.get("providerTeam"),
                *participant.get("aliases", [])
            ] if v}
            targets = {aliases[v] for v in synonyms if v in aliases and
                       (len(v) >= 4 or v == canonical and canonical in members)}
            if len(targets) == 1:
                for fixture in participant.get("matches", []):
                    include_actual(fixture, "date")
        leagues[code] = unified
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
                   "SHOTS": 65, "SOT": 28, "YELLOW_CARDS": 16, "CARDS": 18, "FOULS": 55}[metric]
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
        "HALFTIME1X2": ("GOALS_1H", "RESULT"),
        "HALFTIMEDOUBLECHANCE": ("GOALS_1H", "DC"),
        "BTTS": ("GOALS_FT", "BTTS"),
        "MATCHGOALS": ("GOALS_FT", "TOTAL"),
        "TEAMGOALS": ("GOALS_FT", "TEAM"),
        "TEAMTOTALGOALS": ("GOALS_FT", "TEAM"),
        "FIRSTHALFGOALS": ("GOALS_1H", "TOTAL"),
        "MATCHFIRSTHALFGOALS": ("GOALS_1H", "TOTAL"),
        "TEAMFIRSTHALFGOALS": ("GOALS_1H", "TEAM"),
        "SECONDHALFGOALS": ("GOALS_2H", "TOTAL"),
        "MATCHSECONDHALFGOALS": ("GOALS_2H", "TOTAL"),
        "TEAMSECONDHALFGOALS": ("GOALS_2H", "TEAM"),
        "MATCHCORNERS": ("CORNERS", "TOTAL"),
        "TEAMCORNERS": ("CORNERS", "TEAM"),
        "MOSTCORNERS": ("CORNERS", "RESULT"),
        "MATCHSHOTS": ("SHOTS", "TOTAL"),
        "TEAMSHOTS": ("SHOTS", "TEAM"),
        "MOSTSHOTS": ("SHOTS", "RESULT"),
        "MATCHSHOTSONTARGET": ("SOT", "TOTAL"),
        "TEAMSHOTSONTARGET": ("SOT", "TEAM"),
        "MOSTSHOTSONTARGET": ("SOT", "RESULT"),
        "MATCHYELLOWCARDS": ("YELLOW_CARDS", "TOTAL"),
        "TEAMYELLOWCARDS": ("YELLOW_CARDS", "TEAM"),
        "MATCHCARDS": ("CARDS", "TOTAL"),
        "TEAMCARDS": ("CARDS", "TEAM"),
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
        # Real provider selections are frequently "Corners Over 9.5" or
        # "Shots on Target Under 4.5", not just "Over".
        tokens = re.findall(r"\b(over|under)\b", text, flags=re.IGNORECASE)
        if len(tokens) != 1 or row["line"] is None:
            return None
        side = tokens[0].upper()
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
    if isinstance(counts, dict):
        total = counts["runs"]
        if scope == "RESULT":
            return counts[side] / total, 0.0
        if scope == "DC":
            wins = {"HOME_OR_DRAW": counts["HOME"] + counts["DRAW"],
                    "AWAY_OR_DRAW": counts["AWAY"] + counts["DRAW"],
                    "HOME_OR_AWAY": counts["HOME"] + counts["AWAY"]}[side]
            return wins / total, 0.0
        if scope == "DNB":
            return counts[side] / total, counts["DRAW"] / total
        if scope == "BTTS":
            yes = counts["BTTS"]
            return (yes if side == "YES" else total - yes) / total, 0.0
        values = counts[team_side + "_COUNTS" if team_side else "TOTAL"]
        below, through = bisect.bisect_left(values, line), bisect.bisect_right(values, line)
        wins = total - through if side == "OVER" else below
        return wins / total, (through - below) / total
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


def sampled_distribution(pairs):
    """Index the SAME 10,000 sampled pairs once for every actual market line."""
    return {
        "runs": len(pairs),
        "HOME": sum(h > a for h, a in pairs),
        "DRAW": sum(h == a for h, a in pairs),
        "AWAY": sum(a > h for h, a in pairs),
        "BTTS": sum(h > 0 and a > 0 for h, a in pairs),
        "HOME_COUNTS": sorted(h for h, _ in pairs),
        "AWAY_COUNTS": sorted(a for _, a in pairs),
        "TOTAL": sorted(h + a for h, a in pairs),
    }


def restore_fixture_quotes(con, db_path):
    """Append exact quotes from this bundle's full feeds, preserving old ordinals.

    catalog_payload contains ONE representative per market for filter menus;
    it is not a complete probability/price input. No external/provider call.
    Conflicting unattributed prices are retained so Android rejects ambiguity.
    """
    directory = Path(db_path).resolve().parent.parent / "files/app_ready_odds"
    totals = {}
    for competition in ("domestic", "champions_league", "europa_league", "conference_league"):
        path = directory / f"{competition}.json"
        if not path.is_file():
            raise ValueError(f"Full same-bundle odds input missing: {path}")
        payload = json.loads(path.read_text())
        matches = list(payload.get("matches", []))
        matches.extend(m for group in payload.get("leagues", []) for m in group.get("matches", []))
        stored = defaultdict(list)
        for fixture in con.execute("SELECT * FROM prepared_fixture_matches WHERE competition_id=?", (competition,)):
            if fixture["id"]:
                stored[str(fixture["id"])].append(dict(fixture))
        existing = defaultdict(set)
        next_ordinal = defaultdict(int)
        for row in con.execute("SELECT * FROM prepared_fixture_markets WHERE competition_id=?", (competition,)):
            key = (row["snapshot_version"], row["match_key"])
            existing[key].add((row["market"], row["selection"], row["team"], row["line"], row["odd"]))
            next_ordinal[key] = max(next_ordinal[key], row["ordinal"] + 1)
        pending = []
        for match in matches:
            for fixture in stored.get(str(match.get("id") or ""), []):
                # Same provider fixture and kickoff, not a team-name/fuzzy join.
                if kickoff(match.get("kickoff")) != kickoff(fixture["kickoff"]):
                    continue
                key = (fixture["snapshot_version"], fixture["match_key"])
                for quote in match.get("markets", []):
                    if quote.get("exactBookmakerOdds") is not True or not quote.get("bookmaker"):
                        continue
                    try:
                        odd = float(quote.get("odds", quote.get("odd")))
                        line = None if quote.get("line") is None else float(quote["line"])
                    except (ValueError, TypeError):
                        continue
                    if not math.isfinite(odd) or odd <= 1 or line is not None and not math.isfinite(line):
                        continue
                    market = str(quote.get("market") or "").strip()
                    selection = str(quote.get("selection") or "").strip()
                    team = str(quote.get("team") or "").strip() or None
                    offer = dict(fixture, market=market, selection=selection, team=team, line=line, odd=odd)
                    if quote_market(offer, "", "") is None:
                        continue
                    identity = (market, selection, team, line, odd)
                    if identity in existing[key]:
                        continue
                    existing[key].add(identity)
                    ordinal = next_ordinal[key]
                    next_ordinal[key] += 1
                    pending.append((competition, *key, ordinal, market, selection, team, line, odd))
        con.executemany("INSERT INTO prepared_fixture_markets VALUES (?,?,?,?,?,?,?,?,?)", pending)
        totals[competition] = len(pending)
    return totals


def run(db_path):
    history = load_actual_results()
    now = datetime.now(timezone.utc).timestamp()
    counts = defaultdict(int)
    with sqlite3.connect(db_path) as con:
        con.row_factory = sqlite3.Row
        restored = restore_fixture_quotes(con, db_path)
        print("BETTING_V2_FULL_QUOTES_RESTORED", json.dumps(restored, sort_keys=True), flush=True)
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
               OR (f.competition_id IN
                      ('champions_league','europa_league','conference_league')
                   AND f.team_mapping_status='provider_identity'
                   AND TRIM(f.id)<>'' AND TRIM(f.home_team)<>''
                   AND TRIM(f.away_team)<>'')
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
            league = league_code(row["league_code"])
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
                aliases = UEFA_ALIASES.get(league, {})
                choices = [aliases.get(v, v) for v in choices]
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
                draws[key] = sampled_distribution(samples(hr, ar, "|".join(map(str, key)) + "|" + MODEL))
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
        counts["restored_exact_quotes"] = sum(restored.values())
    return dict(counts)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("prepared_db", type=Path)
    args = parser.parse_args()
    print("BETTING_V2_DIRECT_MODEL", json.dumps(run(args.prepared_db), sort_keys=True))

