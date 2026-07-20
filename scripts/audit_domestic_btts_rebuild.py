#!/usr/bin/env python3
"""Audit canonical BTTS against the final effective archived provider state."""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any, Dict, List, Tuple
import odds_api_io_market_audit as market_audit
import rebuild_domestic_btts_from_archive as rebuild

ROOT = Path(__file__).resolve().parents[1]
ODDS_PATH = ROOT / "odds" / "odds_api_io" / "domestic_odds.json"
ARCHIVE_PATH = ROOT / "odds" / "odds_api_io" / "domestic_provider_markets.json"
REPORT_PATH = ROOT / "reports" / "domestic_btts_archive_rebuild.json"


def btts_rows(match: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [dict(row) for row in match.get("markets", []) or [] if row.get("market") == "BTTS"]


def has_btts_payload(match: Dict[str, Any]) -> bool:
    for payload in match.get("providerMarkets", []) or []:
        market = payload.get("market")
        if payload.get("exactProviderPayload") is not True or not isinstance(market, dict):
            continue
        family = market_audit.classify_provider_market(market_audit.provider_market_text(market)).get("family")
        if family in rebuild.BTTS_FAMILIES:
            return True
    return False


def compact(rows: List[Dict[str, Any]]) -> List[Tuple[str, float, str]]:
    return sorted((str(r.get("selection")), float(r.get("odds") or 0), str(r.get("bookmaker") or "")) for r in rows)


def main() -> int:
    feed = rebuild.read_json(ODDS_PATH)
    archive = rebuild.read_json(ARCHIVE_PATH)
    canonical = rebuild.canonical_matches(feed)

    effective: Dict[Tuple[str, str, str, str], Tuple[str, Dict[str, Any]]] = {}
    counts: Dict[Tuple[str, str, str, str], int] = {}
    for league in archive.get("leagues", []) or []:
        code = str(league.get("leagueCode") or "")
        for match in league.get("matches", []) or []:
            if not has_btts_payload(match):
                continue
            key = rebuild.archive_key(code, match)
            effective[key] = (code, match)
            counts[key] = counts.get(key, 0) + 1

    fixtures: List[Dict[str, Any]] = []
    mismatches = 0
    focus = None
    for key, (code, archived_match) in effective.items():
        target = canonical.get(key)
        if target is None:
            continue
        _, expected = rebuild.normalize_archived_full_time_btts(archived_match)
        actual = btts_rows(target)
        ok = compact(expected) == compact(actual)
        mismatches += 0 if ok else 1
        row = {
            "leagueCode": code,
            "id": str(target.get("id") or ""),
            "fixture": f"{target.get('homeTeam')} - {target.get('awayTeam')}",
            "archiveSnapshotCount": counts.get(key, 1),
            "expectedFullTimeBtts": expected,
            "canonicalBtts": actual,
            "matchesExpected": ok,
        }
        fixtures.append(row)
        text = row["fixture"].casefold()
        if "ilves" in text and ("turku ps" in text or "tps" in text):
            focus = row

    report = {
        "uniqueFixtureAuditCount": len(fixtures),
        "duplicateArchiveSnapshotCount": sum(max(0, n - 1) for n in counts.values()),
        "mismatchCount": mismatches,
        "focusFixture": focus,
        "fixtures": fixtures,
    }
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "fixtures"}, ensure_ascii=False, indent=2))
    if mismatches:
        raise SystemExit(f"BTTS audit found {mismatches} canonical mismatches")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
