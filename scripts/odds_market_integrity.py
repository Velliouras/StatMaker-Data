#!/usr/bin/env python3
"""Sanitize canonical betting feeds without estimating or changing provider odds.

Rules:
- A market family for one match is sourced from one bookmaker only.
- Configured bookmaker order is preferred after integrity/completeness checks.
- Duplicate entries from the same bookmaker use the conservative lower decimal price.
- Over/Under line curves must be monotonic within the selected bookmaker.
- Invalid market groups fail closed and are removed.
"""

from __future__ import annotations

import argparse
import copy
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple

LINE_MARKETS = {
    "MATCH_GOALS",
    "FIRST_HALF_GOALS",
    "TEAM_TOTAL_GOALS",
    "MATCH_CORNERS",
    "TEAM_CORNERS",
    "MATCH_CARDS",
    "TEAM_CARDS",
    "MATCH_SHOTS",
    "TEAM_SHOTS",
    "MATCH_SHOTS_ON_TARGET",
    "TEAM_SHOTS_ON_TARGET",
}
FIXED_MARKET_SELECTIONS = {
    "1X2": {"Home", "Draw", "Away"},
    "BTTS": {"Yes", "No"},
    "DOUBLE_CHANCE": {"1X", "12", "X2"},
}
DEFAULT_BOOKMAKER_PRIORITY = ("Bet365", "Unibet")
EPSILON = 1e-9


def _text(value: Any) -> str:
    return str(value or "").strip()


def _float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result > 1.0 else None


