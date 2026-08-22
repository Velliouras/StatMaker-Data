#!/usr/bin/env python3
"""Build the StatMaker Domestic Stats registry for the 53-league universe.

Selection is rolling rather than July-specific: a league is selected when its
season is active today or when the next season starts within 45 days. Upcoming
seasons use the immediately preceding completed season as historical support.
Recently-started cross-year seasons keep that completed-season history during
the opening rounds while schedule and odds stay on the current target season.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from typing import Any, Dict, Optional, Sequence, Tuple

import domestic_live_july_pipeline as pipeline
import statmaker_domestic_scope as scope

START_HORIZON_DAYS = 45
ROLLOVER_HISTORY_GRACE_DAYS = 70


def select_target_season_rolling(
    seasons: Sequence[Dict[str, Any]],
    today: dt.date,
    horizon_days: int = START_HORIZON_DAYS,
) -> Optional[Tuple[Dict[str, Any], str]]:
    active = []
    upcoming = []
    horizon_end = today + dt.timedelta(days=max(0, horizon_days))

    for season in seasons:
        start, end = pipeline.season_bounds(season)
        if start is None or end is None:
            continue
        if start <= today <= end:
            active.append((start, season))
        elif today < start <= horizon_end:
            upcoming.append((start, season))

    if active:
        return max(active, key=lambda item: item[0])[1], "active"
    if upcoming:
        return min(upcoming, key=lambda item: item[0])[1], "starts_soon"
    return None


def previous_completed_season(
    seasons: Sequence[Dict[str, Any]],
    target: Dict[str, Any],
) -> Dict[str, Any]:
    """Return the most recent season completed before target starts."""
    target_start, _ = pipeline.season_bounds(target)
    if target_start is None:
        return target

    previous = []
    for season in seasons:
        start, end = pipeline.season_bounds(season)
        if start is None or end is None or end >= target_start:
            continue
        previous.append((end, season))
    return max(previous, key=lambda item: item[0])[1] if previous else target


def select_history_season_rolling(
    seasons: Sequence[Dict[str, Any]],
    target: Dict[str, Any],
    lifecycle: str,
    today: dt.date,
    grace_days: int = ROLLOVER_HISTORY_GRACE_DAYS,
) -> Dict[str, Any]:
    """Preserve prior-season evidence only during a new cross-year season rollover.

    The target season always remains current for fixtures and odds. For a newly
    started season that spans two calendar years, historical evidence stays on
    the immediately preceding completed season during the opening rounds. This
    prevents the evidence sample from collapsing to 0-8 matches at rollover.
    Calendar-year leagues (for example Norway 2026) are never rolled back.
    """
    if lifecycle != "active":
        return previous_completed_season(seasons, target)

    target_start, target_end = pipeline.season_bounds(target)
    if target_start is None or target_end is None:
        return target

    # Only European-style split-year seasons need rollover continuity.
    # Calendar-year competitions keep their current-season history unchanged.
    if target_start.year == target_end.year:
        return target

    age_days = (today - target_start).days
    if age_days < 0 or age_days >= max(0, grace_days):
        return target

    return previous_completed_season(seasons, target)


def main() -> int:
    api_key = os.getenv("API_FOOTBALL_KEY", "").strip()
    if not api_key:
        print("ERROR: API_FOOTBALL_KEY is required.", file=sys.stderr)
        return 2

    today = pipeline.today_utc()
    domestic_config = pipeline.load_json(pipeline.DOMESTIC_CONFIG, {})
    enrichment_config = pipeline.load_json(pipeline.ENRICHMENT_CONFIG, {})

    original_target_selector = pipeline.select_target_season
    original_history_selector = pipeline.select_history_season
    pipeline.select_target_season = select_target_season_rolling
    pipeline.select_history_season = lambda seasons, target, lifecycle: select_history_season_rolling(
        seasons,
        target,
        lifecycle,
        today,
    )
    try:
        registry = pipeline.build_live_registry(
            domestic_config,
            enrichment_config,
            pipeline.api_football_catalog(api_key),
            today,
        )
    finally:
        pipeline.select_target_season = original_target_selector
        pipeline.select_history_season = original_history_selector

    registry = scope.filter_stats_leagues(registry)
    if not registry:
        print("ERROR: rolling 53-league Domestic Stats registry is empty.", file=sys.stderr)
        return 3

    pipeline.write_json(pipeline.REGISTRY_PATH, {
        "schemaVersion": 2,
        "generatedAt": pipeline.now_utc(),
        "asOfDate": today.isoformat(),
        "selectionPolicy": f"configured Stats-universe leagues active now or starting within {START_HORIZON_DAYS} days",
        "startHorizonDays": START_HORIZON_DAYS,
        "rolloverHistoryGraceDays": ROLLOVER_HISTORY_GRACE_DAYS,
        "statsUniverseConfiguredLeagueCount": len(scope.stats_universe_codes()),
        "coreOddsConfiguredLeagueCount": len(scope.included_codes()),
        "statsVisibility": "selected Stats leagues remain visible independently of odds",
        "bettingGate": "matching exact bookmaker odds plus valid historical support",
        "leagueCount": len(registry),
        "leagues": registry,
    })

    print(
        f"Domestic Stats registry written leagues={len(registry)} "
        f"stats_universe={len(scope.stats_universe_codes())} horizon_days={START_HORIZON_DAYS} "
        f"rollover_history_grace_days={ROLLOVER_HISTORY_GRACE_DAYS}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
