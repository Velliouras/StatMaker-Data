#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Sequence
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "data/statmaker/domestic_live_july_registry.json"
ODDS = ROOT / "odds/odds_api_io/domestic_odds.json"
REPORT = ROOT / "reports/turkey_super_lig_schedule_sync.json"
API_BASE = "https://v3.football.api-sports.io"
CODE = "T1"
COUNTRY = "Turkey"
COMPETITION = "Süper Lig"
LEAGUE_ID = 203
SEASON = 2026
FINAL_STATUSES = {"FT", "AET", "PEN", "CANC", "ABD", "AWD", "WO"}


def read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def validate_registry(payload: Dict[str, Any]) -> Dict[str, Any]:
    matches = [
        row for row in payload.get("leagues", []) or []
        if isinstance(row, dict) and str(row.get("leagueCode") or "") == CODE
    ]
    if len(matches) != 1:
        raise RuntimeError(f"Expected exactly one {CODE} registry row, found {len(matches)}")
    row = matches[0]
    if int(row.get("apiFootballLeagueId") or row.get("api_football_league_id") or 0) != LEAGUE_ID:
        raise RuntimeError("T1 API-Football league id changed; refusing schedule publication")
    if str(row.get("targetApiSeason") or row.get("season") or "") != str(SEASON):
        raise RuntimeError("T1 target season changed; refusing schedule publication")
    if str(row.get("providerLeagueSlug") or "").strip():
        raise RuntimeError("T1 unexpectedly has a provider slug; review exact-odds integration before schedule sync")
    return row


class ApiFootballClient:
    def __init__(self, key: str) -> None:
        self.key = key
        self.requests_used = 0

    def get(self, endpoint: str, params: Dict[str, Any]) -> Dict[str, Any]:
        self.requests_used += 1
        query = urlencode(params)
        request = Request(
            f"{API_BASE}/{endpoint}?{query}",
            headers={
                "x-apisports-key": self.key,
                "Accept": "application/json",
                "User-Agent": "StatMaker T1 schedule sync",
            },
        )
        with urlopen(request, timeout=45) as response:
            payload = json.loads(response.read().decode("utf-8"))
        errors = payload.get("errors")
        if errors not in (None, {}, [], ""):
            raise RuntimeError(f"API-Football {endpoint}: {errors}")
        return payload


