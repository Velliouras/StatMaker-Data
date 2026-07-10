#!/usr/bin/env python3
"""Refresh incomplete API-Football stats fairly within the daily quota.

The scheduler allocates the remaining request budget across every incomplete
active/July league. A single large competition can no longer consume the whole
run. This is quota scheduling only; it does not alter betting evidence or apply
market heuristics.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Sequence, Tuple

import api_football_fetch_fixture_stats as stats_fetch
import domestic_live_july_pipeline as pipeline

DEFAULT_MAX_REQUESTS = 85
COMPLETED_STATUSES = {"FT", "AET", "PEN"}


def cache_progress(league: Dict[str, Any]) -> Dict[str, Any]:
    cache = pipeline.load_json(stats_fetch.cache_path_for(league), {})
    fixtures = [item for item in cache.get("fixtures", []) or [] if isinstance(item, dict)]
    completed = [
        item for item in fixtures
        if str(item.get("status") or item.get("status_short") or "").upper() in COMPLETED_STATUSES
    ]
    with_stats = [item for item in completed if isinstance(item.get("normalized_stats"), dict)]
    denominator = len(completed)
    coverage = len(with_stats) / denominator if denominator else 0.0
    return {
        "leagueCode": league.get("leagueCode"),
        "completed": denominator,
        "withStats": len(with_stats),
        "missingStats": max(0, denominator - len(with_stats)),
        "coverage": round(coverage, 6),
        "complete": denominator > 0 and len(with_stats) == denominator,
    }


def incomplete_leagues(registry: Sequence[Dict[str, Any]]) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    rows = [(league, cache_progress(league)) for league in registry]
    return sorted(
        [(league, progress) for league, progress in rows if not progress["complete"]],
        key=lambda item: (
            item[1]["coverage"],
            item[1]["completed"] == 0,
            0 if item[0].get("lifecycle") == "active" else 1,
            str(item[0].get("targetSeasonStart") or ""),
            str(item[0].get("country") or ""),
        ),
    )


def refresh_fairly(
    api_key: str,
    registry: Sequence[Dict[str, Any]],
    max_requests: int,
) -> Dict[str, Any]:
    pending = incomplete_leagues(registry)
    request_state = {"count": 0}
    fetch_rows: List[Dict[str, Any]] = []
    allocations: List[Dict[str, Any]] = []

    for index, (league, before) in enumerate(pending):
        remaining_budget = max_requests - request_state["count"]
        remaining_leagues = len(pending) - index
        if remaining_budget <= 0 or remaining_leagues <= 0:
            break
        fair_share = max(1, remaining_budget // remaining_leagues)
        league_limit = min(max_requests, request_state["count"] + fair_share)
        started = request_state["count"]
        row = stats_fetch.fetch_league(api_key, league, request_state, league_limit)
        fetch_rows.append(row)
        after = cache_progress(league)
        allocations.append({
            "leagueCode": league.get("leagueCode"),
            "country": league.get("country"),
            "league": league.get("competition"),
            "lifecycle": league.get("lifecycle"),
            "allocatedRequests": fair_share,
            "usedRequests": request_state["count"] - started,
            "before": before,
            "after": after,
        })

    stats_fetch.write_reports(fetch_rows, request_state["count"], max_requests)
    final_progress = [cache_progress(league) for league in registry]
    return {
        "generatedAt": pipeline.now_utc(),
        "maxRequests": max_requests,
        "requestsUsed": request_state["count"],
        "registryLeagueCount": len(registry),
        "incompleteBefore": len(pending),
        "completeAfter": sum(1 for row in final_progress if row["complete"]),
        "incompleteAfter": sum(1 for row in final_progress if not row["complete"]),
        "allocations": allocations,
        "progress": final_progress,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fair active/July Domestic stats refresh")
    parser.add_argument("--max-requests", type=int, default=DEFAULT_MAX_REQUESTS)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    api_key = os.getenv("API_FOOTBALL_KEY", "").strip()
    if not api_key:
        print("ERROR: API_FOOTBALL_KEY is required.", file=sys.stderr)
        return 2
    registry_payload = pipeline.load_json(pipeline.REGISTRY_PATH, {})
    registry = registry_payload.get("leagues", []) if isinstance(registry_payload, dict) else []
    if not registry:
        print("ERROR: active/July registry is empty.", file=sys.stderr)
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
