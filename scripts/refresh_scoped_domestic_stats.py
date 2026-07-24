#!/usr/bin/env python3
"""Refresh final-scope Domestic stats with protected priority for Main 5 + Greece."""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Sequence, Tuple

import refresh_domestic_live_july_stats as target
import statmaker_domestic_scope as scope

PROTECTED_PRIORITY_FRACTION = 0.60


def _pending(registry: Sequence[Dict[str, Any]]) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    rows = target.incomplete_leagues(registry)
    return sorted(
        rows,
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


def _process_group(
    *,
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
        row = target.stats_fetch.fetch_league(api_key, league, request_state, league_limit)
        fetch_rows.append(row)
        after = target.cache_progress(league)
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


def refresh_with_protected_priority(
    api_key: str,
    registry: Sequence[Dict[str, Any]],
    max_requests: int,
) -> Dict[str, Any]:
    registry = scope.filter_leagues(registry)
    pending_before = _pending(registry)
    priority_pending = [item for item in pending_before if scope.priority_rank(item[0]) == 0]

    request_state = {"count": 0}
    fetch_rows: List[Dict[str, Any]] = []
    allocations: List[Dict[str, Any]] = []

    if priority_pending:
        protected_ceiling = min(
            max_requests,
            max(len(priority_pending), int(round(max_requests * PROTECTED_PRIORITY_FRACTION))),
        )
        _process_group(
            api_key=api_key,
            pending=priority_pending,
            request_state=request_state,
            ceiling=protected_ceiling,
            fetch_rows=fetch_rows,
            allocations=allocations,
            phase="protected_main5_plus_greece",
        )

    remaining_pending = _pending(registry)
    if remaining_pending and request_state["count"] < max_requests:
        _process_group(
            api_key=api_key,
            pending=remaining_pending,
            request_state=request_state,
            ceiling=max_requests,
            fetch_rows=fetch_rows,
            allocations=allocations,
            phase="shared_remaining_budget",
        )

    target.stats_fetch.write_reports(fetch_rows, request_state["count"], max_requests)
    final_progress = [target.cache_progress(league) for league in registry]
    return {
        "generatedAt": target.pipeline.now_utc(),
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


def main() -> int:
    args = target.parse_args()
    api_key = os.getenv("API_FOOTBALL_KEY", "").strip()
    if not api_key:
        print("ERROR: API_FOOTBALL_KEY is required.", file=sys.stderr)
        return 2

    scope.install_registry_load_guard(target.pipeline)
    registry_payload = target.pipeline.load_json(target.pipeline.REGISTRY_PATH, {})
    registry = registry_payload.get("leagues", []) if isinstance(registry_payload, dict) else []
    registry = scope.filter_leagues(registry)
    if not registry:
        print("ERROR: final-scope Domestic registry is empty.", file=sys.stderr)
        return 3

    report = refresh_with_protected_priority(api_key, registry, max(1, args.max_requests))
    target.pipeline.write_json(
        target.pipeline.ROOT / "reports" / "domestic_live_july_stats_refresh.json",
        report,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
