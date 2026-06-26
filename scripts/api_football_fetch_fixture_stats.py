#!/usr/bin/env python3
"""Fetch API-Football fixture statistics into a repository cache.

API-Football is used only as a stats enrichment provider. This script does not
fetch odds, does not merge Football-Data CSV files, and does not write app data.
The Android app must consume ready repository artifacts and must not call
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
    return CACHE_ROOT / slug(league.get("country")) / slug(league.get("display_name")) / str(league.get("season")) / "fixture_stats.json"


def api_get(api_key: str, endpoint: str, params: Dict[str, Any], request_state: Dict[str, int], max_requests: int) -> Dict[str, Any]:
    if request_state["count"] >= max_requests:
        raise RequestLimitReached
    query = urlencode({key: value for key, value in params.items() if value is not None and value != ""})
    request = Request(
        f"{BASE_URL}/{endpoint}?{query}" if query else f"{BASE_URL}/{endpoint}",
        headers={
            "x-apisports-key": api_key,
            "Accept": "application/json",
            "User-Agent": "StatMaker-Data fixture stats cache",
        },
        method="GET",
    )
    request_state["count"] += 1
    with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        payload = json.loads(response.read().decode("utf-8"))
    time.sleep(REQUEST_DELAY_SECONDS)
    return payload


class RequestLimitReached(RuntimeError):
    pass


def response_items(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    response = payload.get("response") if isinstance(payload, dict) else []
    return response if isinstance(response, list) else []


def fixture_identity(fixture: Dict[str, Any]) -> Optional[int]:
    fixture_id = ((fixture.get("fixture") or {}).get("id")) if isinstance(fixture, dict) else None
    return int(fixture_id) if fixture_id is not None else None


def fixture_summary(fixture: Dict[str, Any]) -> Dict[str, Any]:
    fixture_info = fixture.get("fixture") or {}
    teams = fixture.get("teams") or {}
    home = teams.get("home") or {}
    away = teams.get("away") or {}
    status = fixture_info.get("status") or {}
    return {
        "fixture_id": fixture_info.get("id"),
        "date": fixture_info.get("date"),
        "home_team": home.get("name"),
        "away_team": away.get("name"),
        "home_team_id": home.get("id"),
        "away_team_id": away.get("id"),
        "status": status.get("short") or status.get("long"),
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


def cache_payload(league: Dict[str, Any], fixtures: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    ordered = sorted(fixtures, key=lambda item: (str(item.get("date") or ""), int(item.get("fixture_id") or 0)))
    return {
        "provider": "api-football",
        "league_id": league.get("api_football_league_id"),
        "league_name": league.get("display_name"),
        "country": league.get("country"),
        "season": str(league.get("season")),
        "generated_at": now_utc(),
        "fixtures": ordered,
    }


def league_filter(leagues: List[Dict[str, Any]], priority_group: Optional[str], league_id: Optional[int]) -> List[Dict[str, Any]]:
    selected = [league for league in leagues if bool(league.get("enabled"))]
    if priority_group:
        selected = [league for league in selected if str(league.get("priority_group") or "") == priority_group]
    if league_id is not None:
        selected = [league for league in selected if int(league.get("api_football_league_id") or -1) == league_id]
    return selected


def report_row(league: Dict[str, Any], cache_path: Path, completed: int, cached: int, fetched: int, missing: int, requests_before: int, requests_after: int, notes: str) -> Dict[str, Any]:
    return {
        "country": league.get("country"),
        "league": league.get("display_name"),
        "season": str(league.get("season")),
        "api_football_league_id": league.get("api_football_league_id"),
        "completed_fixtures_found": completed,
        "already_cached": cached,
        "newly_fetched": fetched,
        "missing_stats_responses": missing,
        "requests_used": requests_after - requests_before,
        "cache_path": str(cache_path.relative_to(ROOT)).replace("\\", "/"),
        "notes": notes,
    }


def fetch_league(api_key: str, league: Dict[str, Any], request_state: Dict[str, int], max_requests: int) -> Dict[str, Any]:
    cache_path = cache_path_for(league)
    existing_cache = load_json(cache_path, {})
    existing_by_id = cached_fixture_map(existing_cache)
    requests_before = request_state["count"]
    completed_count = 0
    already_cached = 0
    newly_fetched = 0
    missing_stats = 0
    notes: List[str] = []

    try:
        fixtures_payload = api_get(
            api_key,
            "fixtures",
            {
                "league": league.get("api_football_league_id"),
                "season": league.get("season"),
                "status": "FT",
            },
            request_state,
            max_requests,
        )
        fixtures = response_items(fixtures_payload)
        completed_count = len(fixtures)
    except RequestLimitReached:
        notes.append("request cap reached before fixtures request")
        write_json(cache_path, cache_payload(league, existing_by_id.values()))
        return report_row(league, cache_path, completed_count, already_cached, newly_fetched, missing_stats, requests_before, request_state["count"], "; ".join(notes))
    except (HTTPError, URLError) as exc:
        notes.append(f"fixtures request failed: {exc}")
        write_json(cache_path, cache_payload(league, existing_by_id.values()))
        return report_row(league, cache_path, completed_count, already_cached, newly_fetched, missing_stats, requests_before, request_state["count"], "; ".join(notes))

    for fixture in fixtures:
        fixture_id = fixture_identity(fixture)
        if fixture_id is None:
            continue
        if fixture_id in existing_by_id:
            already_cached += 1
            continue
        if request_state["count"] >= max_requests:
            notes.append("request cap reached before all fixture statistics were fetched")
            break
        try:
            stats_payload = api_get(api_key, "fixtures/statistics", {"fixture": fixture_id}, request_state, max_requests)
        except RequestLimitReached:
            notes.append("request cap reached before statistics request")
            break
        except (HTTPError, URLError) as exc:
            missing_stats += 1
            notes.append(f"statistics request failed for fixture {fixture_id}: {exc}")
            continue

        raw_statistics = response_items(stats_payload)
        if not raw_statistics:
            missing_stats += 1
        summary = fixture_summary(fixture)
        existing_by_id[fixture_id] = {
            "fixture_id": summary.get("fixture_id"),
            "date": summary.get("date"),
            "home_team": summary.get("home_team"),
            "away_team": summary.get("away_team"),
            "status": summary.get("status"),
            "raw_statistics": raw_statistics,
            "normalized_stats": normalize_statistics(raw_statistics, fixture),
        }
        newly_fetched += 1

    write_json(cache_path, cache_payload(league, existing_by_id.values()))
    if not notes:
        notes.append("ok")
    return report_row(league, cache_path, completed_count, already_cached, newly_fetched, missing_stats, requests_before, request_state["count"], "; ".join(notes))


def write_reports(rows: List[Dict[str, Any]], request_count: int, max_requests: int) -> None:
    payload = {
        "generated_at": now_utc(),
        "provider": "api-football",
        "purpose": "fixture statistics enrichment cache fetch",
        "request_count": request_count,
        "max_requests": max_requests,
        "reports": rows,
    }
    write_json(REPORT_JSON, payload)

    fieldnames = [
        "country", "league", "season", "api_football_league_id",
        "completed_fixtures_found", "already_cached", "newly_fetched",
        "missing_stats_responses", "requests_used", "cache_path", "notes",
    ]
    REPORT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with REPORT_CSV.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in fieldnames})

    headers = [
        "Country", "League", "Season", "API league ID", "Completed fixtures found",
        "Already cached", "Newly fetched", "Missing stats responses", "Requests used",
        "Cache path", "Notes",
    ]
    lines = [
        "# API-Football fixture statistics fetch",
        "",
        f"Generated at: `{payload['generated_at']}`",
        f"Requests used: `{request_count}` / `{max_requests}`",
        "",
        "API-Football is used only for fixture statistics enrichment. Odds remain with Odds-API.io.",
        "",
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in rows:
        values = [
            row.get("country"), row.get("league"), row.get("season"), row.get("api_football_league_id"),
            row.get("completed_fixtures_found"), row.get("already_cached"), row.get("newly_fetched"),
            row.get("missing_stats_responses"), row.get("requests_used"), row.get("cache_path"), row.get("notes"),
        ]
        lines.append("| " + " | ".join(str(value if value is not None else "").replace("|", "\\|") for value in values) + " |")
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch API-Football fixture statistics cache")
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
            f"fixture-stats league={league.get('display_name')} country={league.get('country')} "
            f"season={league.get('season')} request_count={request_state['count']}"
        )
        row = fetch_league(api_key, league, request_state, args.max_requests)
        rows.append(row)
        print(
            f"fixture-stats league={league.get('display_name')} season={league.get('season')} "
            f"fixtures={row['completed_fixtures_found']} fetched={row['newly_fetched']} "
            f"cached={row['already_cached']} requests={request_state['count']}"
        )

    write_reports(rows, request_state["count"], args.max_requests)
    print(f"fixture-stats reports written leagues={len(rows)} requests={request_state['count']} max_requests={args.max_requests}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())