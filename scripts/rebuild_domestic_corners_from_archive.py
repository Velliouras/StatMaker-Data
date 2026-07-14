#!/usr/bin/env python3
"""Rebuild canonical integer corner totals from exact archived provider payloads.

This module performs no network calls. It reads exact provider archive payloads,
normalizes only full-time match/team corner total markets with the push-aware
corner policy, and merges those selections into matching canonical Domestic
fixtures. The reusable ``rebuild_feed_corners`` entry point is also used by the
rotating Domestic refresh so a fresh league replacement cannot silently remove
real corner markets that are still present in the provider archive.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

import update_domestic_odds_api_io as base
import update_domestic_odds_api_io_push_aware as push_aware

ROOT = Path(__file__).resolve().parents[1]
ODDS_PATH = ROOT / "odds" / "odds_api_io" / "domestic_odds.json"
ARCHIVE_PATH = ROOT / "odds" / "odds_api_io" / "domestic_provider_markets.json"
REPORT_PATH = ROOT / "reports" / "domestic_corner_archive_rebuild.json"
CORNER_MARKETS = {"MATCH_CORNERS", "TEAM_CORNERS"}


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def normalized_text(value: Any) -> str:
    return base.normalize_text(str(value or ""))


def is_supported_full_time_corner_market(raw_name: str) -> bool:
    name = normalized_text(raw_name)
    if "corner" not in name:
        return False
    excluded = ("half", " ht", "1h", "2h", "race", "spread", "handicap")
    return not any(token in f" {name}" for token in excluded)


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


def normalize_archived_corners(match: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    home = str(match.get("homeTeam") or "").strip()
    away = str(match.get("awayTeam") or "").strip()
    if not home or not away or match.get("teamMappingStatus") != "matched":
        return out

    for payload in match.get("providerMarkets", []) or []:
        if payload.get("exactProviderPayload") is not True:
            continue
        bookmaker = str(payload.get("bookmaker") or "").strip()
        market = payload.get("market")
        if not bookmaker or not isinstance(market, dict):
            continue
        raw_name = base.raw_market_name(market)
        if not is_supported_full_time_corner_market(raw_name):
            continue
        normalized = push_aware._normalize_market_with_integer_corners(
            market,
            bookmaker,
            home,
            away,
            {},
        )
        out.extend(item for item in normalized if item.get("market") in CORNER_MARKETS)
    return base.dedupe_markets(out)


def market_key(item: Dict[str, Any]) -> Tuple[str, str, str, str]:
    return (
        str(item.get("market") or ""),
        str(item.get("selection") or ""),
        str(item.get("line") or ""),
        str(item.get("team") or ""),
    )


def merge_exact_corners(existing: Iterable[Dict[str, Any]], rebuilt: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    non_corners = [dict(item) for item in existing if item.get("market") not in CORNER_MARKETS]
    corners = base.dedupe_markets(
        [dict(item) for item in existing if item.get("market") in CORNER_MARKETS]
        + [dict(item) for item in rebuilt]
    )
    return sorted(
        non_corners + corners,
        key=lambda item: (
            str(item.get("market") or ""),
            str(item.get("team") or ""),
            str(item.get("selection") or ""),
            float(item.get("odds") or 0.0),
        ),
    )


def rebuild_feed_corners(
    feed: Dict[str, Any],
    archive: Dict[str, Any],
    *,
    require_corners: bool = True,
) -> Dict[str, Any]:
    """Merge exact archived corner prices into an in-memory canonical feed.

    ``require_corners`` remains strict for the dedicated manual rebuild command.
    The rotating live refresh uses ``False`` so a provider cycle with genuinely no
    corner payloads is reported as zero rather than replaced with synthetic data.
    """

    push_aware._self_check()
    matches = canonical_matches(feed)

    archive_matches = 0
    matched_fixtures = 0
    added_or_replaced = 0
    emitted_by_market = {"MATCH_CORNERS": 0, "TEAM_CORNERS": 0}
    examples: List[Dict[str, Any]] = []

    for league in archive.get("leagues", []) or []:
        code = str(league.get("leagueCode") or "")
        for archived_match in league.get("matches", []) or []:
            archive_matches += 1
            target = matches.get(archive_key(code, archived_match))
            if target is None:
                continue
            rebuilt = normalize_archived_corners(archived_match)
            if not rebuilt:
                continue
            matched_fixtures += 1
            before = {market_key(item): item for item in target.get("markets", []) or []}
            target["markets"] = merge_exact_corners(target.get("markets", []) or [], rebuilt)
            after = {market_key(item): item for item in target.get("markets", []) or []}
            changed_keys = {
                key
                for key, item in after.items()
                if key not in before or before[key] != item
            }
            added_or_replaced += len(changed_keys)
            for item in rebuilt:
                emitted_by_market[str(item.get("market"))] += 1
            if len(examples) < 8:
                examples.append(
                    {
                        "leagueCode": code,
                        "fixture": f"{target.get('homeTeam')} - {target.get('awayTeam')}",
                        "corners": rebuilt[:6],
                    }
                )

    total_corner_rows = sum(
        1
        for league in feed.get("leagues", []) or []
        for match in league.get("matches", []) or []
        for market in match.get("markets", []) or []
        if market.get("market") in CORNER_MARKETS
    )
    if require_corners and total_corner_rows <= 0:
        raise RuntimeError(
            "Provider archive rebuild emitted zero canonical corner markets; refusing to write feed"
        )

    summary = {
        "source": "exact archived Odds-API.io provider payloads",
        "syntheticOdds": False,
        "archiveMatchesScanned": archive_matches,
        "canonicalFixturesMatched": matched_fixtures,
        "addedOrReplacedSelections": added_or_replaced,
        "totalCanonicalCornerSelections": total_corner_rows,
        "rebuiltByMarket": emitted_by_market,
    }
    debug = feed.setdefault("debug", {})
    debug["cornerArchiveRebuild"] = summary
    debug["emittedMarketCounts"] = base.emitted_market_counts(feed)

    return {**summary, "examples": examples}


def main() -> int:
    feed = read_json(ODDS_PATH)
    archive = read_json(ARCHIVE_PATH)
    report = rebuild_feed_corners(feed, archive, require_corners=True)
    write_json(ODDS_PATH, feed)
    write_json(REPORT_PATH, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
