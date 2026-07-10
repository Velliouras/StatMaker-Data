#!/usr/bin/env python3
"""Refresh exact Domestic odds for the published active/July registry only.

This job makes zero API-Football calls. It rotates Odds-API.io league batches,
merges refreshed leagues into the existing artifact, and preserves valid leagues
that were not processed in the current rate-limited cycle.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import domestic_live_july_pipeline as pipeline
import domestic_odds_expansion
import update_domestic_odds_api_io as odds_fetch


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh exact odds for active/July Domestic leagues")
    parser.add_argument("--cycle-size", type=int, default=pipeline.DEFAULT_ODDS_CYCLE_SIZE)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    domestic_odds_expansion.install(odds_fetch, pipeline)

    api_key = os.getenv("ODDS_API_IO_KEY", "").strip()
    if not api_key:
        print("ERROR: ODDS_API_IO_KEY is required.", file=sys.stderr)
        return 2

    registry_payload = pipeline.load_json(pipeline.REGISTRY_PATH, {})
    registry = registry_payload.get("leagues", []) if isinstance(registry_payload, dict) else []
    if not registry:
        print("ERROR: active/July registry is empty; refusing to overwrite odds.", file=sys.stderr)
        return 3

    domestic_config = pipeline.load_json(pipeline.DOMESTIC_CONFIG, {})
    state = pipeline.load_json(pipeline.STATE_PATH, {"statsCursor": 0, "oddsCursor": 0})
    feed = pipeline.fetch_odds_cycle(
        api_key=api_key,
        bookmakers=os.getenv("ODDS_API_IO_BOOKMAKERS", odds_fetch.DEFAULT_BOOKMAKERS).strip(),
        registry=registry,
        domestic_config=domestic_config,
        state=state,
        cycle_size=max(1, args.cycle_size),
        today=pipeline.today_utc(),
    )
    state["generatedAt"] = pipeline.now_utc()
    state["registryLeagueCount"] = len(registry)
    pipeline.write_json(pipeline.STATE_PATH, state)

    report = {
        "generatedAt": pipeline.now_utc(),
        "registryLeagueCount": len(registry),
        "cycleSize": max(1, args.cycle_size),
        "refreshedLeagueCodes": state.get("lastOddsLeagues", []),
        "rateLimitRemaining": state.get("lastOddsRateLimitRemaining"),
        "oddsLeagueCount": len(feed.get("leagues", []) or []),
        "oddsLeaguesWithMatches": sum(1 for league in feed.get("leagues", []) or [] if league.get("matches")),
        "matches": sum(len(league.get("matches", []) or []) for league in feed.get("leagues", []) or []),
        "markets": sum(
            len(match.get("markets", []) or [])
            for league in feed.get("leagues", []) or []
            for match in league.get("matches", []) or []
        ),
        "doubleChanceMarkets": sum(
            1
            for league in feed.get("leagues", []) or []
            for match in league.get("matches", []) or []
            for market in match.get("markets", []) or []
            if market.get("market") == "DOUBLE_CHANCE"
        ),
    }
    pipeline.write_json(pipeline.ROOT / "reports" / "domestic_live_july_odds_refresh.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
