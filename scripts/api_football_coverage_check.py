#!/usr/bin/env python3
"""Check API-Football league coverage for limited domestic Football-Data leagues.

This is a coverage proof only. It discovers API-Football league records and
season coverage metadata. It does not fetch fixtures, fixture statistics, CSV
files, odds, or app data. The Android app must continue to consume ready
repository files and must never call API-Football directly.
"""

from __future__ import annotations

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
CONFIG_PATH = ROOT / "config" / "api_football_yellow_leagues.json"
JSON_OUT = ROOT / "reports" / "api_football_yellow_coverage.json"
CSV_OUT = ROOT / "reports" / "api_football_yellow_coverage.csv"
MD_OUT = ROOT / "reports" / "api_football_yellow_coverage.md"
BASE_URL = "https://v3.football.api-sports.io/leagues"
REQUEST_DELAY_SECONDS = 0.75
TIMEOUT_SECONDS = 30


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_config() -> Dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Missing config: {CONFIG_PATH.relative_to(ROOT)}")
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))


def api_season(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) >= 4 and text[:4].isdigit():
        return text[:4]
    return text


def normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    return re.sub(r"\s+", " ", text)


def bool_value(value: Any) -> bool:
    return bool(value) if value is not None else False


def yes_no_unknown(value: Optional[bool]) -> str:
    if value is None:
        return "Unknown"
    return "Yes" if value else "No"


