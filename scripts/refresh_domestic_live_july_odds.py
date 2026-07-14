#!/usr/bin/env python3
"""Refresh exact odds for the active/July Domestic registry.

The job uses no API-Football quota. Canonical betting markets and every raw
Odds-API.io bookmaker market payload are stored separately. Empty/partial
refreshes preserve valid prior data, and rate-limited batches are retried rather
than skipped by the rotation cursor.

After each successful rotating batch, exact full-time corner totals are rebuilt
from the matching provider archive before the canonical feed is written. This
prevents a non-empty fresh league replacement from silently deleting legitimate
MATCH_CORNERS / TEAM_CORNERS selections added by the push-aware archive path.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from typing import Any, Dict, List, Sequence, Tuple

import domestic_live_july_pipeline as pipeline
import domestic_odds_expansion
import rebuild_domestic_corners_from_archive as corner_rebuild
import update_domestic_odds_api_io as odds_fetch

REPORT_PATH = pipeline.ROOT / "reports" / "domestic_live_july_odds_refresh.json"
PROVIDER_ARCHIVE_PATH = pipeline.ROOT / "odds" / "odds_api_io" / "domestic_provider_markets.json"


def _maps(previous, fresh, registry, today):
    codes = {str(row.get("leagueCode") or "") for row in registry}
    previous_by_code = {
        str(row.get("leagueCode") or ""): pipeline.prune_expired_matches(row, today)
        for row in previous.get("leagues", []) or []
        if str(row.get("leagueCode") or "") in codes
    }
    fresh_by_code = {
        str(row.get("leagueCode") or ""): pipeline.prune_expired_matches(row, today)
        for row in fresh.get("leagues", []) or []
        if str(row.get("leagueCode") or "") in codes
    }
    metadata = {str(row.get("leagueCode") or ""): row for row in registry}
    return codes, previous_by_code, fresh_by_code, metadata


def _merged_leagues(previous, fresh, registry, today, archive=False):
    codes, previous_by_code, fresh_by_code, metadata = _maps(previous, fresh, registry, today)
    combined: List[Dict[str, Any]] = []
    preserved: List[str] = []
    for code in sorted(codes):
        new = fresh_by_code.get(code)
        old = previous_by_code.get(code)
        if new and new.get("matches"):
            league = new
        elif old and old.get("matches"):
            league = old
            if new is not None:
                preserved.append(code)
        elif new is not None:
            league = new
        elif old is not None:
            league = old
        else:
            meta = metadata[code]
            league = {
                "leagueCode": code,
                "country": meta.get("country"),
                "competition": meta.get("competition"),
                "season": meta.get("targetAppSeason"),
                "matches": [],
            }
            if not archive:
                league.update(
                    {
                        "apiFootballLeagueId": meta.get("apiFootballLeagueId"),
                        "enabledForStats": True,
                        "enabledForOdds": bool(meta.get("enabledForOdds", True)),
                        "enabledForBetting": bool(meta.get("enabledForBetting", True)),
                    }
                )
        combined.append(league)
    return codes, combined, preserved


def safe_merge_odds_feed(previous, fresh, registry, today):
    codes, leagues, preserved = _merged_leagues(previous, fresh, registry, today)
    merged = dict(previous)
    merged.update(
        {
            "schemaVersion": max(
                int(previous.get("schemaVersion") or 0),
                int(fresh.get("schemaVersion") or 0),
                3,
            ),
            "source": "odds-api-io",
            "provider": "Odds-API.io",
            "generatedAt": fresh.get("generatedAt") or pipeline.now_utc(),
            "registry": fresh.get("registry") or previous.get("registry") or {},
            "dataContract": fresh.get("dataContract") or previous.get("dataContract") or {},
            "leagues": leagues,
        }
    )
    merged.pop("providerMarketsArchive", None)
    merged["debug"] = {
        **(previous.get("debug") or {}),
        **(fresh.get("debug") or {}),
        "mergePolicy": "replace only with non-empty fresh exact odds; preserve unexpired prior matches; prune expired matches",
        "selectedLeagueCount": len(codes),
        "leaguesWithUsableMatches": sum(1 for row in leagues if row.get("matches")),
        "preservedAfterEmptyRefresh": preserved,
    }
    return merged


def safe_merge_provider_archive(previous, fresh, registry, today):
    _, leagues, preserved = _merged_leagues(previous, fresh, registry, today, archive=True)
    return {
        "schemaVersion": max(
            int(previous.get("schemaVersion") or 0),
            int(fresh.get("schemaVersion") or 0),
            1,
        ),
        "source": "odds-api-io",
        "provider": "Odds-API.io",
        "generatedAt": fresh.get("generatedAt") or pipeline.now_utc(),
        "dataContract": fresh.get("dataContract")
        or previous.get("dataContract")
        or {
            "purpose": "Store every bookmaker market payload returned by Odds-API.io",
            "bettingInput": False,
            "estimatedPrices": False,
        },
        "mergePolicy": "rotate batches; preserve unexpired provider payloads after empty refresh",
        "preservedAfterEmptyRefresh": preserved,
        "leagues": leagues,
    }


def merge_refresh_payloads(
    previous: Dict[str, Any],
    fresh: Dict[str, Any],
    previous_archive: Dict[str, Any],
    fresh_archive: Dict[str, Any],
    registry: Sequence[Dict[str, Any]],
    today: dt.date,
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    """Merge a rotating refresh and restore exact archive-backed corner markets.

    The fresh canonical feed is still authoritative for fixtures and current odds.
    Corner rows are re-normalized only from the exact provider payload archive; no
    line conversion, estimated price, or synthetic fallback is introduced.
    """

    feed = safe_merge_odds_feed(previous, fresh, registry, today)
    archive = safe_merge_provider_archive(previous_archive, fresh_archive, registry, today)
    corner_report = corner_rebuild.rebuild_feed_corners(
        feed,
        archive,
        require_corners=False,
    )
    return feed, archive, corner_report


def validate_feed(feed, registry, today):
    expected = {str(row.get("leagueCode") or "") for row in registry}
    actual = {str(row.get("leagueCode") or "") for row in feed.get("leagues", []) or []}
    if expected != actual:
        raise RuntimeError(f"Odds registry mismatch: registry={len(expected)} feed={len(actual)}")
    matches = markets = 0
    for league in feed.get("leagues", []) or []:
        for match in league.get("matches", []) or []:
            date = str(match.get("date") or "")[:10]
            if date and date < today.isoformat():
                raise RuntimeError(f"Expired odds match: {league.get('leagueCode')} {date}")
            if match.get("teamMappingStatus") != "matched" or match.get("usableForStats") is not True:
                raise RuntimeError(f"Unmatched teams in canonical odds: {league.get('leagueCode')}")
            matches += 1
            for market in match.get("markets", []) or []:
                if market.get("exactBookmakerOdds") is not True or not str(market.get("bookmaker") or "").strip():
                    raise RuntimeError("Invalid canonical exact-odds market")
                try:
                    valid_price = float(market.get("odds")) > 1.0
                except (TypeError, ValueError):
                    valid_price = False
                if not valid_price:
                    raise RuntimeError("Invalid canonical decimal odds")
                markets += 1
    return {"leagueCount": len(actual), "matchCount": matches, "marketCount": markets}


def validate_provider_archive(archive, registry, today):
    expected = {str(row.get("leagueCode") or "") for row in registry}
    actual = {str(row.get("leagueCode") or "") for row in archive.get("leagues", []) or []}
    if expected != actual:
        raise RuntimeError(f"Provider archive mismatch: registry={len(expected)} archive={len(actual)}")
    matches = payloads = 0
    for league in archive.get("leagues", []) or []:
        for match in league.get("matches", []) or []:
            date = str(match.get("date") or "")[:10]
            if date and date < today.isoformat():
                raise RuntimeError(f"Expired provider archive match: {league.get('leagueCode')} {date}")
            matches += 1
            for payload in match.get("providerMarkets", []) or []:
                if payload.get("exactProviderPayload") is not True:
                    raise RuntimeError("Non-exact provider payload")
                if not str(payload.get("bookmaker") or "").strip() or not isinstance(payload.get("market"), dict):
                    raise RuntimeError("Invalid provider market payload")
                payloads += 1
    return {"leagueCount": len(actual), "matchCount": matches, "marketPayloadCount": payloads}


def processed_codes(debug: Dict[str, Any], selected: Sequence[Dict[str, Any]]) -> List[str]:
    completed = {
        str(row.get("leagueCode") or "")
        for row in (debug.get("leagueReports", []) or []) + (debug.get("leaguesMissing", []) or [])
    }
    return [
        str(row.get("leagueCode") or "")
        for row in selected
        if str(row.get("leagueCode") or "") in completed
    ]


def parse_args():
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

    config_all = pipeline.load_json(pipeline.DOMESTIC_CONFIG, {})
    state = pipeline.load_json(pipeline.STATE_PATH, {"statsCursor": 0, "oddsCursor": 0})
    eligible = [row for row in registry if bool(row.get("enabledForOdds", True))]
    cursor = int(state.get("oddsCursor") or 0)
    selected_registry = pipeline.rotated(eligible, cursor)[: max(1, args.cycle_size)]
    selected = [pipeline.odds_league_view(row) for row in selected_registry]
    config = {
        "version": max(int(config_all.get("version") or 0), 4),
        "horizonDays": max(int(config_all.get("horizonDays") or 0), 31),
        "leagues": selected,
    }
    debug: Dict[str, Any] = {"warnings": [], "apiCalls": []}
    aliases = pipeline.generated_aliases(registry)
    original_loader = odds_fetch.load_aliases
    odds_fetch.load_aliases = lambda: aliases
    try:
        fresh = odds_fetch.build_output(
            config,
            selected,
            api_key,
            False,
            os.getenv("ODDS_API_IO_BOOKMAKERS", odds_fetch.DEFAULT_BOOKMAKERS).strip(),
            debug,
        )
    finally:
        odds_fetch.load_aliases = original_loader

    completed = processed_codes(debug, selected_registry)
    today = pipeline.today_utc()
    previous = pipeline.load_json(pipeline.ODDS_PATH, {})
    previous_archive = pipeline.load_json(PROVIDER_ARCHIVE_PATH, {})
    corner_report: Dict[str, Any]
    if completed:
        fresh_archive = fresh.pop("providerMarketsArchive", {})
        fresh.setdefault("debug", {})["emittedMarketCounts"] = odds_fetch.emitted_market_counts(fresh)
        feed, archive, corner_report = merge_refresh_payloads(
            previous,
            fresh,
            previous_archive,
            fresh_archive,
            registry,
            today,
        )
        pipeline.write_json(pipeline.ODDS_PATH, feed)
        pipeline.write_json(PROVIDER_ARCHIVE_PATH, archive)
    else:
        feed = previous
        archive = previous_archive
        corner_report = dict((feed.get("debug") or {}).get("cornerArchiveRebuild") or {})

    validation = validate_feed(feed, registry, today)
    archive_validation = validate_provider_archive(archive, registry, today)
    pipeline.write_json(odds_fetch.REPORT_PATH, debug)

    if eligible and completed:
        state["oddsCursor"] = (cursor + len(completed)) % len(eligible)
    state.update(
        {
            "lastOddsLeagues": completed,
            "lastOddsRequestedLeagues": [row.get("leagueCode") for row in selected_registry],
            "lastOddsRateLimitRemaining": debug.get("rateLimitRemaining"),
            "lastOddsRefreshAt": pipeline.now_utc(),
            "registryLeagueCount": len(registry),
            "generatedAt": pipeline.now_utc(),
        }
    )
    pipeline.write_json(pipeline.STATE_PATH, state)
    report = {
        "generatedAt": pipeline.now_utc(),
        "registryLeagueCount": len(registry),
        "requestedLeagueCodes": state["lastOddsRequestedLeagues"],
        "processedLeagueCodes": completed,
        "deferredByRateLimit": len(completed) < len(selected_registry),
        "rateLimitRemaining": state.get("lastOddsRateLimitRemaining"),
        "validation": validation,
        "providerArchiveValidation": archive_validation,
        "cornerArchiveRebuild": corner_report,
        "oddsLeaguesWithMatches": sum(1 for row in feed.get("leagues", []) or [] if row.get("matches")),
        "providerArchiveLeaguesWithMatches": sum(
            1 for row in archive.get("leagues", []) or [] if row.get("matches")
        ),
        "nextCursor": state.get("oddsCursor"),
        "warnings": debug.get("warnings", []),
    }
    pipeline.write_json(REPORT_PATH, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
