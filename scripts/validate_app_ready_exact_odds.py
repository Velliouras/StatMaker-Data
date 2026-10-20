#!/usr/bin/env python3
"""Offline fail-fast verification of the three exact UEFA price feeds.

Must run before the expensive App-Ready emulator.  No API requests and no
published data mutations.  The direct V2 model separately validates market
identity and odds for every staged quote.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

COMPETITIONS = ("champions_league", "europa_league", "conference_league")


def check_feeds(directory: Path) -> dict[str, int]:
    if not directory.is_dir():
        raise ValueError(f"Missing UEFA exact feed directory: {directory}")
    result: dict[str, int] = {}
    for code in COMPETITIONS:
        path = directory / f"{code}_odds.json"
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"Missing UEFA exact feed: {path}")
        doc = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            raise ValueError(f"Malformed UEFA odds JSON: {path}")
        stamp = str(doc.get("generatedAt") or "")
        try:
            datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"Missing/invalid generatedAt: {path}") from exc
        rows = doc.get("matches")
        leagues = doc.get("leagues", [])
        if not isinstance(rows, list) or not isinstance(leagues, list):
            raise ValueError(f"Missing matches/leagues: {path}")
        if any(not isinstance(m, dict) or not isinstance(m.get("markets"), list)
               for m in rows):
            raise ValueError(f"Bad match/markets payload: {path}")
        for league in leagues:
            if not isinstance(league, dict) or not isinstance(league.get("matches", []), list):
                raise ValueError(f"Bad league payload: {path}")
            for match in league.get("matches", []):
                if not isinstance(match, dict) or not isinstance(match.get("markets"), list):
                    raise ValueError(f"Bad nested match/markets payload: {path}")
        result[code] = len(rows) + sum(len(l.get("matches", [])) for l in leagues)
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--directory", type=Path, required=True)
    args = ap.parse_args()
    print("APP_READY_UEFA_EXACT_FEED_PREFLIGHT_OK", json.dumps(
        check_feeds(args.directory.resolve()), sort_keys=True))


if __name__ == "__main__":
    main()
