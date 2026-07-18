#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Set, Tuple

import api_football_fetch_fixture_stats as stats
import build_statmaker_domestic_enriched as enriched_build
import domestic_live_july_pipeline as pipeline

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "api_football_enrichment_leagues.json"
REPORT = ROOT / "reports" / "betting_candidate_stats_backfill.json"
LIVE_REGISTRY = ROOT / "data" / "statmaker" / "domestic_live_july_registry.json"
DEFAULT_CODES = "SRB,BGR,SVN,HUN,CZE,SVK,ISL,LVA,LTU,EST"
DEFAULT_MAX_REQUESTS = 85
DEFAULT_MATCHES_PER_TEAM = 5
COMPLETED_STATUSES = {"FT", "AET", "PEN"}


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_codes(value: str) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    for raw in str(value or "").split(","):
        code = raw.strip().upper()
        if code and code not in seen:
            seen.add(code)
            out.append(code)
    return out


def has_real_stats(item: Dict[str, Any]) -> bool:
    normalized = item.get("normalized_stats")
    raw = item.get("raw_statistics")
    return (
        isinstance(normalized, dict)
        and any(value is not None for value in normalized.values())
        and isinstance(raw, list)
        and bool(raw)
    )


def completed_cached_fixtures(cache: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = [row for row in cache.get("fixtures", []) or [] if isinstance(row, dict)]
    return [
        row for row in rows
        if str(row.get("status") or row.get("status_short") or "").upper() in COMPLETED_STATUSES
        and row.get("fixture_id") is not None
    ]


def team_names(item: Dict[str, Any]) -> Tuple[str, str]:
    return str(item.get("home_team") or "").strip(), str(item.get("away_team") or "").strip()


def recent_window_target_ids(fixtures: Sequence[Dict[str, Any]], matches_per_team: int) -> Tuple[Set[int], Dict[str, int]]:
    """Return the union of each team's most recent N completed fixtures.

    This is deliberately smaller than a whole-season backfill and is sufficient to
    make last-N trend calculations possible without changing any betting logic.
    """
    counts: Dict[str, int] = defaultdict(int)
    target_ids: Set[int] = set()
    ordered = sorted(
        fixtures,
        key=lambda row: (str(row.get("date") or ""), int(row.get("fixture_id") or 0)),
        reverse=True,
    )
    for item in ordered:
        fixture_id = int(item.get("fixture_id"))
        home, away = team_names(item)
        home_needed = bool(home) and counts[home] < matches_per_team
        away_needed = bool(away) and counts[away] < matches_per_team
        if home_needed or away_needed:
            target_ids.add(fixture_id)
        if home_needed:
            counts[home] += 1
        if away_needed:
            counts[away] += 1
    return target_ids, dict(counts)


def fixture_stub(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "fixture": {
            "id": item.get("fixture_id"),
            "date": item.get("date"),
            "status": {"short": item.get("status") or item.get("status_short")},
        },
        "teams": {
            "home": {"name": item.get("home_team")},
            "away": {"name": item.get("away_team")},
        },
        "goals": item.get("goals") or {
            "home": item.get("home_goals"),
            "away": item.get("away_goals"),
        },
        "score": item.get("score") or {},
    }


def league_plan(league: Dict[str, Any], matches_per_team: int) -> Dict[str, Any]:
    cache_path = stats.cache_path_for(league)
    cache = load_json(cache_path, {})
    fixtures = completed_cached_fixtures(cache)
    target_ids, team_counts = recent_window_target_ids(fixtures, matches_per_team)
    by_id = {int(row["fixture_id"]): row for row in fixtures}
    target_rows = [by_id[fixture_id] for fixture_id in target_ids if fixture_id in by_id]
    missing = [row for row in target_rows if not has_real_stats(row)]
    missing.sort(key=lambda row: (str(row.get("date") or ""), int(row.get("fixture_id") or 0)), reverse=True)
    teams = sorted(team_counts)
    return {
        "league": league,
        "cachePath": cache_path,
        "cache": cache,
        "fixtures": fixtures,
        "byId": by_id,
        "teams": teams,
        "targetIds": target_ids,
        "missing": missing,
        "targetFixtureCount": len(target_ids),
        "alreadyReadyTargetFixtures": len(target_ids) - len(missing),
    }


def readiness(plan: Dict[str, Any], matches_per_team: int) -> Dict[str, Any]:
    counts: Dict[str, int] = defaultdict(int)
    ordered = sorted(
        [row for row in plan["fixtures"] if int(row.get("fixture_id") or -1) in plan["targetIds"] and has_real_stats(row)],
        key=lambda row: (str(row.get("date") or ""), int(row.get("fixture_id") or 0)),
        reverse=True,
    )
    for item in ordered:
        home, away = team_names(item)
        if home and counts[home] < matches_per_team:
            counts[home] += 1
        if away and counts[away] < matches_per_team:
            counts[away] += 1
    teams = plan["teams"]
    ready_teams = sum(1 for team in teams if counts.get(team, 0) >= matches_per_team)
    return {
        "teamCount": len(teams),
        "readyTeams": ready_teams,
        "allTeamsReady": bool(teams) and ready_teams == len(teams),
        "minReadyMatchesPerTeam": min((counts.get(team, 0) for team in teams), default=0),
    }


def persist_plan(plan: Dict[str, Any]) -> None:
    league = plan["league"]
    all_rows = [row for row in plan["cache"].get("fixtures", []) or [] if isinstance(row, dict)]
    updated_by_id = {int(row.get("fixture_id")): row for row in all_rows if row.get("fixture_id") is not None}
    for fixture_id, row in plan["byId"].items():
        updated_by_id[fixture_id] = row
    stats.write_json(plan["cachePath"], stats.cache_payload(league, updated_by_id.values()))


def backfill(api_key: str, plans: List[Dict[str, Any]], max_requests: int, matches_per_team: int) -> Dict[str, Any]:
    request_state = {"count": 0}
    rows: List[Dict[str, Any]] = []

    # Finish the easiest leagues first. This maximizes the number of leagues that
    # become last-N ready per quota window instead of partially filling all of them.
    plans.sort(key=lambda plan: (len(plan["missing"]), str(plan["league"].get("country") or "")))

    for plan in plans:
        league = plan["league"]
        before = readiness(plan, matches_per_team)
        fetched = 0
        empty_responses = 0
        errors: List[str] = []

        for item in plan["missing"]:
            if request_state["count"] >= max_requests:
                break
            fixture_id = int(item["fixture_id"])
            try:
                payload = stats.api_get(
                    api_key,
                    "fixtures/statistics",
                    {"fixture": fixture_id},
                    request_state,
                    max_requests,
                )
            except stats.RequestLimitReached:
                break
            except Exception as exc:  # preserve prior cache on transient provider failures
                errors.append(f"fixture {fixture_id}: {type(exc).__name__}: {exc}")
                continue

            raw = stats.response_items(payload)
            if not raw:
                empty_responses += 1
                continue

            item["raw_statistics"] = raw
            item["normalized_stats"] = stats.normalize_statistics(raw, fixture_stub(item))
            plan["byId"][fixture_id] = item
            fetched += 1

        persist_plan(plan)
        after = readiness(plan, matches_per_team)
        rows.append({
            "leagueCode": league.get("leagueCode"),
            "country": league.get("country"),
            "league": league.get("display_name"),
            "season": league.get("season"),
            "cachePath": str(plan["cachePath"].relative_to(ROOT)).replace("\\", "/"),
            "targetFixtureCount": plan["targetFixtureCount"],
            "missingTargetFixturesBefore": len(plan["missing"]),
            "statsFetchedThisRun": fetched,
            "emptyStatsResponses": empty_responses,
            "before": before,
            "after": after,
            "errors": errors,
        })

        if request_state["count"] >= max_requests:
            break

    return {
        "mode": "betting-candidate-last-n-backfill",
        "bettingEngineTouched": False,
        "oddsFeedTouched": False,
        "matchesPerTeamTarget": matches_per_team,
        "maxRequests": max_requests,
        "requestsUsed": request_state["count"],
        "leagues": rows,
    }



def rebuild_selected_app_artifacts(codes: Sequence[str]) -> List[str]:
    registry_payload = load_json(LIVE_REGISTRY, {})
    registry_rows = registry_payload.get("leagues", []) if isinstance(registry_payload, dict) else []
    by_code = {str(row.get("leagueCode") or "").upper(): row for row in registry_rows if isinstance(row, dict)}
    missing = [code for code in codes if code not in by_code]
    if missing:
        raise SystemExit(f"Live Domestic registry is missing requested codes: {missing}")

    paths: List[str] = []
    for code in codes:
        league = by_code[code]
        artifact, _ = enriched_build.build_league_artifact(
            league,
            min_fixtures=pipeline.STRICT_READINESS_MIN_FIXTURES,
            min_coverage=pipeline.STRICT_READINESS_MIN_COVERAGE,
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
        output_path = enriched_build.output_path_for(league)
        enriched_build.write_json(output_path, artifact)
        paths.append(str(output_path.relative_to(ROOT)).replace("\\", "/"))
    return paths

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill recent API-Football stats for Domestic betting candidates only")
    parser.add_argument("--codes", default=DEFAULT_CODES, help="Comma-separated StatMaker league codes")
    parser.add_argument("--matches-per-team", type=int, default=DEFAULT_MATCHES_PER_TEAM)
    parser.add_argument("--max-requests", type=int, default=DEFAULT_MAX_REQUESTS)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.matches_per_team < 1:
        print("ERROR: --matches-per-team must be at least 1", file=sys.stderr)
        return 2
    if args.max_requests < 1:
        print("ERROR: --max-requests must be at least 1", file=sys.stderr)
        return 2

    api_key = os.getenv("API_FOOTBALL_KEY", "").strip()
    if not api_key:
        print("ERROR: API_FOOTBALL_KEY is required", file=sys.stderr)
        return 2

    codes = parse_codes(args.codes)
    config = load_json(CONFIG, {})
    by_code = {str(row.get("leagueCode") or "").upper(): row for row in config.get("leagues", []) or []}
    missing_codes = [code for code in codes if code not in by_code]
    if missing_codes:
        print(f"ERROR: Missing league codes from enrichment config: {missing_codes}", file=sys.stderr)
        return 3

    plans = [league_plan(by_code[code], args.matches_per_team) for code in codes]
    result = backfill(api_key, plans, args.max_requests, args.matches_per_team)
    result["requestedCodes"] = codes
    result["rebuiltAppArtifacts"] = rebuild_selected_app_artifacts(codes)
    save_json(REPORT, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
