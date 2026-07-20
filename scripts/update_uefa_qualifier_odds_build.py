#!/usr/bin/env python3
"""Build-only UEFA qualifier odds feed for CL/EL/Conference.

Keeps exact bookmaker odds even when a qualifier participant is not yet in the
static canonical registry. Unknown teams are emitted as provider_identity with
usableForStats=false so the Android app can promote them only after verified
history readiness succeeds.
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
    if not markets:
        return None

    kickoff = base.odds.event_kickoff(event)
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


def main() -> int:
    api_key = os.getenv("ODDS_API_IO_KEY", "").strip()
    if not api_key:
        print("ERROR: ODDS_API_IO_KEY is required", file=sys.stderr)
        return 2

    base.competition_provider_match = provider_match
    base.normalize_event = normalize_event

    shared_debug: Dict[str, Any] = {"warnings": [], "apiCalls": []}
    provider_leagues = base.odds.discover_provider_leagues(api_key, shared_debug)
    reports = [base.refresh_competition(api_key, competition, provider_leagues, shared_debug) for competition in load_competitions()]
    payload = {
        "generatedAt": base.now_utc(),
        "mode": "build-only qualifier provider identity",
        "reports": reports,
        "rateLimitRemaining": shared_debug.get("rateLimitRemaining"),
    }
    base.write_json(ROOT / "reports" / "uefa_qualifier_build_refresh.json", payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
