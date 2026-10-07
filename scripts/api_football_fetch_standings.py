#!/usr/bin/env python3
"""Fetch official API-Football standings descriptions for StatMaker league-table zones."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = ROOT / "data" / "statmaker" / "domestic_enriched" / "index.json"
CACHE_PATH = ROOT / "data" / "api_football" / "standings" / "current_standings.json"
REPORT_PATH = ROOT / "reports" / "api_football_standings_fetch.json"

BASE_URL = "https://v3.football.api-sports.io"
DEFAULT_MAX_REQUESTS = 60
REQUEST_DELAY_SECONDS = 0.75
TIMEOUT_SECONDS = 30


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def api_get(api_key: str, league_id: int, season: str) -> dict[str, Any]:
    query = urlencode({"league": league_id, "season": season})
    request = Request(
        f"{BASE_URL}/standings?{query}",
        headers={
            "x-apisports-key": api_key,
            "Accept": "application/json",
            "User-Agent": "StatMaker-Data standings cache",
        },
        method="GET",
    )
    with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        payload = json.loads(response.read().decode("utf-8"))
    time.sleep(REQUEST_DELAY_SECONDS)
    return payload if isinstance(payload, dict) else {}


def current_entries() -> list[dict[str, Any]]:
    index = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    rows: dict[str, dict[str, Any]] = {}
    for row in index.get("leagues") or []:
        if not isinstance(row, dict):
            continue
        code = str(row.get("league_code") or "").strip().upper()
        if not code:
            continue
        if str(row.get("lifecycle") or "") != "active":
            continue
        if int(row.get("completed_fixtures") or 0) <= 0:
            continue
        league_id = int(row.get("api_football_league_id") or 0)
        api_season = str(row.get("api_football_season") or row.get("target_api_football_season") or "").strip()
        if league_id <= 0 or not api_season:
            continue

        current = rows.get(code)
        if current is None:
            rows[code] = row
            continue

        current_season = str(
            current.get("api_football_season")
            or current.get("target_api_football_season")
            or ""
        ).strip()
        current_target = str(current.get("stats_role") or "") == "current_target"
        candidate_target = str(row.get("stats_role") or "") == "current_target"

        if api_season > current_season or (
            api_season == current_season
            and candidate_target
            and not current_target
        ):
            rows[code] = row

    return sorted(rows.values(), key=lambda row: (str(row.get("country") or ""), str(row.get("league_code") or "")))


def extract_rows(payload: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    errors = payload.get("errors")
    if errors:
        raise RuntimeError(f"API-Football standings error: {errors}")

    response = payload.get("response")
    if not isinstance(response, list) or not response:
        return "", []

    league = response[0].get("league") if isinstance(response[0], dict) else {}
    if not isinstance(league, dict):
        return "", []

    league_name = str(league.get("name") or "").strip()
    standings = league.get("standings")
    if not isinstance(standings, list):
        return league_name, []

    rows: list[dict[str, Any]] = []
    seen: set[tuple[int, str, str]] = set()
    for group_rows in standings:
        if not isinstance(group_rows, list):
            continue
        for row in group_rows:
            if not isinstance(row, dict):
                continue
            rank = int(row.get("rank") or 0)
            description = str(row.get("description") or "").strip()
            group = str(row.get("group") or "").strip()
            team = row.get("team") if isinstance(row.get("team"), dict) else {}
            if rank <= 0:
                continue
            key = (rank, description, group)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "rank": rank,
                    "description": description,
                    "group": group,
                    "teamId": int(team.get("id") or 0),
                    "team": str(team.get("name") or "").strip(),
                }
            )
    rows.sort(key=lambda row: (row["rank"], row["group"], row["team"]))
    return league_name, rows


def load_cache() -> dict[str, Any]:
    try:
        payload = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload.setdefault("schemaVersion", 1)
    payload.setdefault("provider", "api-football")
    payload.setdefault("leagues", {})
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-requests", type=int, default=DEFAULT_MAX_REQUESTS)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    api_key = str(os.environ.get("API_FOOTBALL_KEY") or "").strip()
    if not api_key:
        raise SystemExit("Missing API_FOOTBALL_KEY")
    if args.max_requests < 1:
        raise SystemExit("--max-requests must be >= 1")

    cache = load_cache()
    leagues = cache["leagues"] if isinstance(cache.get("leagues"), dict) else {}
    cache["leagues"] = leagues
    fetched = 0
    skipped = 0
    failures: list[dict[str, str]] = []

    for entry in current_entries():
        if fetched >= args.max_requests:
            break
        code = str(entry.get("league_code") or "").strip().upper()
        league_id = int(entry.get("api_football_league_id") or 0)
        api_season = str(entry.get("api_football_season") or entry.get("target_api_football_season") or "").strip()
        existing = leagues.get(code) if isinstance(leagues.get(code), dict) else {}
        if (
            not args.force
            and str(existing.get("apiFootballSeason") or "") == api_season
            and int(existing.get("leagueId") or 0) == league_id
            and isinstance(existing.get("rows"), list)
            and existing.get("rows")
        ):
            skipped += 1
            continue

        try:
            payload = api_get(api_key, league_id, api_season)
            league_name, rows = extract_rows(payload)
            leagues[code] = {
                "leagueCode": code,
                "leagueId": league_id,
                "apiFootballSeason": api_season,
                "appSeason": str(entry.get("app_season") or entry.get("target_app_season") or ""),
                "country": str(entry.get("country") or ""),
                "league": league_name or str(entry.get("league") or ""),
                "generatedAt": now_utc(),
                "rows": rows,
            }
            fetched += 1
            print("STANDINGS_FETCH_OK", code, api_season, f"rows={len(rows)}")
        except Exception as exc:
            failures.append({"leagueCode": code, "error": f"{type(exc).__name__}: {exc}"})
            print("STANDINGS_FETCH_FAILED", code, type(exc).__name__, str(exc))

    cache["generatedAt"] = now_utc()
    cache["leagueCount"] = len(leagues)
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    report = {
        "generatedAt": cache["generatedAt"],
        "requestsUsed": fetched,
        "maxRequests": args.max_requests,
        "skippedCached": skipped,
        "failures": failures,
        "cachedLeagueCount": len(leagues),
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(
        "API_FOOTBALL_STANDINGS_FETCH_DONE",
        f"requests={fetched}",
        f"cached={len(leagues)}",
        f"skipped={skipped}",
        f"failures={len(failures)}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
