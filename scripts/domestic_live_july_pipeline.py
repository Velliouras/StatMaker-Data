#!/usr/bin/env python3
"""Refresh StatMaker Domestic leagues that are active or start during July.

Contract:
- every selected league is published to the app-facing statistics index;
- historical statistics are independent from odds availability;
- betting output requires exact Odds-API.io prices and canonical team mapping;
- partial odds runs preserve still-valid data from leagues not processed;
- the Android app reads repository artifacts only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.request import Request, urlopen

import api_football_fetch_fixture_stats as stats_fetch
import build_statmaker_domestic_enriched as enriched_build
import update_domestic_odds_api_io as odds_fetch

ROOT = Path(__file__).resolve().parents[1]
DOMESTIC_CONFIG = ROOT / "config" / "domestic_leagues.json"
ENRICHMENT_CONFIG = ROOT / "config" / "api_football_enrichment_leagues.json"
REGISTRY_PATH = ROOT / "data" / "statmaker" / "domestic_live_july_registry.json"
STATE_PATH = ROOT / "data" / "statmaker" / "domestic_live_july_state.json"
REPORT_PATH = ROOT / "reports" / "domestic_live_july_pipeline.json"
ODDS_PATH = ROOT / "odds" / "odds_api_io" / "domestic_odds.json"
API_FOOTBALL_BASE = "https://v3.football.api-sports.io"
DEFAULT_STATS_REQUESTS = 85
DEFAULT_ODDS_CYCLE_SIZE = 8
STRICT_READINESS_MIN_FIXTURES = 15
STRICT_READINESS_MIN_COVERAGE = 0.65


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def today_utc() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_date(value: Any) -> Optional[dt.date]:
    text = str(value or "").strip()[:10]
    if not text:
        return None
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        return None


def app_season_label(start: dt.date, end: dt.date) -> str:
    return str(start.year) if start.year == end.year else f"{start.year}-{end.year}"


def api_football_catalog(api_key: str) -> List[Dict[str, Any]]:
    request = Request(
        f"{API_FOOTBALL_BASE}/leagues",
        headers={
            "x-apisports-key": api_key,
            "Accept": "application/json",
            "User-Agent": "StatMaker-Data live-july registry",
        },
        method="GET",
    )
    with urlopen(request, timeout=45) as response:
        payload = json.loads(response.read().decode("utf-8"))
    rows = payload.get("response") if isinstance(payload, dict) else []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def seasons_by_league_id(catalog: Sequence[Dict[str, Any]]) -> Dict[int, List[Dict[str, Any]]]:
    result: Dict[int, List[Dict[str, Any]]] = {}
    for row in catalog:
        league = row.get("league") or {}
        league_id = league.get("id")
        if league_id is None:
            continue
        seasons = row.get("seasons") if isinstance(row.get("seasons"), list) else []
        result[int(league_id)] = [season for season in seasons if isinstance(season, dict)]
    return result


def season_bounds(season: Dict[str, Any]) -> Tuple[Optional[dt.date], Optional[dt.date]]:
    return parse_date(season.get("start")), parse_date(season.get("end"))


def season_year(season: Dict[str, Any]) -> Optional[int]:
    try:
        return int(season.get("year"))
    except (TypeError, ValueError):
        start, _ = season_bounds(season)
        return start.year if start else None


def select_target_season(
    seasons: Sequence[Dict[str, Any]],
    today: dt.date,
) -> Optional[Tuple[Dict[str, Any], str]]:
    active: List[Tuple[dt.date, Dict[str, Any]]] = []
    july: List[Tuple[dt.date, Dict[str, Any]]] = []
    for season in seasons:
        start, end = season_bounds(season)
        if start is None or end is None:
            continue
        if start <= today <= end:
            active.append((start, season))
        elif start.year == today.year and start.month == 7:
            july.append((start, season))
    if active:
        return max(active, key=lambda item: item[0])[1], "active"
    if july:
        return max(july, key=lambda item: item[0])[1], "starts_in_july"
    return None


def select_history_season(
    seasons: Sequence[Dict[str, Any]],
    target: Dict[str, Any],
    lifecycle: str,
) -> Dict[str, Any]:
    if lifecycle == "active":
        return target
    target_start, _ = season_bounds(target)
    if target_start is None:
        return target
    previous: List[Tuple[dt.date, Dict[str, Any]]] = []
    for season in seasons:
        start, end = season_bounds(season)
        if start is None or end is None or end >= target_start:
            continue
        previous.append((end, season))
    return max(previous, key=lambda item: item[0])[1] if previous else target


def by_api_id(items: Iterable[Dict[str, Any]], key: str) -> Dict[int, Dict[str, Any]]:
    result: Dict[int, Dict[str, Any]] = {}
    for item in items:
        try:
            result[int(item.get(key))] = item
        except (TypeError, ValueError):
            continue
    return result


def build_live_registry(
    domestic_config: Dict[str, Any],
    enrichment_config: Dict[str, Any],
    catalog: Sequence[Dict[str, Any]],
    today: dt.date,
) -> List[Dict[str, Any]]:
    seasons_map = seasons_by_league_id(catalog)
    enrichment_by_id = by_api_id(enrichment_config.get("leagues", []), "api_football_league_id")
    selected: List[Dict[str, Any]] = []

    for domestic in domestic_config.get("leagues", []) or []:
        if not bool(domestic.get("enabled", True)) or not bool(domestic.get("enabledForStats", True)):
            continue
        try:
            league_id = int(domestic.get("apiFootballLeagueId"))
        except (TypeError, ValueError):
            continue
        all_seasons = seasons_map.get(league_id, [])
        target_choice = select_target_season(all_seasons, today)
        if target_choice is None:
            continue
        target, lifecycle = target_choice
        history = select_history_season(all_seasons, target, lifecycle)
        target_start, target_end = season_bounds(target)
        history_start, history_end = season_bounds(history)
        target_year = season_year(target)
        history_year = season_year(history)
        if None in (target_start, target_end, history_start, history_end, target_year, history_year):
            continue
        enrichment = enrichment_by_id.get(league_id, {})
        code = str(domestic.get("leagueCode") or enrichment.get("leagueCode") or "").strip()
        if not code:
            continue

        selected.append({
            "leagueCode": code,
            "continent": domestic.get("continent") or enrichment.get("continent"),
            "country": domestic.get("country") or enrichment.get("country"),
            "competition": domestic.get("competition") or enrichment.get("display_name"),
            "display_name": enrichment.get("display_name") or domestic.get("competition"),
            "football_data_code": enrichment.get("football_data_code") or code,
            "api_football_league_id": league_id,
            "apiFootballLeagueId": league_id,
            "season": str(history_year),
            "historyApiSeason": str(history_year),
            "historySeasonStart": history_start.isoformat(),
            "historySeasonEnd": history_end.isoformat(),
            "app_season": app_season_label(history_start, history_end),
            "targetApiSeason": str(target_year),
            "targetAppSeason": app_season_label(target_start, target_end),
            "targetSeasonStart": target_start.isoformat(),
            "targetSeasonEnd": target_end.isoformat(),
            "lifecycle": lifecycle,
            "statsVisible": True,
            "bettingRequiresExactOdds": True,
            "enabled": True,
            "enabledForStats": True,
            "enabledForOdds": bool(domestic.get("enabledForOdds", True)),
            "enabledForBetting": bool(domestic.get("enabledForBetting", True)),
            "group": domestic.get("group") or enrichment.get("priority_group"),
            "priority_group": enrichment.get("priority_group") or domestic.get("group"),
            "providerLeagueSlug": domestic.get("providerLeagueSlug"),
            "searchTerms": domestic.get("searchTerms", []),
        })

    lifecycle_order = {"active": 0, "starts_in_july": 1}
    return sorted(
        selected,
        key=lambda item: (
            lifecycle_order.get(str(item.get("lifecycle")), 9),
            str(item.get("targetSeasonStart") or ""),
            str(item.get("country") or ""),
            str(item.get("competition") or ""),
        ),
    )


def cache_has_statistics(league: Dict[str, Any]) -> bool:
    cache = load_json(stats_fetch.cache_path_for(league), {})
    fixtures = cache.get("fixtures") if isinstance(cache, dict) else []
    return any(
        isinstance(item, dict) and isinstance(item.get("normalized_stats"), dict)
        for item in fixtures or []
    )


def rotated(items: Sequence[Dict[str, Any]], cursor: int) -> List[Dict[str, Any]]:
    if not items:
        return []
    cursor %= len(items)
    return list(items[cursor:]) + list(items[:cursor])


def fetch_stats_cycle(
    api_key: str,
    registry: Sequence[Dict[str, Any]],
    state: Dict[str, Any],
    max_requests: int,
) -> List[Dict[str, Any]]:
    ordered_base = sorted(
        registry,
        key=lambda league: (
            cache_has_statistics(league),
            0 if league.get("lifecycle") == "active" else 1,
            str(league.get("targetSeasonStart") or ""),
        ),
    )
    cursor = int(state.get("statsCursor") or 0)
    ordered = rotated(ordered_base, cursor)
    request_state = {"count": 0}
    rows: List[Dict[str, Any]] = []
    processed = 0
    for league in ordered:
        if request_state["count"] >= max_requests:
            break
        rows.append(stats_fetch.fetch_league(api_key, league, request_state, max_requests))
        processed += 1
    stats_fetch.write_reports(rows, request_state["count"], max_requests)
    if registry:
        state["statsCursor"] = (cursor + max(processed, 1)) % len(registry)
    state["lastStatsRequests"] = request_state["count"]
    state["lastStatsLeagues"] = [row.get("api_football_league_id") for row in rows]
    return rows


def build_app_statistics(registry: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for league in registry:
        artifact, row = enriched_build.build_league_artifact(
            league,
            min_fixtures=STRICT_READINESS_MIN_FIXTURES,
            min_coverage=STRICT_READINESS_MIN_COVERAGE,
        )
        artifact.setdefault("competition", {}).update({
            "target_api_football_season": league.get("targetApiSeason"),
            "target_app_season": league.get("targetAppSeason"),
            "target_season_start": league.get("targetSeasonStart"),
            "target_season_end": league.get("targetSeasonEnd"),
            "lifecycle": league.get("lifecycle"),
            "stats_visible_without_odds": True,
            "betting_requires_exact_odds": True,
        })
        artifact.setdefault("data_contract", {}).update({
            "stats_visibility": "published independently of odds availability",
            "betting_gate": "exact bookmaker odds plus valid historical support",
        })
        enriched_build.write_json(enriched_build.output_path_for(league), artifact)
        row.update({
            "target_api_football_season": league.get("targetApiSeason"),
            "target_app_season": league.get("targetAppSeason"),
            "target_season_start": league.get("targetSeasonStart"),
            "target_season_end": league.get("targetSeasonEnd"),
            "lifecycle": league.get("lifecycle"),
            "stats_visible_without_odds": True,
            "betting_requires_exact_odds": True,
        })
        rows.append(row)

    rows = sorted(rows, key=lambda row: (str(row.get("country")), str(row.get("league"))))
    enriched_build.write_index(rows)
    index_path = enriched_build.OUTPUT_ROOT / "index.json"
    index = load_json(index_path, {})
    index.update({
        "selection_policy": "all configured leagues active now or starting on any July date",
        "stats_visibility": "independent_of_odds",
        "betting_gate": "exact_odds_required",
        "strict_readiness_min_fixtures": STRICT_READINESS_MIN_FIXTURES,
        "strict_readiness_min_coverage": STRICT_READINESS_MIN_COVERAGE,
    })
    write_json(index_path, index)
    enriched_build.write_reports(rows)
    return rows


def generated_aliases(registry: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, str]]:
    aliases = odds_fetch.load_aliases()
    for league in registry:
        code = str(league.get("leagueCode") or "")
        bucket = aliases.setdefault(code, {})
        cache = load_json(stats_fetch.cache_path_for(league), {})
        for fixture in cache.get("fixtures", []) or []:
            if not isinstance(fixture, dict):
                continue
            for key in ("home_team", "away_team"):
                canonical = str(fixture.get(key) or "").strip()
                normalized = odds_fetch.normalize_text(canonical, drop_suffixes=True)
                if canonical and normalized:
                    bucket[normalized] = canonical
    return aliases


def odds_league_view(league: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(league)
    result["season"] = league.get("targetAppSeason")
    return result


def prune_expired_matches(league: Dict[str, Any], today: dt.date) -> Dict[str, Any]:
    result = dict(league)
    result["matches"] = [
        match for match in league.get("matches", []) or []
        if str(match.get("date") or "") >= today.isoformat()
    ]
    return result


def merge_odds_feed(
    previous: Dict[str, Any],
    fresh: Dict[str, Any],
    registry: Sequence[Dict[str, Any]],
    today: dt.date,
) -> Dict[str, Any]:
    selected_codes = {str(league.get("leagueCode") or "") for league in registry}
    previous_by_code = {
        str(league.get("leagueCode") or ""): prune_expired_matches(league, today)
        for league in previous.get("leagues", []) or []
        if str(league.get("leagueCode") or "") in selected_codes
    }
    fresh_by_code = {
        str(league.get("leagueCode") or ""): prune_expired_matches(league, today)
        for league in fresh.get("leagues", []) or []
        if str(league.get("leagueCode") or "") in selected_codes
    }
    registry_by_code = {str(item.get("leagueCode") or ""): item for item in registry}
    combined: List[Dict[str, Any]] = []
    for code in sorted(selected_codes):
        league = fresh_by_code.get(code) or previous_by_code.get(code)
        if league is None:
            meta = registry_by_code[code]
            league = {
                "leagueCode": code,
                "country": meta.get("country"),
                "competition": meta.get("competition"),
                "season": meta.get("targetAppSeason"),
                "apiFootballLeagueId": meta.get("apiFootballLeagueId"),
                "enabledForStats": True,
                "enabledForOdds": bool(meta.get("enabledForOdds", True)),
                "enabledForBetting": bool(meta.get("enabledForBetting", True)),
                "matches": [],
            }
        combined.append(league)

    merged = dict(previous)
    merged.update({
        "schemaVersion": max(
            int(previous.get("schemaVersion") or 0),
            int(fresh.get("schemaVersion") or 0),
            3,
        ),
        "source": "odds-api-io",
        "provider": "Odds-API.io",
        "generatedAt": fresh.get("generatedAt") or now_utc(),
        "registry": fresh.get("registry") or previous.get("registry") or {},
        "dataContract": fresh.get("dataContract") or previous.get("dataContract") or {},
        "leagues": combined,
    })
    merged["debug"] = {
        **(previous.get("debug") or {}),
        **(fresh.get("debug") or {}),
        "mergePolicy": "replace refreshed leagues, preserve unprocessed leagues, prune expired matches",
        "selectedLeagueCount": len(selected_codes),
        "leaguesWithUsableMatches": sum(1 for league in combined if league.get("matches")),
    }
    return merged


def fetch_odds_cycle(
    api_key: str,
    bookmakers: str,
    registry: Sequence[Dict[str, Any]],
    domestic_config: Dict[str, Any],
    state: Dict[str, Any],
    cycle_size: int,
    today: dt.date,
) -> Dict[str, Any]:
    eligible = [league for league in registry if bool(league.get("enabledForOdds", True))]
    cursor = int(state.get("oddsCursor") or 0)
    selected_registry = rotated(eligible, cursor)[:max(1, cycle_size)]
    selected = [odds_league_view(league) for league in selected_registry]
    config = {
        "version": max(int(domestic_config.get("version") or 0), 4),
        "horizonDays": max(int(domestic_config.get("horizonDays") or 0), 31),
        "leagues": selected,
    }
    debug: Dict[str, Any] = {"warnings": [], "apiCalls": []}
    aliases = generated_aliases(registry)
    original_alias_loader = odds_fetch.load_aliases
    odds_fetch.load_aliases = lambda: aliases
    try:
        fresh = odds_fetch.build_output(config, selected, api_key, False, bookmakers, debug)
    finally:
        odds_fetch.load_aliases = original_alias_loader
    fresh.setdefault("debug", {})["emittedMarketCounts"] = odds_fetch.emitted_market_counts(fresh)
    previous = load_json(ODDS_PATH, {})
    merged = merge_odds_feed(previous, fresh, registry, today)
    write_json(ODDS_PATH, merged)
    write_json(odds_fetch.REPORT_PATH, merged.get("debug", {}))
    if eligible:
        state["oddsCursor"] = (cursor + len(selected_registry)) % len(eligible)
    state["lastOddsLeagues"] = [league.get("leagueCode") for league in selected_registry]
    state["lastOddsRateLimitRemaining"] = debug.get("rateLimitRemaining")
    return merged


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh active and July-starting Domestic leagues")
    parser.add_argument("--stats-max-requests", type=int, default=DEFAULT_STATS_REQUESTS)
    parser.add_argument("--odds-cycle-size", type=int, default=DEFAULT_ODDS_CYCLE_SIZE)
    parser.add_argument("--skip-stats-fetch", action="store_true")
    parser.add_argument("--skip-odds-fetch", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    today = today_utc()
    api_football_key = os.getenv("API_FOOTBALL_KEY", "").strip()
    odds_key = os.getenv("ODDS_API_IO_KEY", "").strip()
    bookmakers = os.getenv("ODDS_API_IO_BOOKMAKERS", odds_fetch.DEFAULT_BOOKMAKERS).strip()
    if not api_football_key:
        print("ERROR: API_FOOTBALL_KEY is required.", file=sys.stderr)
        return 2

    domestic_config = load_json(DOMESTIC_CONFIG, {})
    enrichment_config = load_json(ENRICHMENT_CONFIG, {})
    registry = build_live_registry(
        domestic_config,
        enrichment_config,
        api_football_catalog(api_football_key),
        today,
    )
    if not registry:
        print("ERROR: active/July registry is empty; refusing to replace artifacts.", file=sys.stderr)
        return 3

    write_json(REGISTRY_PATH, {
        "schemaVersion": 1,
        "generatedAt": now_utc(),
        "asOfDate": today.isoformat(),
        "selectionPolicy": "all configured leagues active now or starting on any July date",
        "statsVisibility": "all selected leagues remain visible without odds",
        "bettingGate": "exact bookmaker odds plus valid historical support",
        "leagueCount": len(registry),
        "leagues": registry,
    })

    state = load_json(STATE_PATH, {"statsCursor": 0, "oddsCursor": 0})
    stats_rows: List[Dict[str, Any]] = []
    if not args.skip_stats_fetch:
        stats_rows = fetch_stats_cycle(api_football_key, registry, state, args.stats_max_requests)
    index_rows = build_app_statistics(registry)

    odds_feed: Dict[str, Any] = load_json(ODDS_PATH, {})
    if not args.skip_odds_fetch:
        if not odds_key:
            print("ERROR: ODDS_API_IO_KEY is required unless --skip-odds-fetch is used.", file=sys.stderr)
            return 2
        odds_feed = fetch_odds_cycle(
            odds_key,
            bookmakers,
            registry,
            domestic_config,
            state,
            args.odds_cycle_size,
            today,
        )

    state.update({
        "generatedAt": now_utc(),
        "registryLeagueCount": len(registry),
    })
    write_json(STATE_PATH, state)
    report = {
        "generatedAt": now_utc(),
        "asOfDate": today.isoformat(),
        "registryLeagueCount": len(registry),
        "activeLeagueCount": sum(1 for league in registry if league.get("lifecycle") == "active"),
        "julyStartLeagueCount": sum(1 for league in registry if league.get("lifecycle") == "starts_in_july"),
        "statsFetchRows": stats_rows,
        "appStatsLeagueCount": len(index_rows),
        "oddsLeagueCount": len(odds_feed.get("leagues", []) or []),
        "oddsLeaguesWithMatches": sum(
            1 for league in odds_feed.get("leagues", []) or [] if league.get("matches")
        ),
        "state": state,
    }
    write_json(REPORT_PATH, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
