#!/usr/bin/env python3
"""Annotate exact Domestic odds with the immediately previous stored price.

This script performs no network/API calls. It compares the pre-refresh feed snapshot with the
post-refresh sanitized feed and writes previousOdd/previousOddAt on exact matching selections.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def iter_matches(feed: dict[str, Any]) -> Iterable[dict[str, Any]]:
    for match in feed.get("matches", []) or []:
        if isinstance(match, dict):
            yield match
    for league in feed.get("leagues", []) or []:
        if not isinstance(league, dict):
            continue
        for match in league.get("matches", []) or []:
            if isinstance(match, dict):
                yield match


def match_key(match: dict[str, Any]) -> tuple[Any, ...]:
    match_id = str(match.get("id") or match.get("matchId") or "").strip()
    if match_id:
        return ("id", match_id)
    return (
        "fixture",
        str(match.get("date") or match.get("kickoff") or "")[:16],
        str(match.get("canonicalHomeTeam") or match.get("homeTeam") or "").strip().casefold(),
        str(match.get("canonicalAwayTeam") or match.get("awayTeam") or "").strip().casefold(),
    )


def selection_key(match: dict[str, Any], selection: dict[str, Any]) -> tuple[Any, ...]:
    line = selection.get("line")
    line_key = None if line is None else str(line).strip()
    return match_key(match) + (
        str(selection.get("market") or "").strip().casefold(),
        str(selection.get("selection") or "").strip().casefold(),
        str(selection.get("team") or "").strip().casefold(),
        line_key,
        str(selection.get("bookmaker") or "").strip().casefold(),
    )


def previous_timestamp(feed: dict[str, Any]) -> str:
    for container in (feed, feed.get("metadata") or {}, feed.get("debug") or {}):
        if not isinstance(container, dict):
            continue
        for key in ("generatedAt", "generated_at", "updatedAt", "updated_at"):
            value = container.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def annotate(before: dict[str, Any], current: dict[str, Any]) -> tuple[int, int]:
    prior: dict[tuple[Any, ...], float] = {}
    for match in iter_matches(before):
        for selection in match.get("markets", []) or []:
            if not isinstance(selection, dict):
                continue
            raw = selection.get("odds", selection.get("odd"))
            try:
                odd = float(raw)
            except (TypeError, ValueError):
                continue
            if odd > 1.0:
                prior[selection_key(match, selection)] = odd

    timestamp = previous_timestamp(before)
    annotated = 0
    moved = 0
    for match in iter_matches(current):
        for selection in match.get("markets", []) or []:
            if not isinstance(selection, dict):
                continue
            previous = prior.get(selection_key(match, selection))
            if previous is None:
                selection.pop("previousOdd", None)
                selection.pop("previousOddAt", None)
                continue
            raw = selection.get("odds", selection.get("odd"))
            try:
                current_odd = float(raw)
            except (TypeError, ValueError):
                continue
            selection["previousOdd"] = round(previous, 6)
            if timestamp:
                selection["previousOddAt"] = timestamp
            annotated += 1
            if abs(previous - current_odd) >= 0.005:
                moved += 1
    return annotated, moved


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("before")
    parser.add_argument("current")
    args = parser.parse_args()
    before_path = Path(args.before)
    current_path = Path(args.current)
    before = load(before_path)
    current = load(current_path)
    annotated, moved = annotate(before, current)
    current.setdefault("debug", {})["oddsMovement"] = {
        "previousSelectionsMatched": annotated,
        "selectionsMoved": moved,
        "source": "pre-refresh repository snapshot",
        "extraApiCalls": 0,
    }
    current_path.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Odds movement: matched={annotated}, moved={moved}, extraApiCalls=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
