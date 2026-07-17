#!/usr/bin/env python3
"""Remove mixed-period corner rows and rebuild full-time corners only.

The provider archive contains the raw market name. The canonical feed does not,
so once a 1H/2H corner market has been normalized as MATCH_CORNERS it cannot be
reliably relabelled inside Android. This cleanup replaces every canonical corner
ladder for archived fixtures with rows rebuilt only from raw full-time corner
markets. No synthetic price or transformed line is created.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

import rebuild_domestic_corners_from_archive as corner_rebuild

ROOT = Path(__file__).resolve().parents[1]
ODDS_PATH = ROOT / "odds" / "odds_api_io" / "domestic_odds.json"
ARCHIVE_PATH = ROOT / "odds" / "odds_api_io" / "domestic_provider_markets.json"
REPORT_PATH = ROOT / "reports" / "domestic_corner_scope_cleanup.json"
CORNER_MARKETS = {"MATCH_CORNERS", "TEAM_CORNERS"}


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def match_key(league_code: str, match: Dict[str, Any]) -> Tuple[str, str, str, str]:
    return corner_rebuild.archive_key(league_code, match)


def market_sort_key(item: Dict[str, Any]) -> Tuple[str, str, str, float]:
    return (
        str(item.get("market") or ""),
        str(item.get("team") or ""),
        str(item.get("selection") or ""),
        float(item.get("odds") or 0.0),
    )


def clean_feed(feed: Dict[str, Any], archive: Dict[str, Any]) -> Dict[str, Any]:
    archived: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}
    for league in archive.get("leagues", []) or []:
        code = str(league.get("leagueCode") or "")
        for match in league.get("matches", []) or []:
            archived[match_key(code, match)] = match

    fixtures_checked = 0
    fixtures_rebuilt = 0
    fixtures_cleared = 0
    removed_rows = 0
    rebuilt_rows = 0
    examples: List[Dict[str, Any]] = []

    for league in feed.get("leagues", []) or []:
        code = str(league.get("leagueCode") or "")
        for match in league.get("matches", []) or []:
            fixtures_checked += 1
            archived_match = archived.get(match_key(code, match))
            if archived_match is None:
                continue

            existing = list(match.get("markets", []) or [])
            existing_corners = [row for row in existing if row.get("market") in CORNER_MARKETS]
            clean_corners = corner_rebuild.normalize_archived_corners(archived_match)
            non_corners = [dict(row) for row in existing if row.get("market") not in CORNER_MARKETS]

            # Replace, never merge, because canonical rows no longer retain the raw
            # provider period and therefore cannot prove they are full-time.
            rebuilt = corner_rebuild.base.dedupe_markets(clean_corners)
            match["markets"] = sorted(non_corners + rebuilt, key=market_sort_key)

            if existing_corners or rebuilt:
                fixtures_rebuilt += 1
            if existing_corners and not rebuilt:
                fixtures_cleared += 1
            removed_rows += len(existing_corners)
            rebuilt_rows += len(rebuilt)

            if len(examples) < 10 and existing_corners != rebuilt:
                examples.append(
                    {
                        "leagueCode": code,
                        "fixture": f"{match.get('homeTeam')} - {match.get('awayTeam')}",
                        "removedCanonicalCorners": existing_corners[:8],
                        "rebuiltFullTimeCorners": rebuilt[:8],
                    }
                )

    summary = {
        "source": "exact archived Odds-API.io provider payloads",
        "policy": "canonical corners are replaced by raw-name-verified full-time markets only",
        "syntheticOdds": False,
        "fixturesChecked": fixtures_checked,
        "fixturesRebuilt": fixtures_rebuilt,
        "fixturesClearedWithoutFullTimeCornerMarket": fixtures_cleared,
        "removedCanonicalCornerRows": removed_rows,
        "rebuiltFullTimeCornerRows": rebuilt_rows,
    }
    feed.setdefault("debug", {})["cornerScopeCleanup"] = summary
    feed["debug"]["emittedMarketCounts"] = corner_rebuild.base.emitted_market_counts(feed)
    return {**summary, "examples": examples}


def main() -> int:
    feed = read_json(ODDS_PATH)
    archive = read_json(ARCHIVE_PATH)
    report = clean_feed(feed, archive)
    write_json(ODDS_PATH, feed)
    write_json(REPORT_PATH, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
