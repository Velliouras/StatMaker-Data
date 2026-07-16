#!/usr/bin/env python3
"""Write compact examples for every exact Domestic provider market shape."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "odds" / "odds_api_io" / "domestic_provider_markets.json"
REPORT = ROOT / "reports" / "domestic_market_shapes_v16.json"


def compact_market(market: Dict[str, Any]) -> Dict[str, Any]:
    raw = market.get("outcomes") or market.get("odds") or market.get("prices") or []
    if isinstance(raw, dict):
        rows = []
        for key, value in list(raw.items())[:4]:
            rows.append({"key": key, "value": value})
    elif isinstance(raw, list):
        rows = raw[:4]
    else:
        rows = raw
    return {
        "name": market.get("name") or market.get("market") or market.get("type") or market.get("key"),
        "topLevelKeys": sorted(market.keys()),
        "rows": rows,
    }


def main() -> int:
    archive = json.loads(ARCHIVE.read_text(encoding="utf-8-sig"))
    counts = defaultdict(int)
    examples = defaultdict(list)
    for league in archive.get("leagues", []) or []:
        code = str(league.get("leagueCode") or "")
        for match in league.get("matches", []) or []:
            fixture = f"{match.get('homeTeam')} - {match.get('awayTeam')}"
            for payload in match.get("providerMarkets", []) or []:
                market = payload.get("market")
                if payload.get("exactProviderPayload") is not True or not isinstance(market, dict):
                    continue
                name = str(payload.get("providerMarket") or market.get("name") or "").strip()
                if not name:
                    continue
                counts[name] += 1
                if len(examples[name]) < 2:
                    examples[name].append({
                        "leagueCode": code,
                        "fixture": fixture,
                        "bookmaker": payload.get("bookmaker"),
                        "market": compact_market(market),
                    })
    report = {
        "providerMarketCount": len(counts),
        "payloadCount": sum(counts.values()),
        "markets": {
            name: {"count": counts[name], "examples": examples[name]}
            for name in sorted(counts)
        },
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"providerMarketCount": len(counts), "payloadCount": sum(counts.values())}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
