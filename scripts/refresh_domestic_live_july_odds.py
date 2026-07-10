#!/usr/bin/env python3
"""Refresh exact Domestic odds for the published active/July registry only.

This job makes zero API-Football calls. It rotates Odds-API.io league batches,
keeps all registry leagues in the app artifact, prunes expired matches, and
preserves still-valid previous odds when a processed league returns no usable
replacement. Canonical betting markets and the complete provider-market archive
are written to separate artifacts.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from typing import Any, Dict, List, Sequence

import domestic_live_july_pipeline as pipeline
import domestic_odds_expansion
import update_domestic_odds_api_io as odds_fetch

REPORT_PATH = pipeline.ROOT / "reports" / "domestic_live_july_odds_refresh.json"
PROVIDER_ARCHIVE_PATH = pipeline.ROOT / "odds" / "odds_api_io" / "domestic_provider_markets.json"


def _league_maps(
    previous: Dict[str, Any],
    fresh: Dict[str, Any],
    registry: Sequence[Dict[str, Any]],
    today: dt.date,
) -> tuple[set[str], Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    selected_codes = {str(league.get("leagueCode") or "") for league in registry}
    previous_by_code = {
        str(league.get("leagueCode") or ""): pipeline.prune_expired_matches(league, today)
        for league in previous.get("leagues", []) or []
        if str(league.get("leagueCode") or "") in selected_codes
    }
    fresh_by_code = {
        str(league.get("leagueCode") or ""): pipeline.prune_expired_matches(league, today)
        for league in fresh.get("leagues", []) or []
        if str(league.get("leagueCode") or "") in selected_codes
    }
    registry_by_code = {str(item.get("leagueCode") or ""): item for item in registry}
    return selected_codes, previous_by_code, fresh_by_code, registry_by_code


def safe_merge_odds_feed(
    previous: Dict[str, Any],
    fresh: Dict[str, Any],
    registry: Sequence[Dict[str, Any]],
    today: dt.date,
) -> Dict[str, Any]:
    selected_codes, previous_by_code, fresh_by_code, registry_by_code = _league_maps(
        previous, fresh, registry, today
    )
    combined: List[Dict[str, Any]] = []
    preserved_after_empty_refresh: List[str] = []
    for code in sorted(selected_codes):
        fresh_league = fresh_by_code.get(code)
        previous_league = previous_by_code.get(code)
        if fresh_league and fresh_league.get("matches"):
            league = fresh_league
        elif previous_league and previous_league.get("matches"):
            league = previous_league
            if fresh_league is not None:
                preserved_after_empty_refresh.append(code)
        elif fresh_league is not None:
            league = fresh_league
        elif previous_league is not None:
            league = previous_league
        else:
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
        "generatedAt": fresh.get("generatedAt") or pipeline.now_utc(),
        "registry": fresh.get("registry") or previous.get("registry") or {},
        "dataContract": fresh.get("dataContract") or previous.get("dataContract") or {},
        "leagues": combined,
    })
    merged.pop("providerMarketsArchive", None)
    merged["debug"] = {
        **(previous.get("debug") or {}),
        **(fresh.get("debug") or {}),
        "mergePolicy": "replace with non-empty fresh exact odds; preserve unexpired previous matches after empty refresh; prune expired matches",
        "selectedLeagueCount": len(selected_codes),
        "leaguesWithUsableMatches": sum(1 for league in combined if league.get("matches")),
        "preservedAfterEmptyRefresh": preserved_after_empty_refresh,
    }
    return merged


def safe_merge_provider_archive(
    previous: Dict[str, Any],
    fresh: Dict[str, Any],
    registry: Sequence[Dict[str, Any]],
    today: dt.date,
) -> Dict[str, Any]:
    selected_codes, previous_by_code, fresh_by_code, registry_by_code = _league_maps(
        previous, fresh, registry, today
    )
    combined: List[Dict[str, Any]] = []
    preserved: List[str] = []
    for code in sorted(selected_codes):
        fresh_league = fresh_by_code.get(code)
        previous_league = previous_by_code.get(code)
        if fresh_league and fresh_league.get("matches"):
            league = fresh_league
        elif previous_league and previous_league.get("matches"):
            league = previous_league
            if fresh_league is not None:
                preserved.append(code)
        elif fresh_league is not None:
            league = fresh_league
        elif previous_league is not None:
            league = previous_league
        else:
            meta = registry_by_code[code]
            league = {
                "leagueCode": code,
                "country": meta.get("country"),
                "competition": meta.get("competition"),
                "season": meta.get("targetAppSeason"),
                "matches": [],
            }
        combined.append(league)
    return {
        "schemaVersion": max(int(previous.get("schemaVersion") or 0), int(fresh.get("schemaVersion") or 0), 1),
        "source": "odds-api-io",
        "provider": "Odds-API.io",
        "generatedAt": fresh.get("generatedAt") or pipeline.now_utc(),
        "dataContract": fresh.get("dataContract") or previous.get("dataContract") or {
            "purpose": "Store every bookmaker market payload returned by Odds-API.io",
            "bettingInput": False,
            "estimatedPrices": False,
        },
        "mergePolicy": "rotate league batches; preserve unexpired provider payloads after empty refresh",
        "preservedAfterEmptyRefresh": preserved,
        "leagues": combined,
    }


def validate_feed(
    feed: Dict[str, Any],
    registry: Sequence[Dict[str, Any]],
    today: dt.date,
) -> Dict[str, int]:
    registry_codes = {str(item.get("leagueCode") or "") for item in registry}
    feed_codes = {str(item.get("leagueCode") or "") for item in feed.get("leagues", []) or []}
    if registry_codes != feed_codes:
        raise RuntimeError(f"Odds registry mismatch: registry={len(registry_codes)} feed={len(feed_codes)}")
    matches = 0
    markets = 0
    for league in feed.get("leagues", []) or []:
        for match in league.get("matches", []) or []:
            date = str(match.get("date") or "")[:10]
            if date and date < today.isoformat():
                raise RuntimeError(f"Expired match remained in odds feed: {league.get('leagueCode')} {date}")
            if match.get("teamMappingStatus") != "matched" or match.get("usableForStats") is not True:
                raise RuntimeError(f"Unmatched teams reached production odds: {league.get('leagueCode')}")
            matches += 1
            for market in match.get("markets", []) or []:
                if market.get("exactBookmakerOdds") is not True:
                    raise RuntimeError("Non-exact market reached production odds")
                if not str(market.get("bookmaker") or "").strip():
                    raise RuntimeError("Bookmaker name missing from production market")
                try:
                    price = float(market.get("odds"))
                except (TypeError, ValueError) as exc:
                    raise RuntimeError("Invalid decimal odds in production market") from exc
                if price <= 1.0:
                    raise RuntimeError("Invalid decimal odds in production market")
                markets += 1
    return {"leagueCount": len(feed_codes), "matchCount": matches, "marketCount": markets}


def validate_provider_archive(
    archive: Dict[str, Any],
    registry: Sequence[Dict[str, Any]],
    today: dt.date,
) -> Dict[str, int]:
    registry_codes = {str(item.get("leagueCode") or "") for item in registry}
    archive_codes = {str(item.get("leagueCode") or "") for item in archive.get("leagues", []) or []}
    if registry_codes != archive_codes:
        raise RuntimeError(
            f"Provider archive registry mismatch: registry={len(registry_codes)} archive={len(archive_codes)}"
        )
    matches = 0
    payloads = 0
    for league in archive.get("leagues", []) or []:
        for match in league.get("matches", []) or []:
            date = str(match.get("date") or "")[:10]
            if date and date < today.isoformat():
                raise RuntimeError(f"Expired match remained in provider archive: {league.get('leagueCode')} {date}")
            matches += 1
            for payload in match.get("providerMarkets", []) or []:
                if payload.get("exactProviderPayload") is not True:
                    raise RuntimeError("Non-exact payload reached provider archive")
                if not str(payload.get("bookmaker") or "").strip():
                    raise RuntimeError("Bookmaker missing from provider archive")
                if not isinstance(payload.get("market"), dict):
                    raise RuntimeError("Raw provider market object missing")
                payloads += 1
    return {"leagueCount": len(archive_codes), "matchCount": matches, "marketPayloadCount": payloads}


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
    eligible = [league for league in registry if bool(league.get("enabledForOdds", True))]
    cursor = int(state.get("oddsCursor") or 0)
    selected_registry = pipeline.rotated(eligible, cursor)[:max(1, args.cycle_size)]
    selected = [pipeline.odds_league_view(league) for league in selected_registry]
    config = {
        "version": max(int(domestic_config.get("version") or 0), 4),
        "horizonDays": max(int(domestic_config.get("horizonDays") or 0), 31),
        "leagues": selected,
    }
    debug: Dict[str, Any] = {"warnings": [], "apiCalls": []}
    aliases = pipeline.generated_aliases(registry)
    original_alias_loader = odds_fetch.load_aliases
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
        odds_fetch.load_aliases = original_alias_loader

    fresh_archive = fresh.pop("providerMarketsArchive", {})
    fresh.setdefault("debug", {})["emittedMarketCounts"] = odds_fetch.emitted_market_counts(fresh)
    today = pipeline.today_utc()
    previous = pipeline.load_json(pipeline.ODDS_PATH, {})
    feed = safe_merge_odds_feed(previous, fresh, registry, today)
    previous_archive = pipeline.load_json(PROVIDER_ARCHIVE_PATH, {})
    archive = safe_merge_provider_archive(previous_archive, fresh_archive, registry, today)
    validation = validate_feed(feed, registry, today)
    archive_validation = validate_provider_archive(archive, registry, today)
    pipeline.write_json(pipeline.ODDS_PATH, feed)
    pipeline.write_json(PROVIDER_ARCHIVE_PATH, archive)
    pipeline.write_json(odds_fetch.REPORT_PATH, feed.get("debug", {}))

    if eligible:
        state["oddsCursor"] = (cursor + len(selected_registry)) % len(eligible)
    state.update({
        "lastOddsLeagues": [league.get("leagueCode") for league in selected_registry],
        "lastOddsRateLimitRemaining": debug.get("rateLimitRemaining"),
        "lastOddsRefreshAt": pipeline.now_utc(),
        "registryLeagueCount": len(registry),
        "generatedAt": pipeline.now_utc(),
    })
    pipeline.write_json(pipeline.STATE_PATH, state)
    report = {
        "generatedAt": pipeline.now_utc(),
        "registryLeagueCount": len(registry),
        "cycleSize": len(selected_registry),
        "refreshedLeagueCodes": state.get("lastOddsLeagues", []),
        "rateLimitRemaining": state.get("lastOddsRateLimitRemaining"),
        "validation": validation,
        "providerArchiveValidation": archive_validation,
        "oddsLeaguesWithMatches": sum(1 for league in feed.get("leagues", []) or [] if league.get("matches")),
        "providerArchiveLeaguesWithMatches": sum(
            1 for league in archive.get("leagues", []) or [] if league.get("matches")
        ),
        "preservedAfterEmptyRefresh": feed.get("debug", {}).get("preservedAfterEmptyRefresh", []),
        "providerArchivePreservedAfterEmptyRefresh": archive.get("preservedAfterEmptyRefresh", []),
        "nextCursor": state.get("oddsCursor"),
        "warnings": debug.get("warnings", []),
    }
    pipeline.write_json(REPORT_PATH, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
