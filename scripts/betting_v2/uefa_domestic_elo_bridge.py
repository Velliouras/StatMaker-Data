"""UEFA research-only forecasting from the existing, previous-season-aware App-Ready team ELO.

Uses the SAME materialized prepared_team_elo contract as Match Simulation,
together with real prior Domestic results/stats. No external provider calls,
guesswork, synthetic match outcomes, new ELO algorithm, or STRONG certification.

Research bridge is NOT evidence of cross-league calibration, bankroll EV or ROI.
Unmapped/ambiguous team identity, missing Elo or insufficient prior form block
a fixture rather than silently assigning a league-average rating.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3

from scripts.betting_v2.fixture_lookup import canonical_name
from scripts.betting_v2.audit_data import number
from scripts.betting_v2.walk_forward_elo import lambda_rates
from scripts.betting_v2.freeze_prematch_shadow import ELO_PARAMS

UEFA_CODES = {"CL", "EL", "UECL"}
COUNT_STATS = {
    "CORNERS": ("HC", "AC"),
    "SHOTS": ("HS", "AS"),
    "SOT": ("HST", "AST"),
    "YELLOW_CARDS": ("HY", "AY"),
}
MIN_PRIOR_GAMES = 8
MIN_VENUE_GAMES = 3


class UefaDomesticEloBridge:
    def __init__(self, db_path: Path, root: Path, domestic_history: list[dict],
                 cutoff: datetime):
        self.blockers: dict[str, int] = defaultdict(int)
        self.ratings: dict[str, list[dict]] = defaultdict(list)
        self.forms: dict[str, list[dict]] = defaultdict(list)
        self.aliases: dict[str, dict[str, str]] = defaultdict(dict)
        self.rate_keys: set[str] = set()
        self.available = False

        with sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True) as db:
            table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='prepared_team_elo'"
            ).fetchone()
            if table is None:
                self.blockers["PREPARED_TEAM_ELO_TABLE_MISSING"] += 1
                return
            rows = db.execute(
                "SELECT league_code,season,team_name,elo_rating,previous_rating,"
                "historical_matches,current_matches FROM prepared_team_elo"
            ).fetchall()
        for league, season, name, elo, previous, historical, current in rows:
            key = canonical_name(name)
            if not key or not math.isfinite(elo):
                continue
            if int(historical or 0) + int(current or 0) < MIN_PRIOR_GAMES:
                continue
            self.ratings[key].append({
                "name": name, "league": str(league), "season": str(season),
                "elo": float(elo), "previous": previous,
                "historical": int(historical or 0),
                "current": int(current or 0),
            })
        self.available = bool(self.ratings)
        for m in domestic_history:
            # Only actual prior, completed FT historical matches. No UEFA support
            # match is ever used as domestic-league context.
            if m.get("prematch_target") or m["date"] >= cutoff:
                continue
            if not str(m.get("group", "")).split("|")[0] not in UEFA_CODES:
                continue
            for team_field, own, conceded, venue in (
                ("home", "hg", "ag", "home"), ("away", "ag", "hg", "away")
            ):
                own_goals, against = m.get(own), m.get(conceded)
                if not isinstance(own_goals, int) or not isinstance(against, int):
                    continue
                key = canonical_name(m.get(team_field))
                if not key:
                    continue
                self.forms[key].append({
                    "date": m["date"], "venue": venue,
                    "forgoals": own_goals, "againstgoals": against,
                    "stats": m.get("stats") or {},
                })
        for rows in self.forms.values():
            rows.sort(key=lambda m: m["date"])

        # The competition configuration supplies explicit naming aliases only.
        # It must never provide fabricated ratings or historic outcome labels.
        config = root / "config/uefa_simulation_2026_27.json"
        if config.is_file():
            payload = json.loads(config.read_text(encoding="utf-8"))
            for comp in payload.get("competitions", []):
                code = str(comp.get("leagueCode") or "").upper()
                if code not in UEFA_CODES:
                    continue
                for label, canonical in (comp.get("aliases") or {}).items():
                    k, target = canonical_name(label), canonical_name(canonical)
                    if k and target:
                        self.aliases[code][k] = target

    def _resolve(self, code: str, *names: str) -> tuple[str, dict] | None:
        # Require exact normalized identity, or one explicitly configured alias.
        # A repeated name from several leagues is ambiguous, NOT an Elo match.
        for name in names:
            key = canonical_name(name)
            if not key:
                continue
            found = self.ratings.get(key) or self.ratings.get(self.aliases[code].get(key, ""))
            if found:
                current_season = max((r["season"] for r in found))
                current = [r for r in found if r["season"] == current_season]
                if len(current) != 1:
                    self.blockers["AMBIGUOUS_PREPARED_ELO_IDENTITY"] += 1
                    return None
                resolved_key = canonical_name(current[0]["name"])
                return resolved_key, current[0]
        self.blockers["MISSING_PREPARED_ELO_TEAM"] += 1
        return None

    @staticmethod
    def _mean(items: list[dict], field: str) -> float:
        return sum(float(item[field]) for item in items) / len(items)

    def _sample(self, key: str, date: datetime) -> tuple[list[dict], list[dict]]:
        # Never leak same-day completed results into a prematch forecast.
        previous = [m for m in self.forms.get(key, [])
                    if m["date"].date() < date.date()]
        return previous[-20:], previous

    def forecast(self, row: dict) -> tuple[float, float, dict] | None:
        code = row["leagueCode"]
        if code not in UEFA_CODES or not self.available:
            self.blockers["ELO_SOURCE_UNAVAILABLE"] += 1
            return None
        kickoff = datetime.fromisoformat(str(row["kickoff"]).replace("Z", "+00:00"))
        home = self._resolve(code, row.get("homeTeam"), row.get("canonicalHomeTeam"),
                             row.get("providerHomeTeam"))
        away = self._resolve(code, row.get("awayTeam"), row.get("canonicalAwayTeam"),
                             row.get("providerAwayTeam"))
        if home is None or away is None or home[0] == away[0]:
            self.blockers["UNRESOLVED_CROSS_COMPETITION_TEAMS"] += 1
            return None
        h_key, h_rating = home
        a_key, a_rating = away
        h_last, _ = self._sample(h_key, kickoff)
        a_last, _ = self._sample(a_key, kickoff)
        hv = [r for r in h_last if r["venue"] == "home"][-8:]
        av = [r for r in a_last if r["venue"] == "away"][-8:]
        if min(len(h_last), len(a_last)) < MIN_PRIOR_GAMES or min(len(hv), len(av)) < MIN_VENUE_GAMES:
            self.blockers["INSUFFICIENT_REAL_DOMESTIC_FORM"] += 1
            return None

        def profile(last: list[dict], venue: list[dict], field: str):
            return (self._mean(last[-5:], field), self._mean(last, field),
                    self._mean(venue, field))

        # lambda_rates reads feature attributes only. Do not manufacture a
        # match score merely to satisfy a historical training-row structure.
        from types import SimpleNamespace
        features = SimpleNamespace(
            attack_h=profile(h_last, hv, "forgoals"),
            defense_h=profile(h_last, hv, "againstgoals"),
            attack_a=profile(a_last, av, "forgoals"),
            defense_a=profile(a_last, av, "againstgoals"),
            elo_delta=h_rating["elo"] - a_rating["elo"] + 55.0,
            league_h=1.4, league_a=1.15,
        )
        h, a = lambda_rates(features, ELO_PARAMS)
        return (h, a, {
            "homeKey": h_key, "awayKey": a_key,
            "homeElo": h_rating["elo"], "awayElo": a_rating["elo"],
            "homeEloFromSeason": h_rating["season"],
            "awayEloFromSeason": a_rating["season"],
            "homeHistoricalMatches": h_rating["historical"],
            "awayHistoricalMatches": a_rating["historical"],
            "homeObservedPriorMatches": len(h_last),
            "awayObservedPriorMatches": len(a_last),
            "source": "EXISTING_PREPARED_TEAM_ELO_AND_REAL_DOMESTIC_FORM",
            "certified": False,
        })

    def metric_rates(self, row: dict, family: str, evidence: dict,
                     kickoff: datetime) -> tuple[float, float] | None:
        fields = COUNT_STATS.get(family)
        if not fields:
            return None
        home_key = evidence["homeKey"]
        away_key = evidence["awayKey"]

        def metric(team_key: str, perspective: str) -> tuple[list[tuple], list[tuple]]:
            out = []
            hk, ak = fields
            for item in self._sample(team_key, kickoff)[0]:
                own, conceded = (hk, ak) if item["venue"] == "home" else (ak, hk)
                gf, ga = number(item["stats"].get(own)), number(item["stats"].get(conceded))
                if gf is not None and ga is not None and gf >= 0 and ga >= 0:
                    out.append((item["venue"], gf, ga))
            venue = [r for r in out if r[0] == perspective][-8:]
            return out, venue
        home, hv = metric(home_key, "home")
        away, av = metric(away_key, "away")
        if min(len(home), len(away)) < MIN_PRIOR_GAMES or min(len(hv), len(av)) < MIN_VENUE_GAMES:
            self.blockers["INSUFFICIENT_REAL_MARKET_STATS"] += 1
            return None

        def mean(items: list[tuple], i: int) -> float:
            return sum(v[i] for v in items) / len(items)
        h = .35*mean(home, 1) + .35*mean(hv, 1) + .30*mean(av, 2)
        a = .35*mean(away, 1) + .35*mean(av, 1) + .30*mean(hv, 2)
        return (max(0.05, h), max(0.05, a))
