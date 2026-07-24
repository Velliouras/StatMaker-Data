#!/usr/bin/env python3
"""Refresh final-scope API-Football stats within the daily quota.

Only the authoritative 27 Domestic leagues are eligible. Main 5 leagues plus
Greek Super League receive first access to a protected request pool before the
remaining budget is shared across the rest of the active scope.
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

DEFAULT_MAX_REQUESTS = 85
COMPLETED_STATUSES = {"FT", "AET", "PEN"}
PROTECTED_PRIORITY_FRACTION = 0.60


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


def incomplete_leagues(registry: Sequence[Dict[str, Any]]) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    rows = [(league, cache_progress(league)) for league in scope.filter_leagues(registry)]
    return sorted(
        [(league, progress) for league, progress in rows if not progress["complete"]],
        key=lambda item: (
            scope.priority_rank(item[0]),
            item[1]["scoreCoverage"],
            item[1]["coverage"],
            item[1]["completed"] == 0,
            0 if item[0].get("lifecycle") == "active" else 1,
            str(item[0].get("targetSeasonStart") or ""),
            str(item[0].get("country") or ""),
        ),
    )


def process_group(
    api_key: str,
    pending: Sequence[Tuple[Dict[str, Any], Dict[str, Any]]],
    request_state: Dict[str, int],
    ceiling: int,
    fetch_rows: List[Dict[str, Any]],
    allocations: List[Dict[str, Any]],
    phase: str,
) -> None:
    for index, (league, before) in enumerate(pending):
        remaining_budget = ceiling - request_state["count"]
        remaining_leagues = len(pending) - index
        if remaining_budget <= 0 or remaining_leagues <= 0:
            break
        fair_share = max(1, remaining_budget // remaining_leagues)
        league_limit = min(ceiling, request_state["count"] + fair_share)
        started = request_state["count"]
        row = stats_fetch.fetch_league(api_key, league, request_state, league_limit)
        fetch_rows.append(row)
        after = cache_progress(league)
        allocations.append({
            "phase": phase,
            "leagueCode": league.get("leagueCode"),
            "country": league.get("country"),
            "league": league.get("competition"),
            "lifecycle": league.get("lifecycle"),
            "absolutePriority": scope.priority_rank(league) == 0,
            "allocatedRequests": fair_share,
            "usedRequests": request_state["count"] - started,
            "before": before,
            "after": after,
        })


def refresh_fairly(
    api_key: str,
    registry: Sequence[Dict[str, Any]],
    max_requests: int,
) -> Dict[str, Any]:
    registry = scope.filter_leagues(registry)
    pending_before = incomplete_leagues(registry)
    priority_pending = [item for item in pending_before if scope.priority_rank(item[0]) == 0]
    request_state = {"count": 0}
    fetch_rows: List[Dict[str, Any]] = []
    allocations: List[Dict[str, Any]] = []

    if priority_pending:
        priority_ceiling = min(
            max_requests,
            max(len(priority_pending), int(round(max_requests * PROTECTED_PRIORITY_FRACTION))),
        )
        process_group(
            api_key,
            priority_pending,
            request_state,
            priority_ceiling,
            fetch_rows,
            allocations,
            "protected_main5_plus_greece",
        )

    remaining_pending = incomplete_leagues(registry)
    if remaining_pending and request_state["count"] < max_requests:
        process_group(
            api_key,
            remaining_pending,
            request_state,
            max_requests,
            fetch_rows,
            allocations,
            "shared_remaining_budget",
        )

    stats_fetch.write_reports(fetch_rows, request_state["count"], max_requests)
    final_progress = [cache_progress(league) for league in registry]
    return {
        "generatedAt": pipeline.now_utc(),
        "completenessContract": "final score plus at least one non-null normalized API-Football statistic",
        "scopeContract": "final 27 Domestic leagues only",
        "priorityPolicy": "Main 5 plus Greek Super League receive first access to a protected 60% request pool; unused budget rolls into the shared pool",
        "absolutePriorityLeagueCodes": sorted(scope.absolute_priority_codes()),
        "maxRequests": max_requests,
        "requestsUsed": request_state["count"],
        "registryLeagueCount": len(registry),
        "incompleteBefore": len(pending_before),
        "completeAfter": sum(1 for row in final_progress if row["complete"]),
        "incompleteAfter": sum(1 for row in final_progress if not row["complete"]),
        "allocations": allocations,
        "progress": final_progress,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh final-scope Domestic stats with protected priority")
    parser.add_argument("--max-requests", type=int, default=DEFAULT_MAX_REQUESTS)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    api_key = os.getenv("API_FOOTBALL_KEY", "").strip()
    if not api_key:
        print("ERROR: API_FOOTBALL_KEY is required.", file=sys.stderr)
        return 2

    scope.install_registry_load_guard(pipeline)
    registry_payload = pipeline.load_json(pipeline.REGISTRY_PATH, {})
    registry = registry_payload.get("leagues", []) if isinstance(registry_payload, dict) else []
    registry = scope.filter_leagues(registry)
    if not registry:
        print("ERROR: final-scope Domestic registry is empty.", file=sys.stderr)
        return 3

    report = refresh_fairly(api_key, registry, max(1, args.max_requests))
    pipeline.write_json(
        pipeline.ROOT / "reports" / "domestic_live_july_stats_refresh.json",
        report,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
