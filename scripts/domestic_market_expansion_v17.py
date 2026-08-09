#!/usr/bin/env python3
"""Settlement-safe Asian Handicap exact-market normalization for StatMaker.

This wrapper extends the existing v16 Domestic market normalizer without changing
any existing market. It emits only full-time 2-way Asian Handicap selections whose
lines settle as ordinary win/loss/push in the current app model (0, +/-0.5,
+/-1.0, +/-1.5, ...). Quarter lines are deliberately excluded until the app has
explicit half-win / half-loss settlement states.

Only prices returned directly by Odds-API.io are emitted. No derived, converted,
synthetic, or estimated prices are created.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import domestic_market_expansion_v15 as base

VERSION = "domestic-market-expansion-v17-asian-handicap"
ASIAN_HANDICAP = "ASIAN_HANDICAP"
FULL_TIME_ASIAN_MARKET_NAMES = {
    "spread",
    "asian handicap",
    "asian spread",
    "alternative asian handicap",
}


def _is_settlement_safe_line(line: float) -> bool:
    """Current app can settle integer/half-goal Asian lines, but not quarter lines."""
    doubled = line * 2.0
    return abs(doubled - round(doubled)) < 1e-9


def _is_full_time_asian_market(odds: Any, market: Dict[str, Any]) -> bool:
    raw = base._norm(odds, base._raw(odds, market))
    return raw in FULL_TIME_ASIAN_MARKET_NAMES


def normalize_asian_handicap(
    odds: Any,
    market: Dict[str, Any],
    bookmaker: str,
    home: str,
    away: str,
) -> Optional[List[Dict[str, Any]]]:
    if not _is_full_time_asian_market(odds, market):
        return None

    out: List[Dict[str, Any]] = []
    for row in base._rows(odds, market):
        home_line = base._line(odds, market, row)
        if home_line is None or not _is_settlement_safe_line(home_line):
            continue
        home_line = 0.0 if abs(home_line) < 1e-9 else home_line
        away_line = 0.0 if home_line == 0.0 else -home_line

        home_price = odds.row_side_price(row, "home")
        away_price = odds.row_side_price(row, "away")
        base._add(
            odds,
            out,
            ASIAN_HANDICAP,
            "Home",
            home_price,
            bookmaker,
            line=home_line,
            team=home,
        )
        base._add(
            odds,
            out,
            ASIAN_HANDICAP,
            "Away",
            away_price,
            bookmaker,
            line=away_line,
            team=away,
        )
    return base._dedupe(out)


def install(odds_module: Any, pipeline_module: Any = None) -> None:
    if getattr(odds_module, "_statmaker_market_v17_installed", False):
        return

    # The archive rebuild module keys its replaceable families from v15.NEW_MARKETS.
    # Add Asian Handicap there as well so already-cached exact provider Spread rows can
    # be re-normalized without any extra Odds-API.io request.
    base.NEW_MARKETS.add(ASIAN_HANDICAP)

    # Preserve every existing v16 market, including the legacy European Handicap
    # while Production still runs the pre-Asian app. The UAT app explicitly rejects
    # European Handicap rows, so this gives a safe staged migration with no Prod gap.
    base.install(odds_module, pipeline_module)
    original = odds_module.normalize_market

    def expanded(
        market: Dict[str, Any],
        bookmaker: str,
        home: str,
        away: str,
        debug: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        normalized = normalize_asian_handicap(odds_module, market, bookmaker, home, away)
        if normalized is None:
            return original(market, bookmaker, home, away, debug)

        raw = base._raw(odds_module, market)
        odds_module.record_raw_market(debug, raw, ASIAN_HANDICAP)
        base._record(debug, raw, ASIAN_HANDICAP, len(normalized))
        if not normalized:
            odds_module.record_skipped_market(
                debug,
                raw,
                "Asian Handicap present but no settlement-safe exact rows were normalized",
                family_override=ASIAN_HANDICAP,
            )
        return normalized

    odds_module.SUPPORTED_MARKETS.add(ASIAN_HANDICAP)
    if ASIAN_HANDICAP not in odds_module.EMITTED_MARKET_COUNT_KEYS:
        odds_module.EMITTED_MARKET_COUNT_KEYS.append(ASIAN_HANDICAP)
    odds_module.normalize_market = expanded
    odds_module._statmaker_market_v17_installed = True
