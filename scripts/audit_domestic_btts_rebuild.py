#!/usr/bin/env python3
"""Audit canonical Domestic BTTS against the exact archived provider payloads."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import odds_api_io_market_audit as market_audit
import rebuild_domestic_btts_from_archive as rebuild

ROOT = Path(__file__).resolve().parents[1]
ODDS_PATH = ROOT / "odds" / "odds_api_io" / "domestic_odds.json"
ARCHIVE_PATH = ROOT / "odds" / "odds_api_io" / "domestic_provider_markets.json"
REPORT_PATH = ROOT / "reports" / "domestic_btts_archive_rebuild.json"


def btts_rows(match: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [dict(row) for row in match.get("markets", []) or [] if row.get("market") == "BTTS"]


def provider_btts_markets(match: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for payload in match.get("providerMarkets", []) or []:
        if payload.get("exactProviderPayload") is not True:
            continue
        market = payload.get("market")
        if not isinstance(market, dict):
            continue
        classification = market_audit.classify_provider_market(market_audit.provider_market_text(market))
        if classification.get("family") not in rebuild.BTTS_FAMILIES:
            continue
        rows.append(
            {
                "bookmaker": str(payload.get("bookmaker") or ""),
                "providerMarket": str(payload.get("providerMarket") or market.get("name") or ""),
                "family": classification.get("family"),
                "status": classification.get("status"),
                "odds": market.get("odds") or market.get("outcomes") or market.get("prices") or [],
            }
        )
    return rows


def main() -> int:
    feed = rebuild.read_json(ODDS_PATH)
    archive = rebuild.read_json(ARCHIVE_PATH)
    canonical = rebuild.canonical_matches(feed)
    fixtures: List[Dict[str, Any]] = []
    mismatches = 0

    for league in archive.get("leagues", []) or []:
        code = str(league.get("leagueCode") or "")
        for archived_match in league.get("matches", []) or []:
            raw_btts = provider_btts_markets(archived_match)
            if not raw_btts:
                continue
            target = canonical.get(rebuild.archive_key(code, archived_match))
            if target is None:
                continue
            _, expected = rebuild.normalize_archived_full_time_btts(archived_match)
            actual = btts_rows(target)
            expected_compact = sorted(
                (str(row.get("selection")), float(row.get("odds") or 0), str(row.get("bookmaker") or ""))
                for row in expected
            )
            actual_compact = sorted(
                (str(row.get("selection")), float(row.get("odds") or 0), str(row.get("bookmaker") or ""))
                for row in actual
            )
            matches_expected = expected_compact == actual_compact
            if not matches_expected:
                mismatches += 1
            fixtures.append(
                {
                    "leagueCode": code,
                    "id": str(target.get("id") or ""),
                    "fixture": f"{target.get('homeTeam')} - {target.get('awayTeam')}",
                    "providerBttsMarkets": raw_btts,
                    "expectedFullTimeBtts": expected,
                    "canonicalBtts": actual,
                    "matchesExpected": matches_expected,
                }
            )

    report = {
        "source": "exact archived Odds-API.io provider payloads",
        "syntheticOdds": False,
        "policy": "canonical BTTS must equal full-time provider BTTS only; HT/2H excluded",
        "fixtureAuditCount": len(fixtures),
        "mismatchCount": mismatches,
        "fixtures": fixtures,
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "fixtures"}, ensure_ascii=False, indent=2))
    if mismatches:
        raise SystemExit(f"BTTS audit found {mismatches} canonical mismatches")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
