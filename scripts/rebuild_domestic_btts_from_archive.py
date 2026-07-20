#!/usr/bin/env python3
"""Rebuild canonical full-time BTTS from exact archived Odds-API.io payloads.

This module performs no network calls. For canonical fixtures that have any archived
BTTS-family provider payload, it removes the existing canonical BTTS rows and
rebuilds them only from provider markets classified as full-time BTTS. Period-specific
markets such as first-half or second-half BTTS are never emitted as full-time BTTS.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

import odds_api_io_market_audit as audit
import update_domestic_odds_api_io as base

ROOT = Path(__file__).resolve().parents[1]
ODDS_PATH = ROOT / "odds" / "odds_api_io" / "domestic_odds.json"
ARCHIVE_PATH = ROOT / "odds" / "odds_api_io" / "domestic_provider_markets.json"
BTTS_MARKET = "BTTS"
BTTS_FAMILIES = {"BTTS", "HALF_TIME_BTTS", "SECOND_HALF_BTTS"}


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def archive_key(league_code: str, match: Dict[str, Any]) -> Tuple[str, str, str, str]:
    return (
        league_code,
        str(match.get("id") or ""),
        str(match.get("homeTeam") or "").strip().casefold(),
        str(match.get("awayTeam") or "").strip().casefold(),
    )


def canonical_matches(feed: Dict[str, Any]) -> Dict[Tuple[str, str, str, str], Dict[str, Any]]:
    result: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}
    for league in feed.get("leagues", []) or []:
        code = str(league.get("leagueCode") or "")
        for match in league.get("matches", []) or []:
            result[archive_key(code, match)] = match
    return result


def normalize_archived_full_time_btts(match: Dict[str, Any]) -> Tuple[bool, List[Dict[str, Any]]]:
    """Return (has_any_btts_payload, exact full-time canonical BTTS rows)."""

    out: List[Dict[str, Any]] = []
    saw_btts_payload = False
    home = str(match.get("homeTeam") or "").strip()
    away = str(match.get("awayTeam") or "").strip()
    if not home or not away or match.get("teamMappingStatus") != "matched":
        return False, out

    for payload in match.get("providerMarkets", []) or []:
        if payload.get("exactProviderPayload") is not True:
            continue
        bookmaker = str(payload.get("bookmaker") or "").strip()
        market = payload.get("market")
        if not bookmaker or not isinstance(market, dict):
            continue

        classification = audit.classify_provider_market(audit.provider_market_text(market))
        family = classification.get("family")
        if family in BTTS_FAMILIES:
            saw_btts_payload = True
        if family != BTTS_MARKET or classification.get("status") != "supported":
            continue

        normalized = base.normalize_market(market, bookmaker, home, away, {})
        out.extend(item for item in normalized if item.get("market") == BTTS_MARKET)

    return saw_btts_payload, base.dedupe_markets(out)


def market_key(item: Dict[str, Any]) -> Tuple[str, str, str, str]:
    return (
        str(item.get("market") or ""),
        str(item.get("selection") or ""),
        str(item.get("line") or ""),
        str(item.get("team") or ""),
    )


def replace_exact_btts(existing: Iterable[Dict[str, Any]], rebuilt: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Replace, never merge, BTTS rows so a stale larger 2H price cannot survive."""

    non_btts = [dict(item) for item in existing if item.get("market") != BTTS_MARKET]
    btts = base.dedupe_markets([dict(item) for item in rebuilt])
    return sorted(
        non_btts + btts,
        key=lambda item: (
            str(item.get("market") or ""),
            str(item.get("team") or ""),
            str(item.get("selection") or ""),
            float(item.get("odds") or 0.0),
        ),
    )