def _line(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _selection_key(item: Dict[str, Any]) -> Tuple[str, str, str]:
    line = _line(item.get("line"))
    return (
        _text(item.get("selection")),
        "" if line is None else f"{line:g}",
        _text(item.get("team")),
    )


def _group_key(item: Dict[str, Any]) -> Tuple[str, str]:
    market = _text(item.get("market"))
    team = _text(item.get("team")) if market.startswith("TEAM_") else ""
    return market, team


def _priority(payload: Dict[str, Any]) -> List[str]:
    requested = payload.get("bookmakersRequested")
    values = [_text(value) for value in requested] if isinstance(requested, list) else []
    values = [value for value in values if value]
    for fallback in DEFAULT_BOOKMAKER_PRIORITY:
        if fallback not in values:
            values.append(fallback)
    return values


def _dedupe_same_bookmaker(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep a conservative exact price when duplicate canonical rows exist."""
    chosen: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for raw in items:
        item = copy.deepcopy(raw)
        odds = _float(item.get("odds"))
        if odds is None:
            continue
        key = _selection_key(item)
        previous = chosen.get(key)
        if previous is None or odds < float(previous["odds"]):
            chosen[key] = item
    return list(chosen.values())


def _line_curve_valid(items: Sequence[Dict[str, Any]]) -> bool:
    sides: Dict[str, List[Tuple[float, float]]] = defaultdict(list)
    for item in items:
        market = _text(item.get("market"))
        if market not in LINE_MARKETS:
            continue
        line = _line(item.get("line"))
        odds = _float(item.get("odds"))
        selection = _text(item.get("selection")).casefold()
        if line is None or odds is None:
            return False
        if "over" in selection:
            sides["over"].append((line, odds))
        elif "under" in selection:
            sides["under"].append((line, odds))
        else:
            return False

    for side, rows in sides.items():
        rows.sort()
        for (_, previous), (_, current) in zip(rows, rows[1:]):
            if side == "over" and current + EPSILON < previous:
                return False
            if side == "under" and current - EPSILON > previous:
                return False
    return True


def _completeness(market: str, items: Sequence[Dict[str, Any]]) -> Tuple[int, int]:
    expected = FIXED_MARKET_SELECTIONS.get(market)
    if expected:
        present = {_text(item.get("selection")) for item in items}
        return len(present & expected), len(expected)
    unique_lines = {
        (_text(item.get("selection")).casefold(), _line(item.get("line")))
        for item in items
        if _line(item.get("line")) is not None
    }
    lines = {_line(item.get("line")) for item in items if _line(item.get("line")) is not None}
    return len(unique_lines), max(1, len(lines) * 2)


def _candidate_valid(market: str, items: Sequence[Dict[str, Any]]) -> bool:
    if not items:
        return False
    if market in LINE_MARKETS:
        return _line_curve_valid(items)
    expected = FIXED_MARKET_SELECTIONS.get(market)
    if expected:
        return bool({_text(item.get("selection")) for item in items} & expected)
    return True


def _choose_bookmaker(
    market: str,
    candidates: Dict[str, List[Dict[str, Any]]],
    priority: Sequence[str],
) -> Tuple[str | None, List[Dict[str, Any]], Dict[str, Any]]:
    rank = {name: index for index, name in enumerate(priority)}
    valid: List[Tuple[int, int, int, str, List[Dict[str, Any]]]] = []
    rejected: Dict[str, str] = {}

    for bookmaker, raw_items in candidates.items():
        items = _dedupe_same_bookmaker(raw_items)
        if not _candidate_valid(market, items):
            rejected[bookmaker] = "integrity"
            continue
        complete, expected = _completeness(market, items)
        is_complete = 1 if complete >= expected else 0
        valid.append((
            is_complete,
            complete,
            -rank.get(bookmaker, len(priority) + 100),
            bookmaker,
            items,
        ))

    if not valid:
        return None, [], {"rejected": rejected}

    valid.sort(reverse=True)
    _, _, _, bookmaker, items = valid[0]
    return bookmaker, items, {"rejected": rejected}


def sanitize_match(
    match: Dict[str, Any],
    bookmaker_priority: Sequence[str],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    groups: Dict[Tuple[str, str], Dict[str, List[Dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    passthrough: List[Dict[str, Any]] = []

    for raw in match.get("markets", []) or []:
        if not isinstance(raw, dict):
            continue
        market = _text(raw.get("market"))
        bookmaker = _text(raw.get("bookmaker"))
        if not market or not bookmaker:
            continue
        if market not in LINE_MARKETS and market not in FIXED_MARKET_SELECTIONS:
            passthrough.append(copy.deepcopy(raw))
            continue
        groups[_group_key(raw)][bookmaker].append(raw)

    output = copy.deepcopy(match)
    sanitized: List[Dict[str, Any]] = []
    report_groups: List[Dict[str, Any]] = []

    for (market, team), candidates in sorted(groups.items()):
        bookmaker, items, details = _choose_bookmaker(market, candidates, bookmaker_priority)
        if bookmaker is None:
            report_groups.append({
                "market": market,
                "team": team or None,
                "status": "dropped",
                "candidateBookmakers": sorted(candidates),
                **details,
            })
            continue
        items.sort(key=lambda item: (
            _text(item.get("selection")),
            _line(item.get("line")) if _line(item.get("line")) is not None else -1,
        ))
        sanitized.extend(items)
        report_groups.append({
            "market": market,
            "team": team or None,
            "status": "kept",
            "bookmaker": bookmaker,
            "rows": len(items),
            "candidateBookmakers": sorted(candidates),
            **details,
        })

    sanitized.extend(passthrough)
    output["markets"] = sanitized
    return output, {
        "matchId": _text(match.get("id")),
        "fixture": f"{_text(match.get('homeTeam'))} - {_text(match.get('awayTeam'))}",
        "before": len(match.get("markets", []) or []),
        "after": len(sanitized),
        "groups": report_groups,
    }


def sanitize_payload(payload: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    result = copy.deepcopy(payload)
    priority = _priority(result)
    reports: List[Dict[str, Any]] = []

    if isinstance(result.get("matches"), list):
        new_matches = []
        for match in result["matches"]:
            if not isinstance(match, dict):
                continue
            sanitized, report = sanitize_match(match, priority)
            new_matches.append(sanitized)
            reports.append(report)
        result["matches"] = new_matches

    for league in result.get("leagues", []) or []:
        if not isinstance(league, dict):
            continue
        new_matches = []
        for match in league.get("matches", []) or []:
            if not isinstance(match, dict):
                continue
            sanitized, report = sanitize_match(match, priority)
            report["leagueCode"] = _text(league.get("leagueCode"))
            new_matches.append(sanitized)
            reports.append(report)
        league["matches"] = new_matches

    dropped_groups = sum(
        1 for report in reports for group in report["groups"] if group["status"] == "dropped"
    )
    mixed_groups_before = sum(
        1 for report in reports for group in report["groups"]
        if len(group.get("candidateBookmakers", [])) > 1
    )
    integrity = {
        "policy": "single bookmaker per match market family; fail closed on invalid line curves",
        "bookmakerPriority": priority,
        "matchesChecked": len(reports),
        "mixedBookmakerGroupsResolved": mixed_groups_before,
        "groupsDropped": dropped_groups,
        "matches": reports,
    }
    debug = result.setdefault("debug", {})
    if isinstance(debug, dict):
        debug["marketIntegrity"] = {
            key: value for key, value in integrity.items() if key != "matches"
        }
    return result, integrity


def sanitize_file(path: Path, *, write: bool, report_path: Path | None = None) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    sanitized, report = sanitize_payload(payload)
    if write:
        path.write_text(json.dumps(sanitized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--report-dir", type=Path)
    args = parser.parse_args()

    summary = []
    for path in args.paths:
        report_path = args.report_dir / f"{path.stem}_integrity.json" if args.report_dir else None
        report = sanitize_file(path, write=args.write, report_path=report_path)
        summary.append({
            "path": str(path),
            "matchesChecked": report["matchesChecked"],
            "mixedBookmakerGroupsResolved": report["mixedBookmakerGroupsResolved"],
            "groupsDropped": report["groupsDropped"],
        })
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