def safe_get(mapping: Dict[str, Any], *keys: str) -> Any:
    current: Any = mapping
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def request_leagues(api_key: str, params: Dict[str, str]) -> Dict[str, Any]:
    query = urlencode({key: value for key, value in params.items() if value})
    request = Request(
        f"{BASE_URL}?{query}" if query else BASE_URL,
        headers={
            "x-apisports-key": api_key,
            "Accept": "application/json",
            "User-Agent": "StatMaker-Data coverage check",
        },
        method="GET",
    )
    with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def response_items(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    response = payload.get("response") if isinstance(payload, dict) else []
    return response if isinstance(response, list) else []


def call_api(api_key: str, params: Dict[str, str], label: str, request_counter: Dict[str, int]) -> List[Dict[str, Any]]:
    request_counter["count"] += 1
    try:
        payload = request_leagues(api_key, params)
    except HTTPError as exc:
        print(f"coverage discovery={label} status=http-{exc.code} count=0")
        raise
    except URLError as exc:
        print(f"coverage discovery={label} status=url-error count=0")
        raise RuntimeError(f"API-Football request failed for {label}: {exc.reason}") from exc
    items = response_items(payload)
    print(f"coverage discovery={label} status=ok count={len(items)}")
    time.sleep(REQUEST_DELAY_SECONDS)
    return items


def season_entries(api_item: Dict[str, Any]) -> List[Dict[str, Any]]:
    seasons = api_item.get("seasons")
    return seasons if isinstance(seasons, list) else []


def available_seasons(api_item: Dict[str, Any]) -> List[str]:
    years = []
    for entry in season_entries(api_item):
        year = entry.get("year")
        if year is not None:
            years.append(str(year))
    return sorted(set(years), key=lambda value: int(value) if value.isdigit() else -1, reverse=True)


def selected_season_entry(api_item: Dict[str, Any], requested_season: str) -> Tuple[Optional[str], Optional[Dict[str, Any]], List[str]]:
    seasons = season_entries(api_item)
    available = available_seasons(api_item)
    requested_entry = next((entry for entry in seasons if str(entry.get("year")) == requested_season), None)
    if requested_entry is not None:
        return requested_season, requested_entry, available
    if not seasons:
        return None, None, available
    sorted_entries = sorted(
        seasons,
        key=lambda entry: int(str(entry.get("year"))) if str(entry.get("year", "")).isdigit() else -1,
        reverse=True,
    )
    selected = sorted_entries[0]
    selected_year = selected.get("year")
    return str(selected_year) if selected_year is not None else None, selected, available


def fixture_coverage(coverage: Dict[str, Any]) -> Dict[str, bool]:
    fixtures = coverage.get("fixtures") if isinstance(coverage, dict) else {}
    fixtures = fixtures if isinstance(fixtures, dict) else {}
    return {
        "events": bool_value(fixtures.get("events")),
        "lineups": bool_value(fixtures.get("lineups")),
        "statistics": bool_value(fixtures.get("statistics", fixtures.get("statistics_fixtures"))),
        "players_statistics": bool_value(fixtures.get("players_statistics", fixtures.get("statistics_players"))),
        "standings": bool_value(coverage.get("standings")) if isinstance(coverage, dict) else False,
        "odds": bool_value(coverage.get("odds")) if isinstance(coverage, dict) else False,
    }


def candidate_names(config_item: Dict[str, Any]) -> List[str]:
    names = []
    display_name = str(config_item.get("display_name") or "").strip()
    if display_name:
        names.append(display_name)
    configured = config_item.get("candidate_league_names")
    if isinstance(configured, list):
        names.extend(str(name).strip() for name in configured if str(name).strip())
    deduped = []
    seen = set()
    for name in names:
        key = normalize_text(name)
        if key and key not in seen:
            seen.add(key)
            deduped.append(name)
    return deduped


def dedupe_leagues(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    deduped: List[Dict[str, Any]] = []
    seen = set()
    for item in items:
        league_id = safe_get(item, "league", "id")
        key = league_id if league_id is not None else (safe_get(item, "league", "name"), safe_get(item, "country", "name"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def country_matches(api_item: Dict[str, Any], target_country: str) -> bool:
    return normalize_text(safe_get(api_item, "country", "name")) == normalize_text(target_country)


def league_name_matches(api_item: Dict[str, Any], names: List[str]) -> bool:
    league_name = normalize_text(safe_get(api_item, "league", "name"))
    if not league_name:
        return False
    for name in names:
        candidate = normalize_text(name)
        if candidate and (candidate == league_name or candidate in league_name or league_name in candidate):
            return True
    return False


def discover_leagues(api_key: str, config_item: Dict[str, Any], request_counter: Dict[str, int]) -> Tuple[List[Dict[str, Any]], str]:
    country = str(config_item.get("country") or "").strip()
    names = candidate_names(config_item)
    country_results = call_api(api_key, {"country": country}, f"country:{country}", request_counter)
    country_matches_by_name = [item for item in country_results if league_name_matches(item, names)]
    if country_matches_by_name:
        return dedupe_leagues(country_matches_by_name), "country"
    if country_results:
        return dedupe_leagues(country_results), "country"

    fallback_items: List[Dict[str, Any]] = []
    for name in names:
        items = call_api(api_key, {"name": name}, f"name:{name}", request_counter)
        matched = [item for item in items if country_matches(item, country)]
        fallback_items.extend(matched)
    return dedupe_leagues(fallback_items), "name-fallback"


def season_coverage_rows(api_item: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = []
    for entry in season_entries(api_item):
        year = entry.get("year")
        flags = fixture_coverage(entry.get("coverage", {}))
        rows.append({
            "season": str(year) if year is not None else None,
            "coverage_fixtures_events": flags["events"],
            "coverage_fixtures_lineups": flags["lineups"],
            "coverage_fixtures_statistics": flags["statistics"],
            "coverage_fixtures_players_statistics": flags["players_statistics"],
            "coverage_standings": flags["standings"],
            "coverage_odds": flags["odds"],
        })
    return rows


def found_row(config_item: Dict[str, Any], api_item: Dict[str, Any], discovery_source: str) -> Dict[str, Any]:
    requested = api_season(config_item.get("season"))
    selected_year, selected_entry, available = selected_season_entry(api_item, requested)
    coverage_flags = fixture_coverage((selected_entry or {}).get("coverage", {}))
    usable = coverage_flags["statistics"]
    notes = "fixture statistics available" if usable else "fixture statistics not advertised for selected season"
    if selected_year and requested and selected_year != requested:
        notes += f"; requested season {requested} not available, used latest available season {selected_year}"
    return {
        "country": config_item.get("country"),
        "football_data_code": config_item.get("football_data_code"),
        "configured_display_name": config_item.get("display_name"),
        "requested_season": requested,
        "selected_api_season": selected_year,
        "available_seasons": available,
        "season_coverages": season_coverage_rows(api_item),
        "api_football_league_id": safe_get(api_item, "league", "id"),
        "api_football_league_name": safe_get(api_item, "league", "name") or "",
        "api_football_country": safe_get(api_item, "country", "name") or config_item.get("country") or "",
        "discovery_status": "FOUND",
        "discovery_source": discovery_source,
        "coverage_fixtures_events": coverage_flags["events"],
        "coverage_fixtures_lineups": coverage_flags["lineups"],
        "coverage_fixtures_statistics": coverage_flags["statistics"],
        "coverage_fixtures_players_statistics": coverage_flags["players_statistics"],
        "coverage_standings": coverage_flags["standings"],
        "coverage_odds": coverage_flags["odds"],
        "usable_for_enrichment": usable,
        "notes": notes,
    }


def not_found_row(config_item: Dict[str, Any], note: str) -> Dict[str, Any]:
    return {
        "country": config_item.get("country"),
        "football_data_code": config_item.get("football_data_code"),
        "configured_display_name": config_item.get("display_name"),
        "requested_season": api_season(config_item.get("season")),
        "selected_api_season": None,
        "available_seasons": [],
        "api_football_league_id": None,
        "api_football_league_name": "",
        "api_football_country": config_item.get("country"),
        "discovery_status": "NOT_FOUND",
        "discovery_source": "none",
        "coverage_fixtures_events": None,
        "coverage_fixtures_lineups": None,
        "coverage_fixtures_statistics": None,
        "coverage_fixtures_players_statistics": None,
        "coverage_standings": None,
        "coverage_odds": None,
        "usable_for_enrichment": False,
        "notes": note,
    }


def disabled_row(config_item: Dict[str, Any]) -> Dict[str, Any]:
    row = not_found_row(config_item, "disabled in config")
    row["discovery_status"] = "DISABLED"
    return row


def write_json(rows: List[Dict[str, Any]], request_count: int, config: Dict[str, Any]) -> None:
    payload = {
        "generated_at": now_utc(),
        "source": "api-football",
        "endpoint": "leagues",
        "request_count": request_count,
        "discovery_logic": "country-only discovery first, then candidate league name fallback; coverage evaluated only after league discovery",
        "usable_for_enrichment_rule": "discovery_status == FOUND and coverage.fixtures.statistics == true",
        "config_version": config.get("version"),
        "leagues": rows,
    }
    JSON_OUT.parent.mkdir(parents=True, exist_ok=True)
    JSON_OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_csv(rows: List[Dict[str, Any]]) -> None:
    CSV_OUT.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "country",
        "football_data_code",
        "configured_display_name",
        "requested_season",
        "selected_api_season",
        "api_football_league_id",
        "api_football_league_name",
        "api_football_country",
        "available_seasons",
        "discovery_status",
        "discovery_source",
        "coverage_fixtures_statistics",
        "coverage_fixtures_events",
        "coverage_fixtures_lineups",
        "coverage_fixtures_players_statistics",
        "coverage_standings",
        "coverage_odds",
        "usable_for_enrichment",
        "notes",
    ]
    with CSV_OUT.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            csv_row = {key: row.get(key) for key in fieldnames}
            csv_row["available_seasons"] = ", ".join(row.get("available_seasons") or [])
            writer.writerow(csv_row)


def md_cell(value: Any) -> str:
    if isinstance(value, bool) or value is None:
        value = yes_no_unknown(value)
    if isinstance(value, list):
        value = ", ".join(str(item) for item in value)
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def write_markdown(rows: List[Dict[str, Any]], request_count: int) -> None:
    headers = [
        "Country",
        "Football-Data code",
        "Requested season",
        "Selected API season",
        "API-Football League ID",
        "API-Football League Name",
        "Available seasons",
        "Discovery status",
        "Fixture statistics",
        "Events",
        "Lineups",
        "Players statistics",
        "Standings",
        "Odds",
        "Usable for StatMaker enrichment",
        "Notes",
    ]
    lines = [
        "# API-Football yellow league coverage",
        "",
        f"Generated at: `{now_utc()}`",
        f"Request count: `{request_count}`",
        "",
        "Discovery first calls `/leagues?country=<country>` without a season filter, then falls back to configured candidate league names.",
        "Fixture statistics is `Unknown` when no league was discovered, not `No`.",
        "Usable for StatMaker enrichment means `discovery_status == FOUND` and `coverage.fixtures.statistics == true`.",
        "",
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in rows:
        values = [
            row.get("country"),
            row.get("football_data_code"),
            row.get("requested_season"),
            row.get("selected_api_season"),
            row.get("api_football_league_id"),
            row.get("api_football_league_name"),
            row.get("available_seasons"),
            row.get("discovery_status"),
            row.get("coverage_fixtures_statistics"),
            row.get("coverage_fixtures_events"),
            row.get("coverage_fixtures_lineups"),
            row.get("coverage_fixtures_players_statistics"),
            row.get("coverage_standings"),
            row.get("coverage_odds"),
            row.get("usable_for_enrichment"),
            row.get("notes"),
        ]
        lines.append("| " + " | ".join(md_cell(value) for value in values) + " |")
    MD_OUT.parent.mkdir(parents=True, exist_ok=True)
    MD_OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    api_key = os.environ.get("API_FOOTBALL_KEY", "").strip()
    if not api_key:
        print("ERROR: API_FOOTBALL_KEY environment variable is required.", file=sys.stderr)
        return 2

    config = load_config()
    rows: List[Dict[str, Any]] = []
    request_counter = {"count": 0}
    for item in config.get("leagues", []):
        country = str(item.get("country") or "").strip()
        requested = api_season(item.get("season"))
        if not bool_value(item.get("enabled")):
            rows.append(disabled_row(item))
            print(f"coverage country={country} requested_season={requested} status=disabled count=0")
            continue
        if not country:
            rows.append(not_found_row(item, "missing country in config"))
            print("coverage country=UNKNOWN requested_season=UNKNOWN status=missing-config count=0")
            continue

        discovered, source = discover_leagues(api_key, item, request_counter)
        print(f"coverage country={country} requested_season={requested} discovery_source={source} discovered={len(discovered)}")
        if discovered:
            rows.extend(found_row(item, league, source) for league in discovered)
        else:
            rows.append(not_found_row(item, "No league discovered, not a stats coverage failure"))

    rows.sort(key=lambda row: (str(row.get("country") or ""), str(row.get("api_football_league_name") or "")))
    write_json(rows, request_counter["count"], config)
    write_csv(rows)
    write_markdown(rows, request_counter["count"])
    print(f"coverage reports written rows={len(rows)} requests={request_counter['count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
