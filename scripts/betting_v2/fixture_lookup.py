#!/usr/bin/env python3
"""Offline identity resolution of bookmaker match to cached provider fixture.

NO guessing: same league, EXACT normalized team names, UTC kickoff within
15 minutes, exactly one candidate. An explicit API-Football fixture ID is
checked against the indexed fixture rather than trusted as a raw string.
Unmatched/ambiguous quotes are excluded and counted; never fuzzy-match.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import unicodedata


def canonical_name(name: object) -> str:
    name = unicodedata.normalize("NFKD", str(name or ""))
    name = "".join(c for c in name if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", name.casefold()).strip()


def as_utc(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except ValueError:
        return None


class CachedFixtureLookup:
    """Only canonical indexed enriched results, never provider requests."""
    def __init__(self, data_root: Path):
        self.by_league_team = defaultdict(list)
        self.by_id = {}
        path = data_root / "data/statmaker/domestic_enriched/index.json"
        index = json.loads(path.read_text(encoding="utf-8"))
        if index.get("schema_version") != 3:
            raise ValueError("Unrecognized cached enriched index")
        for entry in index.get("leagues", []):
            rel = Path(str(entry.get("output_path") or ""))
            if rel.is_absolute() or ".." in rel.parts:
                raise ValueError("Unsafe historical fixture path")
            source = data_root / rel
            if not source.is_file():
                continue
            data = json.loads(source.read_text(encoding="utf-8"))
            if data.get("schema_version") != 3:
                continue
            league = str(data.get("competition", {}).get("league_code") or "")
            for match in data.get("matches", []):
                fid = match.get("fixture_id")
                kick = as_utc(match.get("date_utc"))
                home = canonical_name(match.get("home_team"))
                away = canonical_name(match.get("away_team"))
                if not fid or not kick or not league or not home or not away:
                    continue
                item = {
                    "fixtureId": str(fid),
                    "kickoffUTC": kick,
                    "leagueCode": league,
                    "home": home,
                    "away": away,
                }
                self.by_league_team[(league, home, away)].append(item)
                # A duplicate ID from inconsistent cached seasons is unsafe.
                previous = self.by_id.get(str(fid))
                if previous is not None and previous != item:
                    self.by_id[str(fid)] = None
                elif str(fid) not in self.by_id:
                    self.by_id[str(fid)] = item

    def resolve(self, match: dict, kickoff: datetime,
                provider_fixture_id: str | None) -> tuple[str | None, str]:
        league = str(match.get("leagueCode") or "").strip()
        names = []
        for h, a in (
            ("canonicalHomeTeam", "canonicalAwayTeam"),
            ("providerHomeTeam", "providerAwayTeam"),
            ("homeTeam", "awayTeam"),
        ):
            home = canonical_name(match.get(h))
            away = canonical_name(match.get(a))
            if home and away:
                names.append((home, away))

        if not league or not names:
            return None, "MISSING_TEAM_OR_COMPETITION"
        possible: dict[str, dict] = {}
        for home, away in names:
            for item in self.by_league_team.get((league, home, away), ()):
                if abs((item["kickoffUTC"] - kickoff).total_seconds()) <= 900:
                    possible[item["fixtureId"]] = item
        if provider_fixture_id is not None:
            confirmed = self.by_id.get(str(provider_fixture_id))
            if confirmed is None:
                return None, "UNVERIFIED_EXPLICIT_FIXTURE_ID"
            if str(provider_fixture_id) not in possible:
                return None, "EXPLICIT_FIXTURE_MISMATCH"
        if not possible:
            return None, "NO_EXACT_KICKOFF_TEAM_MATCH"
        if len(possible) != 1:
            return None, "AMBIGUOUS_FIXTURE"
        return next(iter(possible)), "EXACT_CACHED_FIXTURE"


if __name__ == "__main__":
    raise SystemExit("Import CachedFixtureLookup from offline historical replay")