def rebuild_feed_btts(feed: Dict[str, Any], archive: Dict[str, Any]) -> Dict[str, Any]:
    matches = canonical_matches(feed)
    archive_matches = 0
    matched_fixtures = 0
    fixtures_rebuilt = 0
    old_btts_rows_removed = 0
    full_time_btts_rows_emitted = 0
    changed_selections = 0
    examples: List[Dict[str, Any]] = []

    for league in archive.get("leagues", []) or []:
        code = str(league.get("leagueCode") or "")
        for archived_match in league.get("matches", []) or []:
            archive_matches += 1
            target = matches.get(archive_key(code, archived_match))
            if target is None:
                continue
            matched_fixtures += 1

            saw_btts_payload, rebuilt = normalize_archived_full_time_btts(archived_match)
            if not saw_btts_payload:
                continue

            before_markets = list(target.get("markets", []) or [])
            before_btts = [dict(item) for item in before_markets if item.get("market") == BTTS_MARKET]
            target["markets"] = replace_exact_btts(before_markets, rebuilt)
            after_btts = [dict(item) for item in target.get("markets", []) or [] if item.get("market") == BTTS_MARKET]

            old_btts_rows_removed += len(before_btts)
            full_time_btts_rows_emitted += len(after_btts)
            if before_btts != after_btts:
                fixtures_rebuilt += 1
                before_by_key = {market_key(item): item for item in before_btts}
                after_by_key = {market_key(item): item for item in after_btts}
                changed_selections += sum(
                    1
                    for key in set(before_by_key) | set(after_by_key)
                    if before_by_key.get(key) != after_by_key.get(key)
                )
                if len(examples) < 12:
                    examples.append(
                        {
                            "leagueCode": code,
                            "fixture": f"{target.get('homeTeam')} - {target.get('awayTeam')}",
                            "before": before_btts,
                            "after": after_btts,
                        }
                    )

    total_btts_rows = sum(
        1
        for league in feed.get("leagues", []) or []
        for match in league.get("matches", []) or []
        for market in match.get("markets", []) or []
        if market.get("market") == BTTS_MARKET
    )
    summary = {
        "source": "exact archived Odds-API.io provider payloads",
        "syntheticOdds": False,
        "policy": "replace canonical BTTS only from full-time BTTS provider markets; reject HT/2H BTTS",
        "archiveMatchesScanned": archive_matches,
        "canonicalFixturesMatched": matched_fixtures,
        "fixturesRebuilt": fixtures_rebuilt,
        "oldBttsRowsRemoved": old_btts_rows_removed,
        "fullTimeBttsRowsEmitted": full_time_btts_rows_emitted,
        "changedSelections": changed_selections,
        "totalCanonicalBttsSelections": total_btts_rows,
        "examples": examples,
    }
    debug = feed.setdefault("debug", {})
    debug["bttsArchiveRebuild"] = summary
    debug["emittedMarketCounts"] = base.emitted_market_counts(feed)
    return summary


def _self_check() -> None:
    feed = {
        "leagues": [
            {
                "leagueCode": "FIN",
                "matches": [
                    {
                        "id": "fixture-1",
                        "homeTeam": "Turku PS",
                        "awayTeam": "Ilves",
                        "markets": [
                            {"market": "BTTS", "selection": "Yes", "odds": 2.75, "bookmaker": "Bet365", "confidence": "high", "exactBookmakerOdds": True},
                            {"market": "BTTS", "selection": "No", "odds": 1.40, "bookmaker": "Bet365", "confidence": "high", "exactBookmakerOdds": True},
                            {"market": "1X2", "selection": "Home", "odds": 3.10, "bookmaker": "Bet365", "confidence": "high", "exactBookmakerOdds": True},
                        ],
                    }
                ],
            }
        ]
    }
    archive = {
        "leagues": [
            {
                "leagueCode": "FIN",
                "matches": [
                    {
                        "id": "fixture-1",
                        "homeTeam": "Turku PS",
                        "awayTeam": "Ilves",
                        "teamMappingStatus": "matched",
                        "providerMarkets": [
                            {
                                "bookmaker": "Bet365",
                                "exactProviderPayload": True,
                                "market": {"name": "Both Teams To Score", "odds": [{"yes": "1.57", "no": "2.25"}]},
                            },
                            {
                                "bookmaker": "Bet365",
                                "exactProviderPayload": True,
                                "market": {"name": "Both Teams To Score 2H", "odds": [{"yes": "2.75", "no": "1.40"}]},
                            },
                        ],
                    }
                ],
            }
        ]
    }
    rebuild_feed_btts(feed, archive)
    markets = feed["leagues"][0]["matches"][0]["markets"]
    btts = {(row["selection"], row["odds"]) for row in markets if row["market"] == "BTTS"}
    assert btts == {("Yes", 1.57), ("No", 2.25)}, btts
    assert any(row["market"] == "1X2" and row["odds"] == 3.10 for row in markets)
    assert all(row["odds"] != 2.75 for row in markets if row["market"] == "BTTS")


def main() -> int:
    _self_check()
    feed = read_json(ODDS_PATH)
    archive = read_json(ARCHIVE_PATH)
    report = rebuild_feed_btts(feed, archive)
    write_json(ODDS_PATH, feed)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
