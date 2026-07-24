#!/usr/bin/env python3
"""Incrementally refresh the 53-league StatMaker Domestic statistics universe.

Every active/upcoming registry league is lightly polled for fixture discovery so a
previously complete cache can discover newly completed matches. API-Football
statistics are fetched only for completed fixtures that do not already have real
cached statistics.

Priority tiers:
  Tier 1: Main 5 + Greek Super League
  Tier 2: the protected core 27 minus Tier 1
  Tier 3: the 26 restored Stats-only leagues

Budget policy is strict and sequential: Tier 1 receives first access to the full
run budget, Tier 2 uses only what Tier 1 leaves unused, and Tier 3 uses only the
remaining budget after Tier 2.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Sequence, Tuple

import api_football_fetch_fixture_stats as stats_fetch
import domestic_live_july_pipeline as pipeline
import statmaker_domestic_scope as scope

# One scheduled run per day. Even at the cap this leaves substantial room inside
# the user's 7,500/day API-Football PRO quota for other StatMaker workloads.
DEFAULT_MAX_REQUESTS = 2400
COMPLETED_STATUSES = {"FT", "AET", "PEN"}


def has_final_score(item: Dict[str, Any]) -> bool:
    home = item.get("home_goals")
    away = item.get("away_goals")
    if home is None:
        home = item.get("home_score")
    if away is None:
        away = item.get("away_score")
    if home is None:
        home = item.get("fthg")
    if away is None:
        away = item.get("ftag")
    if home is None or away is None:
        score = item.get("score") if isinstance(item.get("score"), dict) else {}
        fulltime = score.get("fulltime") if isinstance(score.get("fulltime"), dict) else {}
        home = home if home is not None else fulltime.get("home")
        away = away if away is not None else fulltime.get("away")
    return home is not None and away is not None


def has_real_normalized_stats(item: Dict[str, Any]) -> bool:
    normalized = item.get("normalized_stats")
    raw = item.get("raw_statistics")
    return (
        isinstance(normalized, dict)
        and any(value is not None for value in normalized.values())
        and isinstance(raw, list)
        and bool(raw)
    )


# Tighten the generic cache predicate so empty provider responses are retried.
stats_fetch.has_cached_stats = has_real_normalized_stats


def cache_progress(league: Dict[str, Any]) -> Dict[str, Any]:
    cache = pipeline.load_json(stats_fetch.cache_path_for(league), {})
    fixtures = [item for item in cache.get("fixtures", []) or [] if isinstance(item, dict)]
    completed = [
        item for item in fixtures
        if str(item.get("status") or item.get("status_short") or "").upper() in COMPLETED_STATUSES
    ]
    with_stats = [item for item in completed if has_real_normalized_stats(item)]
    with_scores = [item for item in completed if has_final_score(item)]
    denominator = len(completed)
    stats_coverage = len(with_stats) / denominator if denominator else 0.0
    score_coverage = len(with_scores) / denominator if denominator else 0.0
    return {
        "leagueCode": league.get("leagueCode"),
        "priorityTier": scope.priority_tier_name(league),
        "completed": denominator,
        "withStats": len(with_stats),
        "withScores": len(with_scores),
        "missingStats": max(0, denominator - len(with_stats)),
        "missingScores": max(0, denominator - len(with_scores)),
        "coverage": round(stats_coverage, 6),
        "scoreCoverage": round(score_coverage, 6),
        "complete": (
            denominator > 0
            and len(with_stats) == denominator
            and len(with_scores) == denominator
        ),
    }


def ordered_leagues(registry: Sequence[Dict[str, Any]]) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    rows = [(league, cache_progress(league)) for league in scope.filter_stats_leagues(registry)]
    return sorted(
        rows,
        key=lambda item: (
            scope.priority_rank(item[0]),
            item[1]["complete"],
            item[1]["scoreCoverage"],
            item[1]["coverage"],
            item[1]["completed"] == 0,
            0 if item[0].get("lifecycle") == "active" else 1,
            str(item[0].get("targetSeasonStart") or ""),
            str(item[0].get("country") or ""),
        ),
    )


def incomplete_leagues(registry: Sequence[Dict[str, Any]]) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """Compatibility helper retained for diagnostics/tests."""
    return [(league, progress) for league, progress in ordered_leagues(registry) if not progress["complete"]]


def process_group(
    api_key: str,
    rows: Sequence[Tuple[Dict[str, Any], Dict[str, Any]]],
    request_state: Dict[str, int],
    ceiling: int,
    fetch_rows: List[Dict[str, Any]],
    allocations: List[Dict[str, Any]],
    phase: str,
) -> None:
    for index, (league, before) in enumerate(rows):
        remaining_budget = ceiling - request_state["count"]
        remaining_leagues = len(rows) - index
        if remaining_budget <= 0 or remaining_leagues <= 0:
            break

        # Every league needs at least one fixture-discovery request. Any spare
        # share is available for missing fixture-stat backfill within that league.
        fair_share = max(1, remaining_budget // remaining_leagues)
        league_limit = min(ceiling, request_state["count"] + fair_share)
        started = request_state["count"]
        row = stats_fetch.fetch_league(api_key, league, request_state, league_limit)
        fetch_rows.append(row)
        after = cache_progress(league)
        allocations.append({
            "phase": phase,
            "priorityTier": scope.priority_tier_name(league),
            "leagueCode": league.get("leagueCode"),
            "country": league.get("country"),
            "league": league.get("competition"),
            "lifecycle": league.get("lifecycle"),
            "allocatedRequests": fair_share,
            "usedRequests": request_state["count"] - started,
            "before": before,
            "after": after,
        })


def refresh_incrementally(
    api_key: str,
    registry: Sequence[Dict[str, Any]],
    max_requests: int,
) -> Dict[str, Any]:
    registry = scope.filter_stats_leagues(registry)
    ordered = ordered_leagues(registry)
    groups = {
        rank: [item for item in ordered if scope.priority_rank(item[0]) == rank]
        for rank in (0, 1, 2)
    }

    request_state = {"count": 0}
    fetch_rows: List[Dict[str, Any]] = []
    allocations: List[Dict[str, Any]] = []
    incomplete_before = sum(1 for _, progress in ordered if not progress["complete"])

    tier1 = groups[0]
    if tier1:
        process_group(
            api_key, tier1, request_state, max_requests,
            fetch_rows, allocations, "tier1_main5_plus_greece",
        )

    tier2 = groups[1]
    if tier2 and request_state["count"] < max_requests:
        process_group(
            api_key, tier2, request_state, max_requests,
            fetch_rows, allocations, "tier2_core27",
        )

    tier3 = groups[2]
    if tier3 and request_state["count"] < max_requests:
        process_group(
            api_key, tier3, request_state, max_requests,
            fetch_rows, allocations, "tier3_restored26",
        )

    stats_fetch.write_reports(fetch_rows, request_state["count"], max_requests)
    final_progress = [cache_progress(league) for league in registry]
    return {
        "generatedAt": pipeline.now_utc(),
        "completenessContract": "final score plus at least one non-null normalized API-Football statistic",
        "incrementalContract": "poll every active Stats league for new completed fixtures; fetch statistics only when real cached stats are missing",
        "scopeContract": "53 configured Domestic leagues eligible for Stats; core odds scope remains separately protected",
        "priorityPolicy": "Strict sequential priority: Tier 1 Main5+Greece may use the full run budget first; Tier 2 and Tier 3 receive only unused remainder",
        "absolutePriorityLeagueCodes": sorted(scope.absolute_priority_codes()),
        "statsUniverseLeagueCount": len(scope.stats_universe_codes()),
        "coreOddsLeagueCount": len(scope.included_codes()),
        "maxRequests": max_requests,
        "requestsUsed": request_state["count"],
        "registryLeagueCount": len(registry),
        "polledLeagueCount": len(fetch_rows),
        "incompleteBefore": incomplete_before,
        "completeAfter": sum(1 for row in final_progress if row["complete"]),
        "incompleteAfter": sum(1 for row in final_progress if not row["complete"]),
        "allocations": allocations,
        "progress": final_progress,
    }


def refresh_fairly(api_key: str, registry: Sequence[Dict[str, Any]], max_requests: int) -> Dict[str, Any]:
    """Backward-compatible alias used by older callers."""
    return refresh_incrementally(api_key, registry, max_requests)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Incrementally refresh the 53-league Domestic Stats universe")
    parser.add_argument("--max-requests", type=int, default=DEFAULT_MAX_REQUESTS)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    api_key = os.getenv("API_FOOTBALL_KEY", "").strip()
    if not api_key:
        print("ERROR: API_FOOTBALL_KEY is required.", file=sys.stderr)
        return 2

    scope.install_stats_registry_load_guard(pipeline)
    registry_payload = pipeline.load_json(pipeline.REGISTRY_PATH, {})
    registry = registry_payload.get("leagues", []) if isinstance(registry_payload, dict) else []
    registry = scope.filter_stats_leagues(registry)
    if not registry:
        print("ERROR: 53-league Domestic Stats registry is empty.", file=sys.stderr)
        return 3

    report = refresh_incrementally(api_key, registry, max(1, args.max_requests))
    pipeline.write_json(
        pipeline.ROOT / "reports" / "domestic_live_july_stats_refresh.json",
        report,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
