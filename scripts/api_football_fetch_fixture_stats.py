#!/usr/bin/env python3
"""Fetch API-Football fixture statistics and score metadata into repository cache.

API-Football is the canonical domestic historical/stat source for StatMaker-Data.
The Android app consumes generated repository JSON artifacts and must not call
API-Football directly.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "api_football_enrichment_leagues.json"
CACHE_ROOT = ROOT / "data" / "api_football" / "fixture_stats"
REPORT_JSON = ROOT / "reports" / "api_football_fixture_stats_fetch.json"
REPORT_CSV = ROOT / "reports" / "api_football_fixture_stats_fetch.csv"
REPORT_MD = ROOT / "reports" / "api_football_fixture_stats_fetch.md"

BASE_URL = "https://v3.football.api-sports.io"
DEFAULT_MAX_REQUESTS = 85
REQUEST_DELAY_SECONDS = 0.75
TIMEOUT_SECONDS = 30

COMPLETED_STATUS_SHORT_CODES = {"FT", "AET", "PEN"}
FIXTURE_LAST_FALLBACK_COUNT = 50

NORMALIZED_FIELDS = [
    "HS", "AS",
    "HST", "AST",
    "HC", "AC",
    "HF", "AF",
    "HY", "AY",
    "HR", "AR",
    "HPossession", "APossession",
    "HSaves", "ASaves",
    "HPasses", "APasses",
    "HPassesAccurate", "APassesAccurate",
    "HxG", "AxG",
]

STAT_FIELD_MAP = {
    "shots on goal": ("HST", "AST"),
    "shots on target": ("HST", "AST"),
    "total shots": ("HS", "AS"),
    "corner kicks": ("HC", "AC"),
    "corners": ("HC", "AC"),
    "fouls": ("HF", "AF"),
    "yellow cards": ("HY", "AY"),
    "red cards": ("HR", "AR"),
    "ball possession": ("HPossession", "APossession"),
    "goalkeeper saves": ("HSaves", "ASaves"),
    "total passes": ("HPasses", "APasses"),
    "passes accurate": ("HPassesAccurate", "APassesAccurate"),
    "expected goals": ("HxG", "AxG"),
    "expected_goals": ("HxG", "AxG"),
    "xg": ("HxG", "AxG"),
}


class RequestLimitReached(RuntimeError):
    pass


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def slug(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text or "unknown"


def normalize_key(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9_]+", " ", text.lower()).strip()
    return re.sub(r"\s+", " ", text)


def parse_number(value: Any) -> Optional[float | int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("%"):
        text = text[:-1].strip()
    text = text.replace(",", ".")
    try:
        number = float(text)
    except ValueError:
        return None
    return int(number) if number.is_integer() else number


def parse_int(value: Any) -> Optional[int]:
    number = parse_number(value)
    if isinstance(number, int):
        return number
    if isinstance(number, float) and number.is_integer():
        return int(number)
    return None


def parse_season(value: Any) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_config() -> Dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Missing config: {CONFIG_PATH.relative_to(ROOT)}")
    return load_json(CONFIG_PATH, {})


def cache_path_for(league: Dict[str, Any]) -> Path:
    return (
        CACHE_ROOT
        / slug(league.get("country"))
        / slug(league.get("display_name"))
        / str(league.get("season"))
        / "fixture_stats.json"
    )


def api_get(
    api_key: str,
    endpoint: str,
    params: Dict[str, Any],
    request_state: Dict[str, int],
    max_requests: int,
) -> Dict[str, Any]:
    if request_state["count"] >= max_requests:
        raise RequestLimitReached

    query = urlencode({key: value for key, value in params.items() if value is not None and value != ""})
    url = f"{BASE_URL}/{endpoint}?{query}" if query else f"{BASE_URL}/{endpoint}"
    request = Request(
        url,
        headers={
            "x-apisports-key": api_key,
            "Accept": "application/json",
            "User-Agent": "StatMaker-Data API-Football cache",
        },
        method="GET",
    )

    request_state["count"] += 1

    with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        payload = json.loads(response.read().decode("utf-8"))

    time.sleep(REQUEST_DELAY_SECONDS)
    return payload


def response_items(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    response = payload.get("response") if isinstance(payload, dict) else []
    return response if isinstance(response, list) else []


def api_errors(payload: Dict[str, Any]) -> str:
    errors = payload.get("errors") if isinstance(payload, dict) else None
    if not errors:
        return ""
    if isinstance(errors, dict):
        return "; ".join(f"{key}: {value}" for key, value in errors.items())
    if isinstance(errors, list):
        return "; ".join(str(item) for item in errors)
    return str(errors)


def score_side(block: Any, side: str) -> Optional[int]:
    return parse_int(block.get(side)) if isinstance(block, dict) else None


def score_pair(block: Any) -> Tuple[Optional[int], Optional[int]]:
    if not isinstance(block, dict):
        return None, None
    return score_side(block, "home"), score_side(block, "away")


def fixture_score_summary(fixture: Dict[str, Any]) -> Dict[str, Any]:
    goals = fixture.get("goals") or {}
    score = fixture.get("score") or {}

    goals_home, goals_away = score_pair(goals)
    ht_home, ht_away = score_pair(score.get("halftime"))
    ft_home, ft_away = score_pair(score.get("fulltime"))
    et_home, et_away = score_pair(score.get("extratime"))
    pen_home, pen_away = score_pair(score.get("penalty"))

    final_home = ft_home if ft_home is not None else goals_home
    final_away = ft_away if ft_away is not None else goals_away

    return {
        "home_goals": final_home,
        "away_goals": final_away,
        "home_score": final_home,
        "away_score": final_away,
        "fthg": final_home,
        "ftag": final_away,
        "hthg": ht_home,
        "htag": ht_away,
        "goals": {"home": goals_home, "away": goals_away},
        "score": {
            "halftime": {"home": ht_home, "away": ht_away},
            "fulltime": {"home": ft_home, "away": ft_away},
            "extratime": {"home": et_home, "away": et_away},
            "penalty": {"home": pen_home, "away": pen_away},
        },
    }


def fixture_identity(fixture: Dict[str, Any]) -> Optional[int]:
    fixture_id = ((fixture.get("fixture") or {}).get("id")) if isinstance(fixture, dict) else None
    return int(fixture_id) if fixture_id is not None else None


def fixture_status_short(fixture: Dict[str, Any]) -> str:
    status = ((fixture.get("fixture") or {}).get("status") or {}) if isinstance(fixture, dict) else {}
    return str(status.get("short") or "").upper()


def is_completed_fixture(fixture: Dict[str, Any]) -> bool:
    return fixture_status_short(fixture) in COMPLETED_STATUS_SHORT_CODES


def fixture_summary(fixture: Dict[str, Any]) -> Dict[str, Any]:
    fixture_info = fixture.get("fixture") or {}
    teams = fixture.get("teams") or {}
    home = teams.get("home") or {}
    away = teams.get("away") or {}
    status = fixture_info.get("status") or {}

    summary = {
        "fixture_id": fixture_info.get("id"),
        "date": fixture_info.get("date"),
        "home_team": home.get("name"),
        "away_team": away.get("name"),
        "home_team_id": home.get("id"),
        "away_team_id": away.get("id"),
        "status": status.get("short") or status.get("long"),
    }
    summary.update(fixture_score_summary(fixture))
    return summary


def fixture_source_league(fixture: Dict[str, Any]) -> Dict[str, Any]:
    league = fixture.get("league") or {}
    return {
        "id": league.get("id"),
        "name": league.get("name"),
        "country": league.get("country"),
        "season": league.get("season"),
        "round": league.get("round"),
    }


def empty_normalized_stats() -> Dict[str, Any]:
    return {field: None for field in NORMALIZED_FIELDS}


def normalize_statistics(raw_statistics: List[Dict[str, Any]], fixture: Dict[str, Any]) -> Dict[str, Any]:
    stats = empty_normalized_stats()
    summary = fixture_summary(fixture)

    home_id = summary.get("home_team_id")
    away_id = summary.get("away_team_id")
    home_name = str(summary.get("home_team") or "").strip().lower()
    away_name = str(summary.get("away_team") or "").strip().lower()

    for team_block in raw_statistics:
        team = team_block.get("team") or {}
        team_id = team.get("id")
        team_name = str(team.get("name") or "").strip().lower()

        if team_id == home_id or (team_name and team_name == home_name):
            side = "home"
        elif team_id == away_id or (team_name and team_name == away_name):
            side = "away"
        else:
            continue

        for stat in team_block.get("statistics") or []:
            stat_type = normalize_key(stat.get("type"))
            field_pair = STAT_FIELD_MAP.get(stat_type)
            if not field_pair:
                continue
            field = field_pair[0] if side == "home" else field_pair[1]
            stats[field] = parse_number(stat.get("value"))

    return stats


def cached_fixture_map(cache: Dict[str, Any]) -> Dict[int, Dict[str, Any]]:
    fixtures = cache.get("fixtures") if isinstance(cache, dict) else []
    result: Dict[int, Dict[str, Any]] = {}
    for item in fixtures if isinstance(fixtures, list) else []:
        fixture_id = item.get("fixture_id")
        if fixture_id is not None:
            result[int(fixture_id)] = item
    return result


def cache_identity_mismatch_reason(
    league: Dict[str, Any],
    cache: Dict[str, Any],
) -> Optional[str]:
    """Return why an existing cache belongs to a different provider scope."""
    if not isinstance(cache, dict) or not cache:
        return None
    expected_id = league.get("api_football_league_id")
    actual_id = cache.get("league_id")
    if expected_id is not None and actual_id is not None:
        try:
            if int(expected_id) != int(actual_id):
                return f"provider league id changed: cache={actual_id} configured={expected_id}"
        except (TypeError, ValueError):
            return f"invalid provider league id identity: cache={actual_id!r} configured={expected_id!r}"
    expected_season = str(league.get("season") or "").strip()
    actual_season = str(cache.get("season") or "").strip()
    if expected_season and actual_season and expected_season != actual_season:
        return f"provider season changed: cache={actual_season} configured={expected_season}"
    return None


def roster_from_fixtures(fixtures: Iterable[Dict[str, Any]]) -> List[str]:
    names: set[str] = set()
    for fixture in fixtures:
        if not isinstance(fixture, dict):
            continue
        teams = fixture.get("teams") or {}
        if not isinstance(teams, dict):
            continue
        for side in ("home", "away"):
            team = teams.get(side) or {}
            if not isinstance(team, dict):
                continue
            name = str(team.get("name") or "").strip()
            if name:
                names.add(name)
    return sorted(names, key=str.casefold)


def cache_payload(
    league: Dict[str, Any],
    fixtures: Iterable[Dict[str, Any]],
    roster: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    ordered = sorted(fixtures, key=lambda item: (str(item.get("date") or ""), int(item.get("fixture_id") or 0)))
    return {
        "provider": "api-football",
        "purpose": "api_only_domestic_history_and_fixture_statistics",
        "league_id": league.get("api_football_league_id"),
        "league_name": league.get("display_name"),
        "country": league.get("country"),
        "season": str(league.get("season")),
        "generated_at": now_utc(),
        "roster": sorted(
            {str(name).strip() for name in (roster or []) if str(name).strip()},
            key=str.casefold,
        ),
        "fixtures": ordered,
    }


def league_filter(
    leagues: List[Dict[str, Any]],
    priority_group: Optional[str],
    league_id: Optional[int],
) -> List[Dict[str, Any]]:
    selected = [league for league in leagues if bool(league.get("enabled"))]
    if priority_group:
        selected = [league for league in selected if str(league.get("priority_group") or "") == priority_group]
    if league_id is not None:
        selected = [
            league for league in selected
            if int(league.get("api_football_league_id") or -1) == league_id
        ]
    return selected


def fixture_query_candidates(league: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    league_id = league.get("api_football_league_id")
    requested_season = parse_season(league.get("season"))

    candidates: List[Tuple[str, Dict[str, Any]]] = []
    seen: set[Tuple[Tuple[str, Any], ...]] = set()

    def add(label: str, params: Dict[str, Any]) -> None:
        clean_params = {key: value for key, value in params.items() if value is not None and value != ""}
        key = tuple(sorted(clean_params.items()))
        if key not in seen:
            candidates.append((label, clean_params))
            seen.add(key)

    if requested_season is not None:
        add(f"league+season:{requested_season}", {"league": league_id, "season": requested_season})
        add(f"league+season:{requested_season - 1}", {"league": league_id, "season": requested_season - 1})
        add(f"league+season:{requested_season + 1}", {"league": league_id, "season": requested_season + 1})
    else:
        add("league+season:raw", {"league": league_id, "season": league.get("season")})

    add(f"league+last:{FIXTURE_LAST_FALLBACK_COUNT}", {"league": league_id, "last": FIXTURE_LAST_FALLBACK_COUNT})
    return candidates


def fetch_fixtures_with_fallback(
    api_key: str,
    league: Dict[str, Any],
    request_state: Dict[str, int],
    max_requests: int,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], str, List[str]]:
    attempt_notes: List[str] = []

    for query_label, params in fixture_query_candidates(league):
        if request_state["count"] >= max_requests:
            raise RequestLimitReached

        try:
            payload = api_get(api_key, "fixtures", params, request_state, max_requests)
        except (HTTPError, URLError) as exc:
            attempt_notes.append(f"{query_label} failed: {exc}")
            continue

        errors_text = api_errors(payload)
        all_fixtures = response_items(payload)
        completed_fixtures = [fixture for fixture in all_fixtures if is_completed_fixture(fixture)]

        if errors_text:
            attempt_notes.append(f"{query_label} errors={errors_text}")
        attempt_notes.append(f"{query_label} returned={len(all_fixtures)} completed={len(completed_fixtures)}")

        if all_fixtures:
            return all_fixtures, completed_fixtures, query_label, attempt_notes

    return [], [], "none", attempt_notes


def has_cached_stats(item: Dict[str, Any]) -> bool:
    return isinstance(item.get("normalized_stats"), dict) and "raw_statistics" in item


def merge_cached_fixture(
    existing: Dict[str, Any],
    fixture: Dict[str, Any],
    fixture_query_used: str,
) -> Dict[str, Any]:
    summary = fixture_summary(fixture)
    merged = dict(existing)
    merged.update({
        "fixture_id": summary.get("fixture_id"),
        "date": summary.get("date"),
        "home_team": summary.get("home_team"),
        "away_team": summary.get("away_team"),
        "status": summary.get("status"),
        "fixture_query_used": fixture_query_used,
        "source_league": fixture_source_league(fixture),
    })
    for key in (
        "home_goals", "away_goals", "home_score", "away_score",
        "fthg", "ftag", "hthg", "htag", "goals", "score",
    ):
        merged[key] = summary.get(key)
    return merged


def report_row(
    league: Dict[str, Any],
    cache_path: Path,
    completed: int,
    cached: int,
    fetched: int,
    missing: int,
    refreshed: int,
    score_missing: int,
    requests_before: int,
    requests_after: int,
    fixture_query_used: str,
    fixtures_returned: int,
    notes: str,
) -> Dict[str, Any]:
    return {
        "country": league.get("country"),
        "league": league.get("display_name"),
        "season": str(league.get("season")),
        "api_football_league_id": league.get("api_football_league_id"),
        "completed_fixtures_found": completed,
        "already_cached": cached,
        "newly_fetched": fetched,
        "metadata_refreshed": refreshed,
        "missing_scores": score_missing,
        "missing_stats_responses": missing,
        "requests_used": requests_after - requests_before,
        "fixture_query_used": fixture_query_used,
        "fixtures_returned": fixtures_returned,
        "cache_path": str(cache_path.relative_to(ROOT)).replace("\\", "/"),
        "notes": notes,
    }


def fetch_league(
    api_key: str,
    league: Dict[str, Any],
    request_state: Dict[str, int],
    max_requests: int,
) -> Dict[str, Any]:
    cache_path = cache_path_for(league)
    existing_cache = load_json(cache_path, {})
    identity_note = cache_identity_mismatch_reason(league, existing_cache)
    if identity_note:
        # The app-facing cache path survives provider-id corrections. Never merge
        # fixtures from the previous competition into the corrected provider scope.
        existing_cache = {}
    existing_by_id = cached_fixture_map(existing_cache)
    existing_roster = [
        str(name).strip()
        for name in (existing_cache.get("roster", []) if isinstance(existing_cache, dict) else [])
        if str(name).strip()
    ]
    roster = existing_roster

    requests_before = request_state["count"]
    completed_count = 0
    already_cached = 0
    newly_fetched = 0
    metadata_refreshed = 0
    missing_scores = 0
    missing_stats = 0
    fixtures_returned = 0
    fixture_query_used = "none"
    notes: List[str] = []
    if identity_note:
        notes.append(f"stale cache discarded: {identity_note}")

    try:
        all_fixtures, fixtures, fixture_query_used, query_notes = fetch_fixtures_with_fallback(
            api_key, league, request_state, max_requests
        )
        fixtures_returned = len(all_fixtures)
        completed_count = len(fixtures)
        notes.extend(query_notes)

        requested_season = parse_season(league.get("season"))
        exact_query = f"league+season:{requested_season}" if requested_season is not None else ""
        if all_fixtures and fixture_query_used == exact_query:
            discovered_roster = roster_from_fixtures(all_fixtures)
            if discovered_roster:
                roster = discovered_roster

        if not all_fixtures:
            notes.append("no fixtures returned after fallback queries")
        elif not fixtures:
            statuses = sorted({fixture_status_short(fixture) or "UNKNOWN" for fixture in all_fixtures})
            notes.append(
                f"fixtures returned={len(all_fixtures)} but no completed fixtures with status FT/AET/PEN; "
                f"statuses={','.join(statuses)}"
            )
    except RequestLimitReached:
        notes.append("request cap reached before fixtures request")
        write_json(cache_path, cache_payload(league, existing_by_id.values(), roster))
        return report_row(
            league, cache_path, completed_count, already_cached, newly_fetched,
            missing_stats, metadata_refreshed, missing_scores, requests_before,
            request_state["count"], fixture_query_used, fixtures_returned, "; ".join(notes)
        )

    for fixture in fixtures:
        fixture_id = fixture_identity(fixture)
        if fixture_id is None:
            continue

        existing = existing_by_id.get(fixture_id, {})
        merged = merge_cached_fixture(existing, fixture, fixture_query_used)
        metadata_refreshed += 1

        if merged.get("home_goals") is None or merged.get("away_goals") is None:
            missing_scores += 1

        if has_cached_stats(merged):
            already_cached += 1
            existing_by_id[fixture_id] = merged
            continue

        if request_state["count"] >= max_requests:
            notes.append("request cap reached before all fixture statistics were fetched")
            existing_by_id[fixture_id] = merged
            break

        try:
            stats_payload = api_get(api_key, "fixtures/statistics", {"fixture": fixture_id}, request_state, max_requests)
        except RequestLimitReached:
            notes.append("request cap reached before statistics request")
            existing_by_id[fixture_id] = merged
            break
        except (HTTPError, URLError) as exc:
            missing_stats += 1
            notes.append(f"statistics request failed for fixture {fixture_id}: {exc}")
            existing_by_id[fixture_id] = merged
            continue

        raw_statistics = response_items(stats_payload)
        if not raw_statistics:
            missing_stats += 1

        merged["raw_statistics"] = raw_statistics
        merged["normalized_stats"] = normalize_statistics(raw_statistics, fixture)
        existing_by_id[fixture_id] = merged
        newly_fetched += 1

    write_json(cache_path, cache_payload(league, existing_by_id.values(), roster))

    if not notes:
        notes.append("ok")

    return report_row(
        league, cache_path, completed_count, already_cached, newly_fetched,
        missing_stats, metadata_refreshed, missing_scores, requests_before,
        request_state["count"], fixture_query_used, fixtures_returned, "; ".join(notes)
    )


def write_reports(rows: List[Dict[str, Any]], request_count: int, max_requests: int) -> None:
    payload = {
        "generated_at": now_utc(),
        "provider": "api-football",
        "purpose": "api-only domestic fixture history and statistics cache fetch",
        "request_count": request_count,
        "max_requests": max_requests,
        "reports": rows,
    }
    write_json(REPORT_JSON, payload)

    fieldnames = [
        "country", "league", "season", "api_football_league_id",
        "completed_fixtures_found", "already_cached", "newly_fetched",
        "metadata_refreshed", "missing_scores", "missing_stats_responses",
        "requests_used", "fixture_query_used", "fixtures_returned", "cache_path", "notes",
    ]

    REPORT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with REPORT_CSV.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in fieldnames})

    headers = [
        "Country", "League", "Season", "API league ID", "Completed fixtures found",
        "Already cached", "Newly fetched", "Metadata refreshed", "Missing scores",
        "Missing stats responses", "Requests used", "Fixture query used",
        "Fixtures returned", "Cache path", "Notes",
    ]

    lines = [
        "# API-Football domestic fixture history/statistics fetch",
        "",
        f"Generated at: `{payload['generated_at']}`",
        f"Requests used: `{request_count}` / `{max_requests}`",
        "",
        "API-Football is the active domestic historical/stat source. Football-Data CSV is inactive archive/fallback.",
        "",
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]

    for row in rows:
        values = [
            row.get("country"), row.get("league"), row.get("season"),
            row.get("api_football_league_id"), row.get("completed_fixtures_found"),
            row.get("already_cached"), row.get("newly_fetched"),
            row.get("metadata_refreshed"), row.get("missing_scores"),
            row.get("missing_stats_responses"), row.get("requests_used"),
            row.get("fixture_query_used"), row.get("fixtures_returned"),
            row.get("cache_path"), row.get("notes"),
        ]
        lines.append("| " + " | ".join(str(value if value is not None else "").replace("|", "\\|") for value in values) + " |")

    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch API-Football fixture history/statistics cache")
    parser.add_argument("--max-requests", type=int, default=DEFAULT_MAX_REQUESTS, help="Safe request cap for this run")
    parser.add_argument("--priority-group", default=None, help="Optional priority_group filter from config")
    parser.add_argument("--league-id", type=int, default=None, help="Optional API-Football league id filter")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.max_requests < 1:
        print("ERROR: --max-requests must be at least 1", file=sys.stderr)
        return 2

    api_key = os.environ.get("API_FOOTBALL_KEY", "").strip()
    if not api_key:
        print("ERROR: API_FOOTBALL_KEY environment variable is required.", file=sys.stderr)
        return 2

    config = load_config()
    leagues = league_filter(config.get("leagues", []), args.priority_group, args.league_id)

    request_state = {"count": 0}
    rows: List[Dict[str, Any]] = []

    for league in leagues:
        if request_state["count"] >= args.max_requests:
            break

        print(
            f"fixture-history league={league.get('display_name')} country={league.get('country')} "
            f"season={league.get('season')} request_count={request_state['count']}"
        )

        row = fetch_league(api_key, league, request_state, args.max_requests)
        rows.append(row)

        print(
            f"fixture-history league={league.get('display_name')} season={league.get('season')} "
            f"fixtures={row['completed_fixtures_found']} returned={row['fixtures_returned']} "
            f"fetched={row['newly_fetched']} cached={row['already_cached']} "
            f"metadata={row['metadata_refreshed']} missing_scores={row['missing_scores']} "
            f"query={row['fixture_query_used']} requests={request_state['count']}"
        )

    write_reports(rows, request_state["count"], args.max_requests)

    print(
        f"fixture-history reports written leagues={len(rows)} "
        f"requests={request_state['count']} max_requests={args.max_requests}"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
