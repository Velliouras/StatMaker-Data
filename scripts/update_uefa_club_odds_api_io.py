#!/usr/bin/env python3
"""Refresh exact Champions League and Europa League odds artifacts.

The script makes one Odds-API.io league-discovery call, finds the two UEFA club
competitions, fetches pending/live events and exact bookmaker markets, and writes
repository JSON consumed by the Android app. Events are emitted only when both
teams map to the verified historical team registry. Existing future events are
preserved when a rate-limited run cannot refresh them.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import domestic_live_july_pipeline as domestic_pipeline
import domestic_odds_expansion
import update_domestic_odds_api_io as odds

domestic_odds_expansion.install(odds, domestic_pipeline)

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "uefa_club_competitions.json"
BOOKMAKERS = os.getenv("ODDS_API_IO_BOOKMAKERS", odds.DEFAULT_BOOKMAKERS)


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def today_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).date().isoformat()


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def provider_text(item: Dict[str, Any]) -> str:
    return odds.normalize_text(f"{item.get('name', '')} {item.get('slug', '')}")


def competition_provider_match(
    competition: Dict[str, Any],
    provider_leagues: Sequence[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    terms = [odds.normalize_text(term) for term in competition.get("searchTerms", []) if str(term).strip()]
    target_code = str(competition.get("leagueCode") or "").upper()
    blocked = {"women", "womens", "woman", "youth", "u19", "u21", "conference"}
    scored: List[Tuple[int, Dict[str, Any]]] = []
    for item in provider_leagues:
        text = provider_text(item)
        words = set(text.split())
        if words & blocked:
            continue
        if target_code == "CL" and "europa" in words:
            continue
        if target_code == "EL" and "champions" in words:
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
            scored.append((best, item))
    if not scored:
        return None
    scored.sort(key=lambda pair: (pair[0], int(pair[1].get("eventsCount") or 0)), reverse=True)
    return scored[0][1]


def simplified_team_name(value: Any) -> str:
    return domestic_odds_expansion.simplified_team_name(odds, value)


def canonical_map(competition: Dict[str, Any]) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    for canonical in competition.get("canonicalTeams", []) or []:
        name = str(canonical or "").strip()
        if name:
            mapping[odds.normalize_text(name, drop_suffixes=True)] = name
            mapping[simplified_team_name(name)] = name
    for alias, canonical in (competition.get("aliases") or {}).items():
        canonical_name = str(canonical or "").strip()
        alias_name = str(alias or "").strip()
        if alias_name and canonical_name:
            mapping[odds.normalize_text(alias_name, drop_suffixes=True)] = canonical_name
            mapping[simplified_team_name(alias_name)] = canonical_name
    return {key: value for key, value in mapping.items() if key}


def canonical_team(name: str, mapping: Dict[str, str]) -> Optional[str]:
    candidates = [
        odds.normalize_text(name, drop_suffixes=True),
        simplified_team_name(name),
    ]
    for candidate in dict.fromkeys(candidates):
        if candidate in mapping:
            return mapping[candidate]
    return None


def normalize_event(
    competition: Dict[str, Any],
    event: Dict[str, Any],
    odds_payload: Optional[Dict[str, Any]],
    mapping: Dict[str, str],
    debug: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    provider_home = odds.event_home(event)
    provider_away = odds.event_away(event)
    canonical_home = canonical_team(provider_home, mapping)
    canonical_away = canonical_team(provider_away, mapping)
    if canonical_home is None or canonical_away is None:
        debug.setdefault("unmatchedTeams", []).append({
            "eventId": odds.event_id(event),
            "providerHomeTeam": provider_home,
            "providerAwayTeam": provider_away,
            "homeMapped": canonical_home,
            "awayMapped": canonical_away,
        })
        return None

    markets: List[Dict[str, Any]] = []
    source = odds_payload or event
    for bookmaker, provider_markets in odds.bookmaker_blocks(source):
        for market in provider_markets:
            markets.extend(
                odds.normalize_market(
                    market,
                    bookmaker,
                    canonical_home,
                    canonical_away,
                    debug,
                )
            )
    markets = odds.dedupe_markets(markets)
    if not markets:
        return None

    kickoff = odds.event_kickoff(event)
    return {
        "id": odds.event_id(event),
        "date": kickoff[:10],
        "kickoff": kickoff,
        "providerHomeTeam": provider_home,
        "providerAwayTeam": provider_away,
        "homeTeam": canonical_home,
        "awayTeam": canonical_away,
        "canonicalHomeTeam": canonical_home,
        "canonicalAwayTeam": canonical_away,
        "teamMappingStatus": "matched",
        "usableForStats": True,
        "markets": markets,
    }


def merge_matches(previous: Iterable[Dict[str, Any]], fresh: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    today = today_iso()
    merged: Dict[str, Dict[str, Any]] = {}
    for match in previous:
        if not isinstance(match, dict) or str(match.get("date") or "") < today:
            continue
        key = str(match.get("id") or "").strip() or "|".join(
            str(match.get(field) or "") for field in ("date", "homeTeam", "awayTeam")
        )
        merged[key] = match
    for match in fresh:
        key = str(match.get("id") or "").strip() or "|".join(
            str(match.get(field) or "") for field in ("date", "homeTeam", "awayTeam")
        )
        merged[key] = match
    return sorted(
        merged.values(),
        key=lambda match: (
            str(match.get("date") or ""),
            str(match.get("kickoff") or ""),
            str(match.get("homeTeam") or ""),
        ),
    )


def output_contract(
    competition: Dict[str, Any],
    provider: Optional[Dict[str, Any]],
    matches: List[Dict[str, Any]],
    debug: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "schemaVersion": 1,
        "source": "odds-api-io",
        "provider": "Odds-API.io",
        "generatedAt": now_utc(),
        "country": competition.get("country") or "International",
        "competition": competition.get("competition"),
        "leagueCode": competition.get("leagueCode"),
        "season": competition.get("season"),
        "providerLeagueSlug": provider.get("slug") if provider else None,
        "bookmakersRequested": [value.strip() for value in BOOKMAKERS.split(",") if value.strip()],
        "dataContract": {
            "statsSource": "verified repository historical results",
            "oddsSource": "Odds-API.io exact bookmaker odds",
            "appRule": "The Android app reads repository JSON only.",
            "bettingGate": "Both canonical team mapping and exact bookmaker odds are required.",
            "emptyState": "Δεν βρέθηκαν αγορές",
        },
        "matches": matches,
        "debug": debug,
    }


def refresh_competition(
    api_key: str,
    competition: Dict[str, Any],
    provider_leagues: Sequence[Dict[str, Any]],
    shared_debug: Dict[str, Any],
) -> Dict[str, Any]:
    output_path = ROOT / str(competition.get("outputPath"))
    report_path = ROOT / str(competition.get("reportPath"))
    previous = read_json(output_path, {})
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

    provider = competition_provider_match(competition, provider_leagues)
    fresh_matches: List[Dict[str, Any]] = []
    if provider is None:
        debug["warnings"].append("No strict UEFA provider league match found.")
    elif odds.should_stop_for_rate_limit(shared_debug):
        debug["warnings"].append("Rate limit guard active before events fetch; previous future matches preserved.")
    else:
        slug = str(provider.get("slug") or "")
        events = odds.fetch_events_for_league(
            api_key,
            slug,
            int(read_json(CONFIG_PATH, {}).get("horizonDays") or 45),
            shared_debug,
        )
        event_ids = [odds.event_id(event) for event in events if odds.event_id(event)]
        odds_by_event = odds.fetch_odds(api_key, event_ids, BOOKMAKERS, shared_debug) if event_ids else {}
        mapping = canonical_map(competition)
        for event in events:
            normalized = normalize_event(
                competition,
                event,
                odds_by_event.get(odds.event_id(event)),
                mapping,
                debug,
            )
            if normalized is not None:
                fresh_matches.append(normalized)
        debug.update({
            "providerLeague": odds.provider_league_summary(provider),
            "eventsFetched": len(events),
            "eventsWithOddsResponse": len(odds_by_event),
            "matchesEmitted": len(fresh_matches),
            "marketsEmitted": sum(len(match.get("markets", [])) for match in fresh_matches),
        })

    debug["rateLimitRemaining"] = shared_debug.get("rateLimitRemaining")
    debug.update(odds.market_audit_report(debug))
    matches = merge_matches(previous.get("matches", []) or [], fresh_matches)
    output = output_contract(competition, provider, matches, debug)
    write_json(output_path, output)
    write_json(report_path, debug)
    return {
        "competitionId": competition.get("competitionId"),
        "leagueCode": competition.get("leagueCode"),
        "providerLeagueSlug": provider.get("slug") if provider else None,
        "freshMatches": len(fresh_matches),
        "publishedMatches": len(matches),
        "publishedMarkets": sum(len(match.get("markets", [])) for match in matches),
        "rateLimitRemaining": shared_debug.get("rateLimitRemaining"),
        "warnings": debug.get("warnings", []),
    }


def main() -> int:
    api_key = os.getenv("ODDS_API_IO_KEY", "").strip()
    if not api_key:
        print("ERROR: ODDS_API_IO_KEY is required.", file=sys.stderr)
        return 2
    config = read_json(CONFIG_PATH, {})
    competitions = config.get("competitions", []) if isinstance(config, dict) else []
    if not competitions:
        print("ERROR: UEFA club competition config is empty.", file=sys.stderr)
        return 3

    shared_debug: Dict[str, Any] = {"warnings": [], "apiCalls": []}
    provider_leagues = odds.discover_provider_leagues(api_key, shared_debug)
    reports = [
        refresh_competition(api_key, competition, provider_leagues, shared_debug)
        for competition in competitions
    ]
    report = {
        "generatedAt": now_utc(),
        "competitionCount": len(reports),
        "rateLimitRemaining": shared_debug.get("rateLimitRemaining"),
        "reports": reports,
    }
    write_json(ROOT / "reports" / "uefa_club_odds_refresh.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
