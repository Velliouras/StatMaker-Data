#!/usr/bin/env python3
"""Build-only UEFA qualifier feed for CL/EL/Conference.

All provider fixtures are preserved for schedule/statistics visibility, even when
Bet365/Unibet exact markets are not currently available. Fixture discovery is
attempted for every UEFA competition even when the odds rate-limit guard is active.
Betting still fails closed: only exact bookmaker markets are emitted in ``markets``.
When odds refresh is skipped by the guard, previously published exact markets for
the same still-live fixture are preserved instead of being erased.

Unknown teams remain provider_identity with usableForStats=false until verified
historical readiness promotes them in the Android app.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

import update_uefa_club_odds_api_io as base

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "uefa_club_competitions.json"


def provider_match(competition: Dict[str, Any], provider_leagues: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    terms = [base.odds.normalize_text(term) for term in competition.get("searchTerms", []) if str(term).strip()]
    target = str(competition.get("leagueCode") or "").upper()
    blocked = {"women", "womens", "woman", "youth", "u19", "u21"}
    scored = []
    for item in provider_leagues:
        text = base.provider_text(item)
        words = set(text.split())
        if words & blocked:
            continue
        if target == "CL" and ("europa" in words or "conference" in words):
            continue
        if target == "EL" and ("champions" in words or "conference" in words):
            continue
        if target == "UECL" and ("champions" in words or "europa" in words and "conference" not in words):
            continue
        best = 0
        for term in terms:
            if term and term in text:
                best = max(best, 200 + len(term))
                continue
            term_words = [word for word in term.split() if len(word) > 2]
            hits = sum(1 for word in term_words if word in words)
            if term_words and hits == len(term_words):
                best = max(best, 100 + hits)
        if best:
            scored.append((best, int(item.get("eventsCount") or 0), item))
    if not scored:
        return None
    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return scored[0][2]


def normalize_event(
    competition: Dict[str, Any],
    event: Dict[str, Any],
    odds_payload: Optional[Dict[str, Any]],
    mapping: Dict[str, str],
    debug: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    provider_home = base.odds.event_home(event)
    provider_away = base.odds.event_away(event)
    mapped_home = base.canonical_team(provider_home, mapping)
    mapped_away = base.canonical_team(provider_away, mapping)
    canonical_home = mapped_home or provider_home
    canonical_away = mapped_away or provider_away
    fully_mapped = mapped_home is not None and mapped_away is not None

    if not fully_mapped:
        debug.setdefault("providerIdentityTeams", []).append({
            "eventId": base.odds.event_id(event),
            "providerHomeTeam": provider_home,
            "providerAwayTeam": provider_away,
            "homeMapped": mapped_home,
            "awayMapped": mapped_away,
        })

    markets = []
    source = odds_payload or event
    for bookmaker, provider_markets in base.odds.bookmaker_blocks(source):
        for market in provider_markets:
            markets.extend(
                base.odds.normalize_market(
                    market,
                    bookmaker,
                    canonical_home,
                    canonical_away,
                    debug,
                )
            )
    markets = base.odds.dedupe_markets(markets)

    kickoff = base.odds.event_kickoff(event)
    if not markets:
        debug.setdefault("fixturesWithoutExactMarkets", []).append({
            "eventId": base.odds.event_id(event),
            "kickoff": kickoff,
            "providerHomeTeam": provider_home,
            "providerAwayTeam": provider_away,
        })

    return {
        "id": base.odds.event_id(event),
        "date": kickoff[:10],
        "kickoff": kickoff,
        "providerHomeTeam": provider_home,
        "providerAwayTeam": provider_away,
        "homeTeam": canonical_home,
        "awayTeam": canonical_away,
        "canonicalHomeTeam": canonical_home,
        "canonicalAwayTeam": canonical_away,
        "teamMappingStatus": "matched" if fully_mapped else "provider_identity",
        "usableForStats": bool(fully_mapped),
        "markets": markets,
    }


def load_competitions() -> list[Dict[str, Any]]:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    competitions = list(config.get("competitions", []) or [])
    competitions.append({
        "competitionId": "conference_league",
        "leagueCode": "UECL",
        "country": "International",
        "competition": "Conference League",
        "season": "2026-2027",
        "outputPath": "odds/odds_api_io/conference_league_odds.json",
        "reportPath": "reports/conference_league_odds_debug.json",
        "searchTerms": ["uefa conference league", "conference league"],
        "canonicalTeams": [],
        "aliases": {},
    })
    return competitions


def _previous_matches_by_id(previous: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for match in previous.get("matches", []) or []:
        if not isinstance(match, dict):
            continue
        event_id = str(match.get("id") or "").strip()
        if event_id:
            result[event_id] = match
    return result


def refresh_competition_fixture_first(
    api_key: str,
    competition: Dict[str, Any],
    provider_leagues: Sequence[Dict[str, Any]],
    shared_debug: Dict[str, Any],
) -> Dict[str, Any]:
    """Refresh fixtures independently from odds availability.

    The global odds guard may stop expensive odds calls, but it must not suppress
    the one lightweight events call that keeps the UEFA schedule/team universe
    current. If odds are skipped, previous exact markets for the same fixture are
    retained; new fixtures are still published with an empty market list.
    """
    output_path = ROOT / str(competition.get("outputPath"))
    report_path = ROOT / str(competition.get("reportPath"))
    previous = base.read_json(output_path, {})
    debug: Dict[str, Any] = {
        "competitionId": competition.get("competitionId"),
        "leagueCode": competition.get("leagueCode"),
        "warnings": [],
        "apiCalls": shared_debug.setdefault("apiCalls", []),
        "providerRawMarketNames": {},
        "providerRawMarketClassifications": {},
        "providerMarketFamilyCounts": {},
        "supportedProviderMarketCounts": {},
        "auditOnlyMarketCounts": {},
        "auditOnlyMarketExamples": {},
        "unsupportedMarketCounts": {},
        "unsupportedMarketExamples": {},
    }
    if "rateLimitRemaining" in shared_debug:
        debug["rateLimitRemaining"] = shared_debug["rateLimitRemaining"]

    provider = provider_match(competition, provider_leagues)
    fresh_matches: list[Dict[str, Any]] = []
    odds_refresh_skipped = False
    preserved_market_rows = 0

    if provider is None:
        debug["warnings"].append("No strict UEFA provider league match found.")
    else:
        slug = str(provider.get("slug") or "")
        events = base.odds.fetch_events_for_league(
            api_key,
            slug,
            int(base.read_json(CONFIG_PATH, {}).get("horizonDays") or 45),
            shared_debug,
        )
        event_ids = [base.odds.event_id(event) for event in events if base.odds.event_id(event)]

        odds_refresh_skipped = base.odds.should_stop_for_rate_limit(shared_debug)
        if odds_refresh_skipped:
            debug["warnings"].append(
                "Odds rate-limit guard active after fixture refresh; fixtures published without new odds."
            )
            odds_by_event: Dict[str, Dict[str, Any]] = {}
        else:
            odds_by_event = (
                base.odds.fetch_odds(api_key, event_ids, base.BOOKMAKERS, shared_debug)
                if event_ids else {}
            )

        mapping = base.canonical_map(competition)
        previous_by_id = _previous_matches_by_id(previous)
        for event in events:
            event_id = base.odds.event_id(event)
            normalized = normalize_event(
                competition,
                event,
                odds_by_event.get(event_id),
                mapping,
                debug,
            )
            if normalized is None:
                continue

            if odds_refresh_skipped and not normalized.get("markets"):
                previous_match = previous_by_id.get(event_id)
                previous_markets = (
                    previous_match.get("markets", [])
                    if isinstance(previous_match, dict)
                    else []
                )
                if isinstance(previous_markets, list) and previous_markets:
                    normalized["markets"] = previous_markets
                    preserved_market_rows += len(previous_markets)

            fresh_matches.append(normalized)

        debug.update({
            "providerLeague": base.odds.provider_league_summary(provider),
            "eventsFetched": len(events),
            "eventsWithOddsResponse": len(odds_by_event),
            "matchesEmitted": len(fresh_matches),
            "marketsEmitted": sum(len(match.get("markets", [])) for match in fresh_matches),
            "fixtureRefreshAttempted": True,
            "oddsRefreshSkippedByRateLimit": odds_refresh_skipped,
            "preservedPreviousExactMarketRows": preserved_market_rows,
        })

    debug["rateLimitRemaining"] = shared_debug.get("rateLimitRemaining")
    debug.update(base.odds.market_audit_report(debug))
    matches = base.merge_matches(previous.get("matches", []) or [], fresh_matches)
    output = base.output_contract(competition, provider, matches, debug)
    base.write_json(output_path, output)
    base.write_json(report_path, debug)
    return {
        "competitionId": competition.get("competitionId"),
        "leagueCode": competition.get("leagueCode"),
        "providerLeagueSlug": provider.get("slug") if provider else None,
        "freshMatches": len(fresh_matches),
        "publishedMatches": len(matches),
        "publishedMarkets": sum(len(match.get("markets", [])) for match in matches),
        "fixtureRefreshAttempted": provider is not None,
        "oddsRefreshSkippedByRateLimit": odds_refresh_skipped,
        "preservedPreviousExactMarketRows": preserved_market_rows,
        "rateLimitRemaining": shared_debug.get("rateLimitRemaining"),
        "warnings": debug.get("warnings", []),
    }


def main() -> int:
    api_key = os.getenv("ODDS_API_IO_KEY", "").strip()
    if not api_key:
        print("ERROR: ODDS_API_IO_KEY is required", file=sys.stderr)
        return 2

    shared_debug: Dict[str, Any] = {"warnings": [], "apiCalls": []}
    provider_leagues = base.odds.discover_provider_leagues(api_key, shared_debug)
    reports = [
        refresh_competition_fixture_first(api_key, competition, provider_leagues, shared_debug)
        for competition in load_competitions()
    ]
    payload = {
        "generatedAt": base.now_utc(),
        "mode": "build-only qualifier fixture-first feed plus exact odds when quota allows",
        "reports": reports,
        "rateLimitRemaining": shared_debug.get("rateLimitRemaining"),
    }
    base.write_json(ROOT / "reports" / "uefa_qualifier_build_refresh.json", payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
