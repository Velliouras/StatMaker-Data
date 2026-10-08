#!/usr/bin/env python3
"""Refresh current-season UEFA club statistics from API-Football.

This producer is intentionally separate from Domestic history and from betting odds.
It publishes only completed Champions League / Europa League / Conference League
fixtures inside the configured current UEFA season window. Fixture statistics are
incremental: an already-complete fixture is reused by fixture_id and only new or
previously incomplete fixtures spend statistics requests.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping

import api_football_fetch_fixture_stats as api
import canonical_team_identity

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "uefa_simulation_2026_27.json"
OUTPUT = ROOT / "data" / "statmaker" / "uefa_current_stats.json"
REPORT = ROOT / "reports" / "uefa_current_stats_refresh.json"
COMPLETED = {"FT", "AET", "PEN"}


def load_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def today_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).date().isoformat()


def norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def season_present(row: Mapping[str, Any], season: int) -> bool:
    for item in row.get("seasons", []) or []:
        if isinstance(item, dict) and int(item.get("year") or -1) == season:
            return True
    return False


def discover_league(api_key: str, competition: Mapping[str, Any], season: int, request_state: dict[str, int], max_requests: int) -> Mapping[str, Any]:
    expected = norm(competition.get("name"))
    blocked = {"women", "womens", "youth", "u19", "u21"}
    candidates: list[tuple[int, Mapping[str, Any]]] = []
    seen_ids: set[int] = set()

    for term in competition.get("apiSearchTerms", []) or [competition.get("name")]:
        payload = api.api_get(api_key, "leagues", {"search": term}, request_state, max_requests)
        for row in api.response_items(payload):
            league = row.get("league") if isinstance(row, dict) else None
            if not isinstance(league, dict):
                continue
            league_id = league.get("id")
            if league_id is None:
                continue
            league_id = int(league_id)
            if league_id in seen_ids:
                continue
            seen_ids.add(league_id)
            name = norm(league.get("name"))
            words = set(name.split())
            if words & blocked or not season_present(row, season):
                continue
            score = 0
            if name == expected:
                score += 1000
            if expected and expected in name:
                score += 500
            for search_term in competition.get("apiSearchTerms", []) or []:
                token = norm(search_term)
                if token and token == name:
                    score += 900
                elif token and token in name:
                    score += 350
            if str(league.get("type") or "").lower() == "cup":
                score += 50
            if score > 0:
                candidates.append((score, row))
        if candidates:
            break

    if not candidates:
        raise RuntimeError(f"No API-Football league match for {competition.get('competitionId')} season={season}")

    candidates.sort(
        key=lambda pair: (
            pair[0],
            int((pair[1].get("league") or {}).get("id") or 0),
        ),
        reverse=True,
    )
    return candidates[0][1]


def canonical_index(competition: Mapping[str, Any]):
    canonical_names = [
        str(team).strip()
        for pot in competition.get("pots", []) or []
        for team in (pot if isinstance(pot, list) else [])
        if str(team).strip()
    ]
    return canonical_team_identity.configured_index(
        scope=str(competition.get("leagueCode") or competition.get("competitionId") or "UEFA"),
        canonical_names=canonical_names,
        aliases=dict(competition.get("aliases") or {}),
    )


def canonical_team(name: str, index) -> tuple[str, str]:
    value, status, _candidates = index.resolve(name)
    return (value or name.strip(), status)


def fixture_id(item: Mapping[str, Any]) -> int | None:
    raw = (item.get("fixture") or {}).get("id") if isinstance(item.get("fixture"), dict) else None
    return int(raw) if raw is not None else None


def fixture_date(item: Mapping[str, Any]) -> str:
    value = str((item.get("fixture") or {}).get("date") or "")
    return value[:10]


def fixture_status(item: Mapping[str, Any]) -> str:
    block = (item.get("fixture") or {}).get("status") if isinstance(item.get("fixture"), dict) else {}
    return str((block or {}).get("short") or "").upper()


def existing_fixture_map(previous: Mapping[str, Any], competition_id: str) -> dict[int, Mapping[str, Any]]:
    for competition in previous.get("competitions", []) or []:
        if not isinstance(competition, dict) or competition.get("competitionId") != competition_id:
            continue
        result = {}
        for row in competition.get("fixtures", []) or []:
            if not isinstance(row, dict):
                continue
            raw = row.get("fixtureId")
            if raw is not None:
                result[int(raw)] = row
        return result
    return {}


def normalized_fixture(
    item: Mapping[str, Any],
    competition: Mapping[str, Any],
    provider_row: Mapping[str, Any],
    index,
    previous: Mapping[str, Any] | None,
    stats: Mapping[str, Any] | None,
    stats_fetched: bool,
) -> dict[str, Any]:
    fixture = item.get("fixture") or {}
    teams = item.get("teams") or {}
    home = teams.get("home") or {}
    away = teams.get("away") or {}
    goals = item.get("goals") or {}
    league = item.get("league") or {}
    provider_league = provider_row.get("league") or {}

    home_name, home_status = canonical_team(str(home.get("name") or ""), index)
    away_name, away_status = canonical_team(str(away.get("name") or ""), index)
    fid = fixture_id(item)
    if fid is None:
        raise RuntimeError("Completed UEFA fixture has no fixture id")

    return {
        "fixtureId": fid,
        "date": str(fixture.get("date") or "")[:10],
        "kickoff": str(fixture.get("date") or ""),
        "status": fixture_status(item),
        "round": str(league.get("round") or ""),
        "competition": str(provider_league.get("name") or competition.get("name") or ""),
        "leagueCode": str(competition.get("leagueCode") or ""),
        "season": int(league.get("season") or 0),
        "homeTeam": home_name,
        "awayTeam": away_name,
        "providerHomeTeam": str(home.get("name") or ""),
        "providerAwayTeam": str(away.get("name") or ""),
        "homeTeamId": home.get("id"),
        "awayTeamId": away.get("id"),
        "homeTeamLogo": str(home.get("logo") or ""),
        "awayTeamLogo": str(away.get("logo") or ""),
        "homeGoals": int(goals.get("home") or 0),
        "awayGoals": int(goals.get("away") or 0),
        "teamMappingStatus": {
            "home": home_status,
            "away": away_status,
        },
        "statsFetched": bool(stats_fetched),
        "normalizedStats": dict(stats or previous.get("normalizedStats") or {}) if previous else dict(stats or {}),
        "sourceLeague": {
            "id": league.get("id"),
            "name": league.get("name"),
            "country": league.get("country"),
            "season": league.get("season"),
            "round": league.get("round"),
        },
        "source": "API-Football current UEFA season",
    }


def refresh_competition(
    api_key: str,
    competition: Mapping[str, Any],
    season: int,
    display_season: str,
    previous: Mapping[str, Any],
    request_state: dict[str, int],
    max_requests: int,
    through_date: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    provider_row = discover_league(api_key, competition, season, request_state, max_requests)
    provider_league = provider_row.get("league") or {}
    league_id = int(provider_league.get("id"))
    phase_start = str(competition.get("phaseStart") or f"{season:04d}-09-01")
    from_date = max(phase_start, f"{season:04d}-09-01")

    fixtures_payload = api.api_get(
        api_key,
        "fixtures",
        {"league": league_id, "season": season, "from": from_date, "to": through_date},
        request_state,
        max_requests,
    )
    fixtures = [
        item for item in api.response_items(fixtures_payload)
        if isinstance(item, dict)
        and fixture_status(item) in COMPLETED
        and fixture_date(item) >= from_date
        and fixture_date(item) <= through_date
    ]
    fixtures.sort(key=lambda item: (fixture_date(item), fixture_id(item) or 0))

    existing = existing_fixture_map(previous, str(competition.get("competitionId")))
    index = canonical_index(competition)
    output_rows: list[dict[str, Any]] = []
    stats_reused = 0
    stats_requested = 0
    stats_missing = 0

    for item in fixtures:
        fid = fixture_id(item)
        if fid is None:
            continue
        old = existing.get(fid)
        if old and bool(old.get("statsFetched")) and isinstance(old.get("normalizedStats"), dict):
            output_rows.append(
                normalized_fixture(
                    item, competition, provider_row, index, old, old.get("normalizedStats"), True
                )
            )
            stats_reused += 1
            continue

        stats_payload = api.api_get(
            api_key,
            "fixtures/statistics",
            {"fixture": fid},
            request_state,
            max_requests,
        )
        raw_stats = api.response_items(stats_payload)
        normalized = api.normalize_statistics(raw_stats, item) if raw_stats else {}
        fetched = bool(raw_stats)
        if fetched:
            stats_requested += 1
        else:
            stats_missing += 1
        output_rows.append(
            normalized_fixture(item, competition, provider_row, index, old, normalized, fetched)
        )

    participants = [
        str(team).strip()
        for pot in competition.get("pots", []) or []
        for team in (pot if isinstance(pot, list) else [])
        if str(team).strip()
    ]
    participants = list(dict.fromkeys(participants))
    fixture_teams = [
        team
        for row in output_rows
        for team in (str(row.get("homeTeam") or ""), str(row.get("awayTeam") or ""))
        if team
    ]
    participants = list(dict.fromkeys(participants + fixture_teams))

    logos: dict[str, str] = {}
    for row in output_rows:
        if str(row.get("homeTeamLogo") or "").startswith("http"):
            logos[str(row.get("homeTeam"))] = str(row.get("homeTeamLogo"))
        if str(row.get("awayTeamLogo") or "").startswith("http"):
            logos[str(row.get("awayTeam"))] = str(row.get("awayTeamLogo"))

    result = {
        "competitionId": str(competition.get("competitionId") or ""),
        "leagueCode": str(competition.get("leagueCode") or ""),
        "name": str(competition.get("name") or provider_league.get("name") or ""),
        "season": season,
        "displaySeason": display_season,
        "phaseStart": from_date,
        "throughDate": through_date,
        "apiFootballLeagueId": league_id,
        "apiFootballLeagueName": str(provider_league.get("name") or ""),
        "participants": participants,
        "teamLogos": logos,
        "fixtureCount": len(output_rows),
        "fixtures": output_rows,
    }
    report = {
        "competitionId": result["competitionId"],
        "apiFootballLeagueId": league_id,
        "apiFootballLeagueName": result["apiFootballLeagueName"],
        "fixtureCount": len(output_rows),
        "statsReused": stats_reused,
        "statsRequested": stats_requested,
        "statsMissing": stats_missing,
        "phaseStart": from_date,
        "throughDate": through_date,
    }
    return result, report


def validate_output(payload: Mapping[str, Any]) -> None:
    if int(payload.get("schemaVersion") or 0) != 1:
        raise RuntimeError("UEFA current stats schemaVersion must be 1")
    competitions = payload.get("competitions") or []
    expected_ids = {"champions_league", "europa_league", "conference_league"}
    actual_ids = {str(row.get("competitionId")) for row in competitions if isinstance(row, dict)}
    if actual_ids != expected_ids:
        raise RuntimeError(f"UEFA current stats competition mismatch: {actual_ids}")
    for competition in competitions:
        if not isinstance(competition, dict):
            raise RuntimeError("Invalid competition row")
        start = str(competition.get("phaseStart") or "")
        league_id = int(competition.get("apiFootballLeagueId") or 0)
        if league_id <= 0:
            raise RuntimeError(f"Missing API-Football league id: {competition.get('competitionId')}")
        seen: set[int] = set()
        for row in competition.get("fixtures", []) or []:
            fid = int(row.get("fixtureId") or 0)
            if fid <= 0 or fid in seen:
                raise RuntimeError(f"Duplicate/invalid UEFA fixture id: {fid}")
            seen.add(fid)
            if str(row.get("date") or "") < start:
                raise RuntimeError(f"Pre-season UEFA fixture leaked into current stats: {fid}")
            if str(row.get("status") or "") not in COMPLETED:
                raise RuntimeError(f"Non-completed UEFA fixture leaked into current stats: {fid}")
            source = row.get("sourceLeague") if isinstance(row.get("sourceLeague"), dict) else {}
            if int(source.get("id") or 0) != league_id:
                raise RuntimeError(f"Cross-competition fixture leaked into current stats: {fid}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-requests", type=int, default=180)
    parser.add_argument("--through-date", default=today_utc())
    args = parser.parse_args()

    api_key = os.getenv("API_FOOTBALL_KEY", "").strip()
    if not api_key:
        raise SystemExit("API_FOOTBALL_KEY is required")

    config = load_json(CONFIG, {})
    season = int(config.get("season") or 0)
    display_season = str(config.get("displaySeason") or f"{season}-{season + 1}")
    competitions = config.get("competitions") or []
    if season <= 0 or len(competitions) != 3:
        raise SystemExit("UEFA simulation config must define the current CL/EL/Conference season")

    previous = load_json(OUTPUT, {})
    request_state = {"count": 0}
    output_competitions = []
    reports = []

    for competition in competitions:
        result, report = refresh_competition(
            api_key=api_key,
            competition=competition,
            season=season,
            display_season=display_season,
            previous=previous,
            request_state=request_state,
            max_requests=args.max_requests,
            through_date=args.through_date,
        )
        output_competitions.append(result)
        reports.append(report)

    payload = {
        "schemaVersion": 1,
        "generatedAt": now_utc(),
        "source": "API-Football current UEFA season only",
        "season": season,
        "displaySeason": display_season,
        "throughDate": args.through_date,
        "apiRequestsUsed": request_state["count"],
        "apiRequestCap": int(args.max_requests),
        "competitions": output_competitions,
    }
    validate_output(payload)
    write_json(OUTPUT, payload)

    report = {
        "generatedAt": payload["generatedAt"],
        "season": season,
        "displaySeason": display_season,
        "apiRequestsUsed": request_state["count"],
        "apiRequestCap": int(args.max_requests),
        "competitions": reports,
    }
    write_json(REPORT, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
