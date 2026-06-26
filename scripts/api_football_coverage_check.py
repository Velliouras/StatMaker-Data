#!/usr/bin/env python3
"""Check API-Football league coverage for limited domestic Football-Data leagues.

This is a coverage proof only. It does not fetch fixtures, fixture statistics,
CSV files, odds, or app data. The Android app must continue to consume ready
repository files and must never call API-Football directly.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
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


def bool_value(value: Any) -> bool:
    return bool(value) if value is not None else False


def yes_no(value: bool) -> str:
    return "Yes" if value else "No"


def safe_get(mapping: Dict[str, Any], *keys: str) -> Any:
    current: Any = mapping
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


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


def request_leagues(api_key: str, country: str, season: str) -> Dict[str, Any]:
    query = urlencode({"country": country, "season": season})
    request = Request(
        f"{BASE_URL}?{query}",
        headers={
            "x-apisports-key": api_key,
            "Accept": "application/json",
            "User-Agent": "StatMaker-Data coverage check",
        },
        method="GET",
    )
    with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def row_from_response(config_item: Dict[str, Any], item: Dict[str, Any]) -> Dict[str, Any]:
    coverage = safe_get(item, "seasons")
    season_rows = coverage if isinstance(coverage, list) else []
    selected_season = api_season(config_item.get("season"))
    season_entry = next((entry for entry in season_rows if str(entry.get("year")) == selected_season), None)
    if season_entry is None and season_rows:
        season_entry = season_rows[0]
    coverage_flags = fixture_coverage((season_entry or {}).get("coverage", {}))
    usable = coverage_flags["statistics"]
    league_name = safe_get(item, "league", "name") or ""
    api_country = safe_get(item, "country", "name") or config_item.get("country") or ""
    notes = "fixture statistics available" if usable else "fixture statistics not advertised in coverage"
    return {
        "country": config_item.get("country"),
        "football_data_code": config_item.get("football_data_code"),
        "configured_display_name": config_item.get("display_name"),
        "api_football_league_id": safe_get(item, "league", "id"),
        "api_football_league_name": league_name,
        "api_football_country": api_country,
        "season": selected_season,
        "coverage_fixtures_events": coverage_flags["events"],
        "coverage_fixtures_lineups": coverage_flags["lineups"],
        "coverage_fixtures_statistics": coverage_flags["statistics"],
        "coverage_fixtures_players_statistics": coverage_flags["players_statistics"],
        "coverage_standings": coverage_flags["standings"],
        "coverage_odds": coverage_flags["odds"],
        "usable_for_enrichment": usable,
        "notes": notes,
    }


def disabled_row(config_item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "country": config_item.get("country"),
        "football_data_code": config_item.get("football_data_code"),
        "configured_display_name": config_item.get("display_name"),
        "api_football_league_id": None,
        "api_football_league_name": "",
        "api_football_country": config_item.get("country"),
        "season": api_season(config_item.get("season")),
        "coverage_fixtures_events": False,
        "coverage_fixtures_lineups": False,
        "coverage_fixtures_statistics": False,
        "coverage_fixtures_players_statistics": False,
        "coverage_standings": False,
        "coverage_odds": False,
        "usable_for_enrichment": False,
        "notes": "disabled in config",
    }


def no_result_row(config_item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "country": config_item.get("country"),
        "football_data_code": config_item.get("football_data_code"),
        "configured_display_name": config_item.get("display_name"),
        "api_football_league_id": None,
        "api_football_league_name": "",
        "api_football_country": config_item.get("country"),
        "season": api_season(config_item.get("season")),
        "coverage_fixtures_events": False,
        "coverage_fixtures_lineups": False,
        "coverage_fixtures_statistics": False,
        "coverage_fixtures_players_statistics": False,
        "coverage_standings": False,
        "coverage_odds": False,
        "usable_for_enrichment": False,
        "notes": "no API-Football leagues returned for country/season",
    }


def write_json(rows: List[Dict[str, Any]], request_count: int, config: Dict[str, Any]) -> None:
    payload = {
        "generated_at": now_utc(),
        "source": "api-football",
        "endpoint": "leagues",
        "request_count": request_count,
        "usable_for_enrichment_rule": "coverage.fixtures.statistics == true",
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
        "api_football_league_id",
        "api_football_league_name",
        "api_football_country",
        "season",
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
            writer.writerow({key: row.get(key) for key in fieldnames})


def md_cell(value: Any) -> str:
    if isinstance(value, bool):
        value = yes_no(value)
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def write_markdown(rows: List[Dict[str, Any]], request_count: int) -> None:
    headers = [
        "Country",
        "Football-Data code",
        "API-Football League ID",
        "API-Football League Name",
        "Season",
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
        "Usable for StatMaker enrichment means `coverage.fixtures.statistics == true`.",
        "",
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in rows:
        values = [
            row.get("country"),
            row.get("football_data_code"),
            row.get("api_football_league_id"),
            row.get("api_football_league_name"),
            row.get("season"),
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
    request_count = 0
    for item in config.get("leagues", []):
        country = str(item.get("country") or "").strip()
        season = api_season(item.get("season"))
        if not bool_value(item.get("enabled")):
            rows.append(disabled_row(item))
            print(f"coverage country={country} season={season} status=disabled count=0")
            continue
        if not country or not season:
            rows.append(no_result_row(item))
            print(f"coverage country={country or 'UNKNOWN'} season={season or 'UNKNOWN'} status=missing-config count=0")
            continue
        request_count += 1
        try:
            payload = request_leagues(api_key, country, season)
        except HTTPError as exc:
            print(f"coverage country={country} season={season} status=http-{exc.code} count=0")
            raise
        except URLError as exc:
            print(f"coverage country={country} season={season} status=url-error count=0")
            raise RuntimeError(f"API-Football request failed for {country} {season}: {exc.reason}") from exc

        response = payload.get("response") if isinstance(payload, dict) else []
        response = response if isinstance(response, list) else []
        print(f"coverage country={country} season={season} status=ok count={len(response)}")
        if response:
            rows.extend(row_from_response(item, league) for league in response)
        else:
            rows.append(no_result_row(item))
        time.sleep(REQUEST_DELAY_SECONDS)

    rows.sort(key=lambda row: (str(row.get("country") or ""), str(row.get("api_football_league_name") or "")))
    write_json(rows, request_count, config)
    write_csv(rows)
    write_markdown(rows, request_count)
    print(f"coverage reports written rows={len(rows)} requests={request_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