def response_rows(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [row for row in payload.get("response", []) or [] if isinstance(row, dict)]


def fixture_status(row: Dict[str, Any]) -> str:
    return str(((row.get("fixture") or {}).get("status") or {}).get("short") or "").upper()


def fixture_id(row: Dict[str, Any]) -> str:
    return str((row.get("fixture") or {}).get("id") or "").strip()


def kickoff(row: Dict[str, Any]) -> str:
    return str((row.get("fixture") or {}).get("date") or "").strip()


def schedule_row(row: Dict[str, Any]) -> Dict[str, Any]:
    home = ((row.get("teams") or {}).get("home") or {})
    away = ((row.get("teams") or {}).get("away") or {})
    home_name = str(home.get("name") or "").strip()
    away_name = str(away.get("name") or "").strip()
    when = kickoff(row)
    if not fixture_id(row) or not home_name or not away_name or not when:
        raise RuntimeError("Incomplete official T1 fixture returned by API-Football")
    return {
        "id": fixture_id(row),
        "date": when[:10],
        "kickoff": when,
        "providerHomeTeam": home_name,
        "providerAwayTeam": away_name,
        "homeTeam": home_name,
        "awayTeam": away_name,
        "canonicalHomeTeam": home_name,
        "canonicalAwayTeam": away_name,
        "homeTeamLogo": home.get("logo"),
        "awayTeamLogo": away.get("logo"),
        "teamMappingStatus": "schedule_only_api_football",
        "usableForStats": False,
        "scheduleOnly": True,
        "scheduleSource": "api-football",
        "scheduleVerified": True,
        "markets": [],
    }


def match_key(match: Dict[str, Any]) -> tuple[str, ...]:
    match_id = str(match.get("id") or match.get("matchId") or "").strip()
    if match_id:
        return ("id", match_id)
    return (
        "fixture",
        str(match.get("homeTeam") or "").strip().casefold(),
        str(match.get("awayTeam") or "").strip().casefold(),
        str(match.get("date") or match.get("kickoff") or "")[:10],
    )


def merge_schedule(
    feed: Dict[str, Any],
    official_fixtures: Sequence[Dict[str, Any]],
    registry_row: Dict[str, Any],
    generated_at: str,
) -> tuple[Dict[str, Any], Dict[str, int]]:
    result = json.loads(json.dumps(feed))
    leagues = [row for row in result.get("leagues", []) or [] if isinstance(row, dict)]
    league = next((row for row in leagues if str(row.get("leagueCode") or "") == CODE), None)
    if league is None:
        league = {
            "leagueCode": CODE,
            "country": COUNTRY,
            "competition": COMPETITION,
            "season": registry_row.get("targetAppSeason") or "2026-2027",
            "apiFootballLeagueId": LEAGUE_ID,
            "enabledForStats": True,
            "enabledForOdds": bool(registry_row.get("enabledForOdds", True)),
            "enabledForBetting": bool(registry_row.get("enabledForBetting", True)),
            "providerLeagueSlug": None,
            "matches": [],
        }
        leagues.append(league)

    if str(league.get("providerLeagueSlug") or "").strip():
        raise RuntimeError("Refusing to overwrite a verified T1 provider slug")

    kept = [
        dict(match) for match in league.get("matches", []) or []
        if isinstance(match, dict)
        and not (
            match.get("scheduleOnly") is True
            and str(match.get("scheduleSource") or "") == "api-football"
            and not (match.get("markets") or [])
        )
    ]
    by_key = {match_key(match): match for match in kept}
    added = updated = preserved_bettable = 0

    for fixture in official_fixtures:
        fresh = schedule_row(fixture)
        key = match_key(fresh)
        current = by_key.get(key)
        if current is None:
            by_key[key] = fresh
            added += 1
            continue
        merged = {**fresh, **current}
        merged["date"] = fresh["date"]
        merged["kickoff"] = fresh["kickoff"]
        merged["homeTeamLogo"] = fresh.get("homeTeamLogo") or current.get("homeTeamLogo")
        merged["awayTeamLogo"] = fresh.get("awayTeamLogo") or current.get("awayTeamLogo")
        merged["scheduleVerified"] = True
        if current.get("markets"):
            merged["scheduleOnly"] = False
            preserved_bettable += 1
        else:
            merged.update({
                "teamMappingStatus": "schedule_only_api_football",
                "usableForStats": False,
                "scheduleOnly": True,
                "scheduleSource": "api-football",
                "markets": [],
            })
        by_key[key] = merged
        updated += 1

    league.update({
        "country": COUNTRY,
        "competition": COMPETITION,
        "season": registry_row.get("targetAppSeason") or "2026-2027",
        "apiFootballLeagueId": LEAGUE_ID,
        "matches": sorted(
            by_key.values(),
            key=lambda row: (str(row.get("kickoff") or row.get("date") or ""), str(row.get("homeTeam") or "")),
        ),
    })
    league["providerLeagueSlug"] = None
    result["leagues"] = leagues
    result["generatedAt"] = generated_at
    result.setdefault("debug", {})["turkeySuperLigSchedule"] = {
        "source": "api-football",
        "leagueId": LEAGUE_ID,
        "season": SEASON,
        "scheduleOnly": True,
        "syntheticOdds": False,
        "officialFixtures": len(official_fixtures),
        "added": added,
        "updated": updated,
        "preservedBettable": preserved_bettable,
    }
    return result, {
        "officialFixtures": len(official_fixtures),
        "added": added,
        "updated": updated,
        "preservedBettable": preserved_bettable,
        "publishedMatches": len(league.get("matches", [])),
    }


def main() -> int:
    key = os.getenv("API_FOOTBALL_KEY", "").strip()
    if not key:
        print("ERROR: API_FOOTBALL_KEY is required.")
        return 2

    registry_payload = read_json(REGISTRY, {})
    registry_row = validate_registry(registry_payload)
    today = dt.datetime.now(dt.timezone.utc).date()
    horizon = max(1, min(45, int(os.getenv("T1_SCHEDULE_HORIZON_DAYS", "21"))))
    end = today + dt.timedelta(days=horizon)

    client = ApiFootballClient(key)
    payload = client.get("fixtures", {
        "league": LEAGUE_ID,
        "season": SEASON,
        "from": today.isoformat(),
        "to": end.isoformat(),
        "timezone": "UTC",
    })
    fixtures = [
        row for row in response_rows(payload)
        if fixture_status(row) not in FINAL_STATUSES
        and today.isoformat() <= kickoff(row)[:10] <= end.isoformat()
    ]
    fixtures.sort(key=lambda row: (kickoff(row), fixture_id(row)))

    generated_at = now_utc()
    feed = read_json(ODDS, {"schemaVersion": 3, "source": "odds-api-io", "leagues": []})
    merged, metrics = merge_schedule(feed, fixtures, registry_row, generated_at)
    write_json(ODDS, merged)

    report = {
        "generatedAt": generated_at,
        "leagueCode": CODE,
        "country": COUNTRY,
        "competition": COMPETITION,
        "apiFootballLeagueId": LEAGUE_ID,
        "targetSeason": SEASON,
        "from": today.isoformat(),
        "to": end.isoformat(),
        "requestsUsed": client.requests_used,
        "providerLeagueSlug": registry_row.get("providerLeagueSlug"),
        "scheduleSource": "api-football",
        "scheduleVerified": True,
        "syntheticOdds": False,
        **metrics,
    }
    write_json(REPORT, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
