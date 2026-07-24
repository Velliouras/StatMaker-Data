#!/usr/bin/env python3
"""Quota-aware Domestic Stats orchestration with targeted refresh and historical freeze.

Historical support seasons are fetched once, then frozen after every discovered completed
fixture has a final score and has had its fixture-statistics endpoint attempted at least once.
Current seasons remain incremental.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List

import refresh_domestic_live_july_stats as target
import statmaker_domestic_scope as scope

ROOT = Path(__file__).resolve().parents[1]
FREEZE_PATH = ROOT / "data" / "statmaker" / "domestic_historical_stats_freeze.json"
REPORT_PATH = ROOT / "reports" / "domestic_live_july_stats_refresh.json"


def normalize_code(value: Any) -> str:
    return scope.normalize_code(value)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def historical_season(league: Dict[str, Any]) -> str:
    return str(
        league.get("historyApiSeason")
        or league.get("season")
        or ""
    ).strip()


def target_season(league: Dict[str, Any]) -> str:
    return str(
        league.get("targetApiSeason")
        or league.get("target_api_football_season")
        or ""
    ).strip()


def is_historical_snapshot(league: Dict[str, Any]) -> bool:
    history = historical_season(league)
    current = target_season(league)
    return bool(history and current and history != current)


def freeze_key(league: Dict[str, Any]) -> str:
    return f"{normalize_code(league.get('leagueCode'))}:{historical_season(league)}"


def completed_cache_rows(league: Dict[str, Any]) -> List[Dict[str, Any]]:
    cache = load_json(target.stats_fetch.cache_path_for(league), {})
    return [
        item
        for item in cache.get("fixtures", []) or []
        if isinstance(item, dict)
        and str(item.get("status") or item.get("status_short") or "").upper()
        in target.COMPLETED_STATUSES
    ]


def historical_snapshot_ready_to_freeze(league: Dict[str, Any]) -> tuple[bool, Dict[str, int]]:
    rows = completed_cache_rows(league)
    with_scores = sum(1 for row in rows if target.has_final_score(row))
    stats_attempted = sum(1 for row in rows if "raw_statistics" in row)
    real_stats = sum(1 for row in rows if target.has_real_normalized_stats(row))
    summary = {
        "completedFixtures": len(rows),
        "withScores": with_scores,
        "statsAttempted": stats_attempted,
        "withRealStats": real_stats,
        "providerEmptyStats": max(0, stats_attempted - real_stats),
    }
    ready = bool(rows) and with_scores == len(rows) and stats_attempted == len(rows)
    return ready, summary


def parse_codes(raw: str) -> set[str]:
    return {
        normalize_code(code)
        for code in str(raw or "").replace(";", ",").split(",")
        if normalize_code(code)
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Domestic Stats refresh with optional targeted league scope")
    parser.add_argument("--max-requests", type=int, default=target.DEFAULT_MAX_REQUESTS)
    parser.add_argument(
        "--league-codes",
        default=os.getenv("STATMAKER_STATS_LEAGUE_CODES", ""),
        help="Optional comma-separated Domestic league codes, e.g. G1 or E0,G1",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    api_key = os.getenv("API_FOOTBALL_KEY", "").strip()
    if not api_key:
        print("ERROR: API_FOOTBALL_KEY is required.", file=sys.stderr)
        return 2

    target.scope.install_stats_registry_load_guard(target.pipeline)
    registry_payload = target.pipeline.load_json(target.pipeline.REGISTRY_PATH, {})
    registry = registry_payload.get("leagues", []) if isinstance(registry_payload, dict) else []
    registry = scope.filter_stats_leagues(registry)
    if not registry:
        print("ERROR: Domestic Stats registry is empty.", file=sys.stderr)
        return 3

    requested_codes = parse_codes(args.league_codes)
    selected = [
        league for league in registry
        if not requested_codes or normalize_code(league.get("leagueCode")) in requested_codes
    ]
    if requested_codes:
        found = {normalize_code(league.get("leagueCode")) for league in selected}
        missing = sorted(requested_codes - found)
        if missing:
            print(f"ERROR: requested league codes not present in rolling registry: {missing}", file=sys.stderr)
            return 4
    if not selected:
        print("ERROR: no Domestic Stats leagues selected.", file=sys.stderr)
        return 5

    freeze = load_json(
        FREEZE_PATH,
        {
            "schemaVersion": 1,
            "policy": "completed historical season: backfill once, then zero future API calls",
            "snapshots": {},
        },
    )
    snapshots = freeze.setdefault("snapshots", {})

    skipped_frozen: List[str] = []
    active: List[Dict[str, Any]] = []
    for league in selected:
        key = freeze_key(league)
        if is_historical_snapshot(league) and snapshots.get(key, {}).get("frozen") is True:
            skipped_frozen.append(key)
        else:
            active.append(league)

    if active:
        report = target.refresh_incrementally(api_key, active, max(1, args.max_requests))
    else:
        report = {
            "generatedAt": target.pipeline.now_utc(),
            "completenessContract": "all completed fixture scores are cached at discovery time; advanced statistics are incrementally backfilled",
            "incrementalContract": "current seasons incremental; completed historical snapshots frozen after one complete backfill",
            "scopeContract": "53 configured Domestic leagues eligible for Stats; targeted mode may refresh a subset",
            "priorityPolicy": "Frozen historical snapshots use zero API requests",
            "absolutePriorityLeagueCodes": sorted(scope.absolute_priority_codes()),
            "statsUniverseLeagueCount": len(scope.stats_universe_codes()),
            "coreOddsLeagueCount": len(scope.core_odds_codes()),
            "maxRequests": max(1, args.max_requests),
            "requestsUsed": 0,
            "registryLeagueCount": len(selected),
            "polledLeagueCount": 0,
            "incompleteBefore": 0,
            "completeAfter": 0,
            "incompleteAfter": 0,
            "allocations": [],
            "progress": [],
        }

    newly_frozen: List[str] = []
    for league in selected:
        if not is_historical_snapshot(league):
            continue
        key = freeze_key(league)
        if snapshots.get(key, {}).get("frozen") is True:
            continue
        ready, summary = historical_snapshot_ready_to_freeze(league)
        if ready:
            snapshots[key] = {
                "frozen": True,
                "leagueCode": normalize_code(league.get("leagueCode")),
                "country": league.get("country"),
                "competition": league.get("competition"),
                "historyApiSeason": historical_season(league),
                "targetApiSeasonAtFreeze": target_season(league),
                "frozenAt": target.pipeline.now_utc(),
                **summary,
            }
            newly_frozen.append(key)

    freeze["generatedAt"] = target.pipeline.now_utc()
    freeze["frozenSnapshotCount"] = sum(
        1 for row in snapshots.values() if isinstance(row, dict) and row.get("frozen") is True
    )
    write_json(FREEZE_PATH, freeze)

    report["selectedLeagueCodes"] = [normalize_code(league.get("leagueCode")) for league in selected]
    report["targetedMode"] = bool(requested_codes)
    report["historicalFreezePolicy"] = "backfill once then zero future API calls"
    report["historicalSkippedFrozen"] = skipped_frozen
    report["historicalNewlyFrozen"] = newly_frozen
    report["historicalFrozenSnapshotCount"] = freeze["frozenSnapshotCount"]
    write_json(REPORT_PATH, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
