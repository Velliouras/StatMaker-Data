#!/usr/bin/env python3
"""Publish a lightweight rolling settlement feed for StatMaker Model Performance.

This producer is intentionally independent of the heavyweight App-Ready build. It polls only
API-Football's fixture scoreboard for the current/previous UTC day, filters to the configured
Domestic Stats universe plus StatMaker's three UEFA competitions, and fetches fixture statistics
only for newly completed matches. The Android app consumes the repository JSON and never calls
API-Football directly.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import api_football_daily_quota_guard as quota_guard
import api_football_fetch_fixture_stats as stats_fetch

ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "data" / "statmaker" / "domestic_live_july_registry.json"
CACHE_PATH = ROOT / "data" / "api_football" / "live_settlement_cache.json"
FEED_PATH = ROOT / "data" / "statmaker" / "live_settlements.json"

SCHEMA_VERSION = 1
DEFAULT_MAX_REQUESTS = 80
RETENTION_DAYS = 4
MAX_STATS_ATTEMPTS = 4
COMPLETED = {"FT", "AET", "PEN"}

# Verified API-Football competition ids already used by StatMaker's UEFA capability audit.
UEFA_PROVIDER_ROWS: Dict[int, Dict[str, Any]] = {
    2: {
        "leagueCode": "CL",
        "country": "Europe",
        "competition": "UEFA Champions League",
        "display_name": "UEFA Champions League",
        "lifecycle": "active",
    },
    3: {
        "leagueCode": "EL",
        "country": "Europe",
        "competition": "UEFA Europa League",
        "display_name": "UEFA Europa League",
        "lifecycle": "active",
    },
    848: {
        "leagueCode": "CONF",
        "country": "Europe",
        "competition": "UEFA Europa Conference League",
        "display_name": "UEFA Europa Conference League",
        "lifecycle": "active",
    },
}


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_now() -> str:
    return now_utc().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json_if_changed(path: Path, payload: Dict[str, Any], semantic_key: str) -> bool:
    existing = load_json(path, {})
    if isinstance(existing, dict) and existing.get(semantic_key) == payload.get(semantic_key):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True


def as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def fixture_date(value: Any) -> str:
    text = str(value or "").strip()
    return text[:10] if len(text) >= 10 else ""


def registry_rows() -> List[Dict[str, Any]]:
    root = load_json(REGISTRY_PATH, {})
    rows = root.get("leagues", []) if isinstance(root, dict) else []
    return [row for row in rows if isinstance(row, dict) and row.get("enabledForStats") is not False]


def provider_season(row: Dict[str, Any]) -> str:
    return str(
        row.get("targetApiSeason")
        or row.get("historyApiSeason")
        or row.get("season")
        or ""
    ).strip()


def choose_registry_row(
    fixture: Dict[str, Any],
    rows_by_provider: Dict[int, List[Dict[str, Any]]],
) -> Optional[Dict[str, Any]]:
    league = fixture.get("league") or {}
    provider_id = as_int(league.get("id"))
    if provider_id is None:
        return None

    uefa = UEFA_PROVIDER_ROWS.get(provider_id)
    if uefa is not None:
        return {
            **uefa,
            "season": str(league.get("season") or "").strip(),
            "app_season": str(league.get("season") or "").strip(),
        }

    candidates = rows_by_provider.get(provider_id, [])
    if not candidates:
        return None
    season = str(league.get("season") or "").strip()
    exact = [row for row in candidates if provider_season(row) == season]
    if len(exact) == 1:
        return exact[0]
    active = [row for row in (exact or candidates) if str(row.get("lifecycle") or "").lower() == "active"]
    return (active or exact or candidates)[0]


def normalized_stats_ready(stats: Any) -> bool:
    return isinstance(stats, dict) and any(value is not None for value in stats.values())


def cached_map(root: Dict[str, Any]) -> Dict[int, Dict[str, Any]]:
    result: Dict[int, Dict[str, Any]] = {}
    for item in root.get("fixtures", []) if isinstance(root, dict) else []:
        if not isinstance(item, dict):
            continue
        fixture_id = as_int(item.get("fixtureId"))
        if fixture_id is not None:
            result[fixture_id] = item
    return result


def prune_cache(rows: Dict[int, Dict[str, Any]], today: dt.date) -> None:
    cutoff = today - dt.timedelta(days=RETENTION_DAYS)
    stale = []
    for fixture_id, item in rows.items():
        date_text = fixture_date(item.get("dateUtc"))
        try:
            date_value = dt.date.fromisoformat(date_text)
        except ValueError:
            stale.append(fixture_id)
            continue
        if date_value < cutoff:
            stale.append(fixture_id)
    for fixture_id in stale:
        rows.pop(fixture_id, None)


def settlement_row(
    fixture: Dict[str, Any],
    registry: Dict[str, Any],
    existing: Dict[str, Any],
) -> Dict[str, Any]:
    summary = stats_fetch.fixture_summary(fixture)
    league = fixture.get("league") or {}
    status = stats_fetch.fixture_status_short(fixture)
    return {
        "fixtureId": summary.get("fixture_id"),
        "dateUtc": summary.get("date"),
        "leagueCode": str(registry.get("leagueCode") or "").strip(),
        "country": str(registry.get("country") or "").strip(),
        "competition": str(registry.get("competition") or registry.get("display_name") or "").strip(),
        "season": str(registry.get("app_season") or registry.get("targetAppSeason") or registry.get("season") or "").strip(),
        "apiFootballLeagueId": as_int(league.get("id")),
        "apiFootballSeason": str(league.get("season") or "").strip(),
        "homeTeam": summary.get("home_team"),
        "awayTeam": summary.get("away_team"),
        "status": status,
        "homeGoals": summary.get("home_goals"),
        "awayGoals": summary.get("away_goals"),
        "homeHalfGoals": summary.get("hthg"),
        "awayHalfGoals": summary.get("htag"),
        "normalizedStats": existing.get("normalizedStats") if isinstance(existing.get("normalizedStats"), dict) else {},
        "statsAttempts": int(existing.get("statsAttempts") or 0),
        "statsFetched": bool(existing.get("statsFetched", False)),
    }


def fetch_fixture_dates(
    api_key: str,
    dates: Iterable[str],
    request_state: Dict[str, int],
    max_requests: int,
) -> List[Dict[str, Any]]:
    fixtures: List[Dict[str, Any]] = []
    for date_text in dates:
        try:
            payload = stats_fetch.api_get(
                api_key,
                "fixtures",
                {"date": date_text},
                request_state,
                max_requests,
            )
        except stats_fetch.RequestLimitReached:
            break
        except Exception as error:
            print(f"live-settlement fixture poll failed date={date_text}: {error}", file=sys.stderr)
            continue
        error_text = stats_fetch.api_errors(payload)
        if error_text:
            print(f"live-settlement provider errors date={date_text}: {error_text}", file=sys.stderr)
        fixtures.extend(stats_fetch.response_items(payload))
    return fixtures


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh rolling StatMaker live settlement feed")
    parser.add_argument("--max-requests", type=int, default=DEFAULT_MAX_REQUESTS)
    args = parser.parse_args()
    if args.max_requests < 2:
        print("ERROR: --max-requests must be at least 2", file=sys.stderr)
        return 2

    api_key = os.environ.get("API_FOOTBALL_KEY", "").strip()
    if not api_key:
        print("ERROR: API_FOOTBALL_KEY is required", file=sys.stderr)
        return 2

    quota_guard.install(stats_fetch)
    registry = registry_rows()
    rows_by_provider: Dict[int, List[Dict[str, Any]]] = {}
    for row in registry:
        provider_id = as_int(row.get("api_football_league_id") or row.get("apiFootballLeagueId"))
        if provider_id is not None:
            rows_by_provider.setdefault(provider_id, []).append(row)

    existing_root = load_json(CACHE_PATH, {})
    cache = cached_map(existing_root if isinstance(existing_root, dict) else {})
    utc_today = now_utc().date()
    prune_cache(cache, utc_today)

    request_state = {"count": 0}
    poll_dates = [(utc_today - dt.timedelta(days=1)).isoformat(), utc_today.isoformat()]
    fixtures = fetch_fixture_dates(api_key, poll_dates, request_state, args.max_requests)

    completed_seen = 0
    stats_fetched = 0
    for fixture in fixtures:
        if stats_fetch.fixture_status_short(fixture) not in COMPLETED:
            continue
        registry_row = choose_registry_row(fixture, rows_by_provider)
        if registry_row is None:
            continue
        fixture_id = stats_fetch.fixture_identity(fixture)
        if fixture_id is None:
            continue
        completed_seen += 1
        existing = cache.get(fixture_id, {})
        row = settlement_row(fixture, registry_row, existing)
        if row.get("homeGoals") is None or row.get("awayGoals") is None:
            continue

        stats_ready = normalized_stats_ready(row.get("normalizedStats"))
        attempts = int(row.get("statsAttempts") or 0)
        if not stats_ready and attempts < MAX_STATS_ATTEMPTS and request_state["count"] < args.max_requests:
            row["statsAttempts"] = attempts + 1
            try:
                payload = stats_fetch.api_get(
                    api_key,
                    "fixtures/statistics",
                    {"fixture": fixture_id},
                    request_state,
                    args.max_requests,
                )
                raw = stats_fetch.response_items(payload)
                normalized = stats_fetch.normalize_statistics(raw, fixture)
                row["normalizedStats"] = normalized
                row["statsFetched"] = normalized_stats_ready(normalized)
                if row["statsFetched"]:
                    stats_fetched += 1
            except stats_fetch.RequestLimitReached:
                pass
            except Exception as error:
                print(f"live-settlement stats fetch failed fixture={fixture_id}: {error}", file=sys.stderr)
        cache[fixture_id] = row

    ordered = sorted(
        cache.values(),
        key=lambda item: (str(item.get("dateUtc") or ""), int(item.get("fixtureId") or 0)),
    )
    cache_payload = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": iso_now(),
        "retentionDays": RETENTION_DAYS,
        "fixtures": ordered,
    }
    cache_changed = write_json_if_changed(CACHE_PATH, cache_payload, "fixtures")

    feed_rows = [
        {
            "fixtureId": item.get("fixtureId"),
            "dateUtc": item.get("dateUtc"),
            "leagueCode": item.get("leagueCode"),
            "country": item.get("country"),
            "competition": item.get("competition"),
            "season": item.get("season"),
            "homeTeam": item.get("homeTeam"),
            "awayTeam": item.get("awayTeam"),
            "status": item.get("status"),
            "homeGoals": item.get("homeGoals"),
            "awayGoals": item.get("awayGoals"),
            "homeHalfGoals": item.get("homeHalfGoals"),
            "awayHalfGoals": item.get("awayHalfGoals"),
            "normalizedStats": item.get("normalizedStats") or {},
        }
        for item in ordered
        if str(item.get("status") or "").upper() in COMPLETED
        and item.get("homeGoals") is not None
        and item.get("awayGoals") is not None
    ]
    feed_payload = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": iso_now(),
        "source": "api-football-live-settlement",
        "completedStatuses": sorted(COMPLETED),
        "fixtures": feed_rows,
    }
    feed_changed = write_json_if_changed(FEED_PATH, feed_payload, "fixtures")

    print(
        "live-settlement "
        f"dates={','.join(poll_dates)} polled={len(fixtures)} inScopeCompleted={completed_seen} "
        f"statsFetched={stats_fetched} feedFixtures={len(feed_rows)} requests={request_state['count']} "
        f"cacheChanged={cache_changed} feedChanged={feed_changed} quota={json.dumps(quota_guard.status())}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
