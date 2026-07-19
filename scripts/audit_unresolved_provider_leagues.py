#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Any

import update_domestic_odds_api_io as odds

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "reports" / "unresolved_provider_leagues.json"
TARGETS = {
    "HUN": ["hungary", "hungarian"],
    "ISL": ["iceland"],
    "EST": ["estonia", "estonian"],
    "NOR2": ["norway", "norwegian"],
}


def norm(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def main() -> int:
    key = os.getenv("ODDS_API_IO_KEY", "").strip()
    if not key:
        raise SystemExit("ODDS_API_IO_KEY is required")
    debug = {"warnings": [], "apiCalls": []}
    providers = odds.discover_provider_leagues(key, debug)
    out = {}
    for code, country_terms in TARGETS.items():
        rows = []
        for item in providers:
            hay = norm(f"{item.get('name', '')} {item.get('slug', '')}")
            if not any(term in hay for term in country_terms):
                continue
            rows.append({
                "name": item.get("name"),
                "slug": item.get("slug"),
                "eventsCount": item.get("eventsCount"),
            })
        rows.sort(key=lambda x: (-int(x.get("eventsCount") or 0), str(x.get("name") or "")))
        out[code] = rows
    REPORT.write_text(json.dumps({
        "mode": "unresolved-provider-league-catalog",
        "productionOddsTouched": False,
        "bettingEngineTouched": False,
        "apiCallCount": len(debug.get("apiCalls", [])),
        "rateLimitRemaining": debug.get("rateLimitRemaining"),
        "targets": out,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
