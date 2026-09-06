#!/usr/bin/env python3
"""Rebuild canonical corner totals from exact archived provider payloads.

This module performs no network calls. It treats the raw provider archive as the
only authority for canonical MATCH_CORNERS / TEAM_CORNERS identity. Canonical
corner rows are replaced, never merged, so a previously misclassified 1H/2H or
specialty corner market cannot survive once its raw provider scope is available.
If a canonical fixture has no matching raw provider archive entry, corner rows are
removed rather than trusted without provenance. No synthetic price or transformed
line is created.
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
    """Return True only when the raw provider name can safely represent FT corners.

    The base Odds-API.io parser intentionally has broad family detection. That is
    useful for discovery, but canonical publishing must be stricter because the
    canonical row no longer retains the provider period/scope. Any explicit
    non-full-time or specialty scope therefore fails closed here.
    """

    name = normalized_text(raw_name)
    if "corner" not in name:
        return False
    padded = f" {name} "
    excluded = (
        " half",
        " ht ",
        " 1h ",
        " 2h ",
        " 1st ",
        " 2nd ",
        " period",
        " race",
        " spread",
        " handicap",
        " player",
        " first corner",
        " last corner",
    )
    return not any(token in padded for token in excluded)


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


def archived_matches(archive: Dict[str, Any]) -> Dict[Tuple[str, str, str, str], Dict[str, Any]]:
    result: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}
    for league in archive.get("leagues", []) or []:
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


def replace_exact_corners(existing: Iterable[Dict[str, Any]], rebuilt: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Replace the entire canonical corner ladder with raw-verified FT rows only."""

    non_corners = [dict(item) for item in existing if item.get("market") not in CORNER_MARKETS]
    corners = base.dedupe_markets([dict(item) for item in rebuilt])
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
    """Rebuild canonical corners from raw provider provenance, failing closed.

    ``require_corners`` remains strict for the dedicated manual rebuild command.
    Rotating/production cleanup uses ``False`` so a provider snapshot with genuinely
    no verified FT corner payloads removes unproven corner rows instead of creating
    synthetic or stale fallback data.
    """

    push_aware._self_check()
    archive_by_key = archived_matches(archive)

    archive_matches_scanned = len(archive_by_key)
    canonical_fixtures_checked = 0
    canonical_fixtures_matched = 0
    canonical_fixtures_missing_archive = 0
    fixtures_rebuilt = 0
    fixtures_cleared = 0
    removed_canonical_rows = 0
    rebuilt_rows = 0
    changed_selections = 0
    emitted_by_market = {"MATCH_CORNERS": 0, "TEAM_CORNERS": 0}
    examples: List[Dict[str, Any]] = []

    for league in feed.get("leagues", []) or []:
        code = str(league.get("leagueCode") or "")
        for target in league.get("matches", []) or []:
            canonical_fixtures_checked += 1
            archived_match = archive_by_key.get(archive_key(code, target))
            if archived_match is None:
                canonical_fixtures_missing_archive += 1
                rebuilt: List[Dict[str, Any]] = []
            else:
                canonical_fixtures_matched += 1
                rebuilt = normalize_archived_corners(archived_match)

            existing = list(target.get("markets", []) or [])
            existing_corners = [item for item in existing if item.get("market") in CORNER_MARKETS]
            before = {market_key(item): item for item in existing_corners}
            target["markets"] = replace_exact_corners(existing, rebuilt)
            after = {market_key(item): item for item in rebuilt}

            changed_keys = {
                key
                for key in set(before) | set(after)
                if before.get(key) != after.get(key)
            }
            changed_selections += len(changed_keys)
            removed_canonical_rows += len(existing_corners)
            rebuilt_rows += len(rebuilt)

            if existing_corners or rebuilt:
                fixtures_rebuilt += 1
            if existing_corners and not rebuilt:
                fixtures_cleared += 1

            for item in rebuilt:
                emitted_by_market[str(item.get("market"))] += 1

            if len(examples) < 10 and changed_keys:
                examples.append(
                    {
                        "leagueCode": code,
                        "fixture": f"{target.get('homeTeam')} - {target.get('awayTeam')}",
                        "providerArchivePresent": archived_match is not None,
                        "removedCanonicalCorners": existing_corners[:8],
                        "rebuiltFullTimeCorners": rebuilt[:8],
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
            "Provider archive rebuild emitted zero verified full-time canonical corner markets; refusing to write feed"
        )

    summary = {
        "source": "exact archived Odds-API.io provider payloads",
        "policy": "replace canonical corners from raw-name-verified full-time provider markets; fail closed without provenance",
        "syntheticOdds": False,
        "archiveMatchesScanned": archive_matches_scanned,
        "canonicalFixturesChecked": canonical_fixtures_checked,
        "canonicalFixturesMatched": canonical_fixtures_matched,
        "canonicalFixturesMissingProviderArchive": canonical_fixtures_missing_archive,
        "fixturesRebuilt": fixtures_rebuilt,
        "fixturesClearedWithoutVerifiedFullTimeCorners": fixtures_cleared,
        "removedCanonicalCornerSelections": removed_canonical_rows,
        "rebuiltFullTimeCornerSelections": rebuilt_rows,
        "addedOrReplacedSelections": changed_selections,
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
