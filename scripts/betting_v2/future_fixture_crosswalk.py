#!/usr/bin/env python3
"""Independent API-Football upcoming-fixture identity verification, OFFLINE.

Uses the PREEXISTING 'schedule_fixtures' list from API-Football fixture_stats
cache. No provider calls, fuzzy aliases, guessed fixture IDs or score leakage.
Every match must pass league+season+exact both normalized team names+UTC
kickoff (<=15 minutes)+unique fixture identity and source scope tests.

The resulting crosswalk establishes an identity at the time it is BUILT, not
retroactive source availability at an earlier bookmaker-quote timestamp.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
import json

from fixture_lookup import canonical_name, as_utc

MAX_CACHE_SIZE = 55_000_000
FINAL_STATUSES = frozenset({"FT", "AET", "PEN", "CANC", "ABD", "AWD", "WO"})
SCHEDULED_STATUSES = frozenset({"NS"})
MAX_KICKOFF_DELTA_SECONDS = 900


class CachedUpcomingFixtureIndex:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.rejections: Counter = Counter()
        self.by_league_teams: dict[tuple[str,str,str], list[dict]] = defaultdict(list)
        index_path = self.root / "data/statmaker/domestic_enriched/index.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        if index.get("schema_version") != 3:
            raise ValueError("Unknown API-Football enriched index")
        for league in index.get("leagues") or []:
            code = str(league.get("league_code") or "")
            season = str(league.get("api_football_season") or "")
            expected = league.get("api_football_league_id")
            path = Path(str(league.get("cache_path") or ""))
            if not code or not season or not expected or (
                path.is_absolute() or ".." in path.parts
            ):
                self.rejections["invalid_league_cache_scope"] += 1
                continue
            full = (self.root / path).resolve()
            if not full.is_relative_to(self.root) or not full.is_file():
                self.rejections["missing_cache"] += 1
                continue
            if full.stat().st_size > MAX_CACHE_SIZE:
                self.rejections["oversized_cache"] += 1
                continue
            try:
                data = json.loads(full.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeError):
                self.rejections["invalid_cache_json"] += 1
                continue
            if not isinstance(data, dict) or data.get("provider") != "api-football":
                self.rejections["not_api_football_cache"] += 1
                continue
            if str(data.get("league_id")) != str(expected) or (
                str(data.get("season")) != season
            ):
                self.rejections["league_or_season_cache_mismatch"] += 1
                continue
            for row in data.get("schedule_fixtures") or []:
                if not isinstance(row, dict):
                    continue
                fixture_id = row.get("fixture_id")
                start = as_utc(row.get("date"))
                home = canonical_name(row.get("home_team"))
                away = canonical_name(row.get("away_team"))
                status = str(row.get("status") or "").upper()
                if not fixture_id or not start or not home or not away or home == away:
                    self.rejections["incomplete_upcoming_fixture"] += 1
                    continue
                if status not in SCHEDULED_STATUSES:
                    self.rejections["not_confirmed_not_started_fixture"] += 1
                    continue
                source = row.get("source_league")
                if not isinstance(source, dict):
                    self.rejections["missing_source_league_identity"] += 1
                    continue
                if str(source.get("id")) != str(expected) or (
                    str(source.get("season")) != season
                ):
                    self.rejections["source_league_season_mismatch"] += 1
                    continue
                expected_query = f"league+season:{season}"
                if str(row.get("fixture_query_used")) != expected_query:
                    self.rejections["non_exact_season_query"] += 1
                    continue
                item = {
                    "leagueCode": code,
                    "apiFootballLeagueId": int(expected),
                    "apiFootballSeason": season,
                    "apiFootballFixtureId": str(fixture_id),
                    "kickoffUTC": start.isoformat(),
                    "canonicalHomeTeam": home,
                    "canonicalAwayTeam": away,
                    "cachePath": path.as_posix(),
                }
                self.by_league_teams[(code, home, away)].append(item)
        self.entries = sum(len(v) for v in self.by_league_teams.values())

    def resolve(self, event: dict) -> tuple[dict | None, str]:
        code = str(event.get("leagueCode") or "")
        home = canonical_name(event.get("home"))
        away = canonical_name(event.get("away"))
        kickoff = as_utc(event.get("kickoffUTC"))
        if not code or not home or not away or kickoff is None:
            return None, "MISSING_EVENT_IDENTITY_OR_KICKOFF"
        found = {}
        for item in self.by_league_teams.get((code, home, away), []):
            actual = as_utc(item["kickoffUTC"])
            if actual is None:
                continue
            if abs((actual - kickoff).total_seconds()) <= MAX_KICKOFF_DELTA_SECONDS:
                key = (item["apiFootballLeagueId"], item["apiFootballFixtureId"])
                found[key] = item
        if not found:
            return None, "NO_EXACT_INDEPENDENT_API_FOOTBALL_FUTURE_FIXTURE"
        if len(found) != 1:
            return None, "AMBIGUOUS_API_FOOTBALL_FIXTURE"
        return next(iter(found.values())), "EXACT_INDEPENDENT_API_FOOTBALL_FUTURE_FIXTURE"
