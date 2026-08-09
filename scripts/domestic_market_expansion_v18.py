#!/usr/bin/env python3
"""Exact Asian market-family normalization for StatMaker.

Extends the v17 Domestic normalizer with separate Asian families while preserving
legacy canonical markets required by the current Production app. UAT can consume
Asian Handicap, Asian first-half Handicap, Asian goal lines, Asian first-half goal
lines, Asian corner totals, and Asian corner handicaps independently.

Only bookmaker prices returned directly by Odds-API.io are emitted. No derived,
converted, synthetic, or estimated prices are created.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Set

import domestic_market_expansion_v15 as base
import domestic_market_expansion_v17 as previous

VERSION = "domestic-market-expansion-v18-asian-families"

ASIAN_HANDICAP = "ASIAN_HANDICAP"
ASIAN_HANDICAP_1H = "ASIAN_HANDICAP_1H"
ASIAN_GOALS = "ASIAN_GOALS"
ASIAN_GOALS_1H = "ASIAN_GOALS_1H"
ASIAN_CORNERS = "ASIAN_CORNERS"
ASIAN_CORNER_HANDICAP = "ASIAN_CORNER_HANDICAP"

ASIAN_FAMILIES: Set[str] = {
    ASIAN_HANDICAP,
    ASIAN_HANDICAP_1H,
    ASIAN_GOALS,
    ASIAN_GOALS_1H,
    ASIAN_CORNERS,
    ASIAN_CORNER_HANDICAP,
}

FULL_TIME_HANDICAP_NAMES = {
    "spread",
    "asian handicap",
    "asian spread",
    "alternative asian handicap",
}
FIRST_HALF_HANDICAP_NAMES = {
    "spread ht",
    "asian handicap ht",
    "1st half asian handicap",
    "first half asian handicap",
    "alternative 1st half asian handicap",
}
FULL_TIME_GOAL_NAMES = {
    "totals",
    "goal line",
    "alternative goal line",
}
FIRST_HALF_GOAL_NAMES = {
    "totals ht",
    "1st half goal line",
    "first half goal line",
    "alternative 1st half goal line",
}
CORNER_TOTAL_NAMES = {
    "corners totals",
    "corner totals",
}
CORNER_HANDICAP_NAMES = {
    "corners spread",
    "corner spread",
    "asian corner handicap",
}

# Canonical non-Asian totals that may be produced by the pre-v18 normalizer.
# For provider Asian-style total markets we keep ordinary x.5 rows in the old
# family and move integer/quarter rows into their dedicated Asian family.
REGULAR_TOTAL_FAMILIES = {
    "MATCH_GOALS",
    "FIRST_HALF_GOALS",
    "HALF_TIME_GOALS",
    "MATCH_FIRST_HALF_GOALS",
    "MATCH_HALF_TIME_GOALS",
    "MATCH_CORNERS",
    "CORNERS",
}


def _normalized_raw(odds: Any, market: Dict[str, Any]) -> str:
    return base._norm(odds, base._raw(odds, market))


def _is_quarter_increment(line: float) -> bool:
    return math.isfinite(line) and abs(line * 4.0 - round(line * 4.0)) < 1e-9


def _is_regular_half_total(line: float) -> bool:
    """True for ordinary .5 totals (0.5, 1.5, 2.5, ...)."""
    if not math.isfinite(line):
        return False
    return abs((line - math.floor(line)) - 0.5) < 1e-9


def _clean_line(line: float) -> float:
    return 0.0 if abs(line) < 1e-9 else line


def _asian_handicap_rows(
    odds: Any,
    market: Dict[str, Any],
    bookmaker: str,
    home: str,
    away: str,
    canonical: str,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in base._rows(odds, market):
        home_line = base._line(odds, market, row)
        if home_line is None or not _is_quarter_increment(home_line):
            continue
        home_line = _clean_line(home_line)
        away_line = 0.0 if home_line == 0.0 else -home_line
        base._add(
            odds,
            out,
            canonical,
            "Home",
            odds.row_side_price(row, "home"),
            bookmaker,
            line=home_line,
            team=home,
        )
        base._add(
            odds,
            out,
            canonical,
            "Away",
            odds.row_side_price(row, "away"),
            bookmaker,
            line=away_line,
            team=away,
        )
    return base._dedupe(out)


def _asian_total_rows(
    odds: Any,
    market: Dict[str, Any],
    bookmaker: str,
    canonical: str,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in base._rows(odds, market):
        line = base._line(odds, market, row)
        if line is None or not _is_quarter_increment(line) or _is_regular_half_total(line):
            continue
        line = _clean_line(line)
        base._add(
            odds,
            out,
            canonical,
            "Over",
            odds.row_side_price(row, "over"),
            bookmaker,
            line=line,
        )
        base._add(
            odds,
            out,
            canonical,
            "Under",
            odds.row_side_price(row, "under"),
            bookmaker,
            line=line,
        )
    return base._dedupe(out)


def _asian_rows_for_market(
    odds: Any,
    market: Dict[str, Any],
    bookmaker: str,
    home: str,
    away: str,
) -> tuple[Optional[str], List[Dict[str, Any]]]:
    raw = _normalized_raw(odds, market)
    if raw in FULL_TIME_HANDICAP_NAMES:
        return ASIAN_HANDICAP, _asian_handicap_rows(odds, market, bookmaker, home, away, ASIAN_HANDICAP)
    if raw in FIRST_HALF_HANDICAP_NAMES:
        return ASIAN_HANDICAP_1H, _asian_handicap_rows(odds, market, bookmaker, home, away, ASIAN_HANDICAP_1H)
    if raw in FULL_TIME_GOAL_NAMES:
        return ASIAN_GOALS, _asian_total_rows(odds, market, bookmaker, ASIAN_GOALS)
    if raw in FIRST_HALF_GOAL_NAMES:
        return ASIAN_GOALS_1H, _asian_total_rows(odds, market, bookmaker, ASIAN_GOALS_1H)
    if raw in CORNER_TOTAL_NAMES:
        return ASIAN_CORNERS, _asian_total_rows(odds, market, bookmaker, ASIAN_CORNERS)
    if raw in CORNER_HANDICAP_NAMES:
        return ASIAN_CORNER_HANDICAP, _asian_handicap_rows(
            odds, market, bookmaker, home, away, ASIAN_CORNER_HANDICAP
        )
    return None, []


def _filter_regular_total_rows(rows: List[Dict[str, Any]], raw: str) -> List[Dict[str, Any]]:
    """Do not let integer/quarter Asian totals leak into ordinary O/U families."""
    if raw not in FULL_TIME_GOAL_NAMES | FIRST_HALF_GOAL_NAMES | CORNER_TOTAL_NAMES:
        return rows
    filtered: List[Dict[str, Any]] = []
    for row in rows:
        family = str(row.get("market") or "").strip().upper()
        if family not in REGULAR_TOTAL_FAMILIES:
            filtered.append(row)
            continue
        try:
            line = float(row.get("line"))
        except (TypeError, ValueError):
            filtered.append(row)
            continue
        if _is_regular_half_total(line):
            filtered.append(row)
    return filtered


def install(odds_module: Any, pipeline_module: Any = None) -> None:
    if getattr(odds_module, "_statmaker_market_v18_installed", False):
        return

    # Keep the full v17 chain, including temporary legacy European Handicap output
    # for the current Production app. v18 only extends exact Asian support.
    previous.install(odds_module, pipeline_module)
    base.NEW_MARKETS.update(ASIAN_FAMILIES)
    original = odds_module.normalize_market

    def expanded(
        market: Dict[str, Any],
        bookmaker: str,
        home: str,
        away: str,
        debug: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        raw = _normalized_raw(odds_module, market)
        existing = _filter_regular_total_rows(
            original(market, bookmaker, home, away, debug),
            raw,
        )
        family, asian = _asian_rows_for_market(
            odds_module,
            market,
            bookmaker,
            home,
            away,
        )
        if family is None:
            return existing

        raw_label = base._raw(odds_module, market)
        odds_module.record_raw_market(debug, raw_label, family)
        base._record(debug, raw_label, family, len(asian))
        if not asian and raw in (
            FULL_TIME_HANDICAP_NAMES
            | FIRST_HALF_HANDICAP_NAMES
            | CORNER_HANDICAP_NAMES
        ):
            odds_module.record_skipped_market(
                debug,
                raw_label,
                "Asian market present but no exact quarter/integer/half rows were normalized",
                family_override=family,
            )
        return base._dedupe(existing + asian)

    odds_module.SUPPORTED_MARKETS.update(ASIAN_FAMILIES)
    for family in sorted(ASIAN_FAMILIES):
        if family not in odds_module.EMITTED_MARKET_COUNT_KEYS:
            odds_module.EMITTED_MARKET_COUNT_KEYS.append(family)
    odds_module.normalize_market = expanded
    odds_module._statmaker_market_v18_installed = True
