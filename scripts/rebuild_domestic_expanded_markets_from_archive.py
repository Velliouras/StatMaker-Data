#!/usr/bin/env python3
"""Rebuild expanded canonical markets from the exact provider archive.

No API call is made. Existing Odds-API.io payloads are re-normalized and merged
into ``domestic_odds.json``. Prices are copied only from exact provider rows.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import domestic_live_july_pipeline as pipeline
import domestic_market_expansion_v15 as expansion
import domestic_odds_expansion
import update_domestic_odds_api_io as odds

ROOT = Path(__file__).resolve().parents[1]
FEED_PATH = ROOT / "odds" / "odds_api_io" / "domestic_odds.json"
ARCHIVE_PATH = ROOT / "odds" / "odds_api_io" / "domestic_provider_markets.json"
REPORT_PATH = ROOT / "reports" / "domestic_expanded_market_rebuild.json"


def _normalized(value: Any) -> str:
    return odds.normalize_text(value or "", drop_suffixes=True)


def _match_key(match: Dict[str, Any]) -> Tuple[str, str, str]:
    return (
        str(match.get("date") or match.get("kickoff") or "")[:10],
        _normalized(match.get("homeTeam") or match.get("providerHomeTeam")),
        _normalized(match.get("awayTeam") or match.get("providerAwayTeam")),
    )


def _market_key(row: Dict[str, Any]) -> Tuple[Any, ...]:
    return (
        row.get("market"),
        row.get("selection"),
        row.get("bookmaker"),
        row.get("line"),
        row.get("team"),
        row.get("odds"),
    )


def _dedupe(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    seen = set()
    for row in rows:
        key = _market_key(row)
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    return result


def rebuild_feed_markets(
    feed: Dict[str, Any],
    archive: Dict[str, Any],
    odds_module: Any = odds,
) -> Dict[str, Any]:
    """Mutate ``feed`` by replacing only v15-expanded families from archive."""
    domestic_odds_expansion.install(odds_module, pipeline)
    expansion.install(odds_module, pipeline)

    debug: Dict[str, Any] = {"warnings": []}
    archive_leagues = {
        str(league.get("leagueCode") or ""): league
        for league in archive.get("leagues", []) or []
        if isinstance(league, dict)
    }
    report: Dict[str, Any] = {
        "version": expansion.VERSION,
        "generatedAt": pipeline.now_utc(),
        "syntheticOdds": False,
        "leaguesVisited": 0,
        "matchesVisited": 0,
        "matchesWithArchive": 0,
        "providerPayloadsVisited": 0,
        "expandedSelections": 0,
        "marketsRemovedBeforeRebuild": 0,
        "families": {},
        "missingArchiveMatches": 0,
        "changed": False,
    }

    for league in feed.get("leagues", []) or []:
        if not isinstance(league, dict):
            continue
        report["leaguesVisited"] += 1
        code = str(league.get("leagueCode") or "")
        archive_league = archive_leagues.get(code) or {}
        archive_matches = [row for row in archive_league.get("matches", []) or [] if isinstance(row, dict)]
        by_id = {str(row.get("id") or ""): row for row in archive_matches if str(row.get("id") or "")}
        by_key = {_match_key(row): row for row in archive_matches}

        for match in league.get("matches", []) or []:
            if not isinstance(match, dict):
                continue
            report["matchesVisited"] += 1
            archive_match = by_id.get(str(match.get("id") or "")) or by_key.get(_match_key(match))
            if not archive_match:
                report["missingArchiveMatches"] += 1
                continue
            report["matchesWithArchive"] += 1
            home = str(match.get("homeTeam") or archive_match.get("homeTeam") or "").strip()
            away = str(match.get("awayTeam") or archive_match.get("awayTeam") or "").strip()
            if not home or not away or match.get("usableForStats") is not True:
                continue

            current = [row for row in match.get("markets", []) or [] if isinstance(row, dict)]
            kept = [row for row in current if str(row.get("market") or "") not in expansion.NEW_MARKETS]
            removed = len(current) - len(kept)
            rebuilt: List[Dict[str, Any]] = []
            for payload in archive_match.get("providerMarkets", []) or []:
                if not isinstance(payload, dict) or payload.get("exactProviderPayload") is not True:
                    continue
                raw_market = payload.get("market")
                bookmaker = str(payload.get("bookmaker") or "").strip()
                if not isinstance(raw_market, dict) or not bookmaker:
                    continue
                report["providerPayloadsVisited"] += 1
                normalized = odds_module.normalize_market(raw_market, bookmaker, home, away, debug)
                for row in normalized:
                    if str(row.get("market") or "") not in expansion.NEW_MARKETS:
                        continue
                    if row.get("exactBookmakerOdds") is not True:
                        continue
                    rebuilt.append(row)

            rebuilt = _dedupe(rebuilt)
            if removed or rebuilt:
                merged = _dedupe(kept + rebuilt)
                if merged != current:
                    report["changed"] = True
                match["markets"] = merged
            report["marketsRemovedBeforeRebuild"] += removed
            report["expandedSelections"] += len(rebuilt)
            for row in rebuilt:
                family = str(row.get("market") or "")
                report["families"][family] = int(report["families"].get(family, 0)) + 1

    report["families"] = dict(sorted(report["families"].items()))
    report["normalizationWarnings"] = debug.get("warnings", [])
    report["expandedExactMarketCounts"] = debug.get("expandedExactMarketCounts", {})
    feed.setdefault("debug", {})["expandedMarketArchiveRebuild"] = report
    if hasattr(odds_module, "emitted_market_counts"):
        feed.setdefault("debug", {})["emittedMarketCounts"] = odds_module.emitted_market_counts(feed)
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rebuild expanded Domestic markets from exact provider archive")
    parser.add_argument("--feed", type=Path, default=FEED_PATH)
    parser.add_argument("--archive", type=Path, default=ARCHIVE_PATH)
    parser.add_argument("--report", type=Path, default=REPORT_PATH)
    parser.add_argument("--check", action="store_true", help="Validate and report without writing the feed")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.feed.exists() or not args.archive.exists():
        print("No Domestic feed/provider archive found; files were installed but no local rebuild was run.")
        return 0
    feed = json.loads(args.feed.read_text(encoding="utf-8-sig"))
    archive = json.loads(args.archive.read_text(encoding="utf-8-sig"))
    report = rebuild_feed_markets(feed, archive, odds)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not args.check:
        args.feed.write_text(json.dumps(feed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
