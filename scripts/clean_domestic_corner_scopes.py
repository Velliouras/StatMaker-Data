#!/usr/bin/env python3
"""Enforce raw-provider full-time scope for canonical Domestic corner markets.

The canonical betting feed does not retain the provider's raw market period/scope,
so canonical MATCH_CORNERS / TEAM_CORNERS rows cannot be trusted once that scope
has been lost. This cleanup delegates to the single archive rebuild invariant:
replace every canonical corner ladder with rows reconstructed from exact raw
provider payloads that are verified as full-time. Fixtures without matching raw
provider provenance fail closed and keep no canonical corner rows.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import rebuild_domestic_corners_from_archive as corner_rebuild

ROOT = Path(__file__).resolve().parents[1]
ODDS_PATH = ROOT / "odds" / "odds_api_io" / "domestic_odds.json"
ARCHIVE_PATH = ROOT / "odds" / "odds_api_io" / "domestic_provider_markets.json"
REPORT_PATH = ROOT / "reports" / "domestic_corner_scope_cleanup.json"


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def clean_feed(feed: Dict[str, Any], archive: Dict[str, Any]) -> Dict[str, Any]:
    report = corner_rebuild.rebuild_feed_corners(
        feed,
        archive,
        require_corners=False,
    )
    summary = {
        "source": report["source"],
        "policy": report["policy"],
        "syntheticOdds": False,
        "fixturesChecked": report["canonicalFixturesChecked"],
        "fixturesWithProviderArchive": report["canonicalFixturesMatched"],
        "fixturesMissingProviderArchive": report["canonicalFixturesMissingProviderArchive"],
        "fixturesRebuilt": report["fixturesRebuilt"],
        "fixturesClearedWithoutFullTimeCornerMarket": report[
            "fixturesClearedWithoutVerifiedFullTimeCorners"
        ],
        "removedCanonicalCornerRows": report["removedCanonicalCornerSelections"],
        "rebuiltFullTimeCornerRows": report["rebuiltFullTimeCornerSelections"],
        "totalCanonicalCornerRows": report["totalCanonicalCornerSelections"],
    }
    feed.setdefault("debug", {})["cornerScopeCleanup"] = summary
    return {**summary, "examples": report.get("examples", [])}


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
