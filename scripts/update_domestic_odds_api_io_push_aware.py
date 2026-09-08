#!/usr/bin/env python3
"""Run the Domestic odds updater with push-aware integer corner totals enabled.

The upstream updater intentionally accepts only half-unit count lines. That is
correct for goals in the current Android evidence path, but it also removes all
integer corner totals returned by Odds-API.io even though the StatMaker count
probability core models integer-line pushes explicitly.

This production wrapper changes only corner line acceptance and canonical display
identity:
- MATCH_CORNERS and TEAM_CORNERS accept integer or half-unit lines.
- All other count markets keep the existing half-line-only policy.
- Team-scoped totals retain their metric in the canonical selection label, so
  ``Team Under X`` can never hide whether the market is goals/corners/cards/shots/SOT.
- No synthetic odds or transformed lines are created.
"""

from __future__ import annotations

import math
import sys
from typing import Any, Dict, List

import update_domestic_odds_api_io as base


_original_normalize_market = base.normalize_market
_original_is_half_line = base.is_half_line

TEAM_MARKET_LABELS = {
    "TEAM_TOTAL_GOALS": "Goals",
    "TEAM_CORNERS": "Corners",
    "TEAM_CARDS": "Cards",
    "TEAM_SHOTS": "Shots",
    "TEAM_SHOTS_ON_TARGET": "Shots on Target",
}


def _is_integer_line(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and abs(value - round(value)) < 1e-9


def _explicit_team_market_labels(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Make team O/U selection text lossless without changing market identity or price."""

    labelled: List[Dict[str, Any]] = []
    for source in rows:
        item = dict(source)
        metric = TEAM_MARKET_LABELS.get(str(item.get("market") or ""))
        team = str(item.get("team") or "").strip()
        line = base.line_float(item.get("line"))
        selection = base.normalize_text(item.get("selection"))
        side = "Over" if "over" in selection.split() else "Under" if "under" in selection.split() else ""
        if metric and team and line is not None and side:
            item["selection"] = f"{team} {metric} {side} {line:g}"
        labelled.append(item)
    return labelled


def _normalize_market_with_integer_corners(
    market: Dict[str, Any],
    bookmaker: str,
    home: str,
    away: str,
    debug: Dict[str, Any],
) -> List[Dict[str, Any]]:
    raw_name = base.raw_market_name(market)
    family = base.market_family_from_name(raw_name)
    if family != "CORNERS":
        return _explicit_team_market_labels(
            _original_normalize_market(market, bookmaker, home, away, debug)
        )

    # normalize_market consults the module-level is_half_line function. Override it
    # only for this single corner-market call, then restore it immediately.
    base.is_half_line = lambda value: _original_is_half_line(value) or _is_integer_line(value)
    try:
        return _explicit_team_market_labels(
            _original_normalize_market(market, bookmaker, home, away, debug)
        )
    finally:
        base.is_half_line = _original_is_half_line


def _self_check() -> None:
    corner_debug: Dict[str, Any] = {}
    corners = _normalize_market_with_integer_corners(
        {
            "name": "Corners Totals",
            "odds": [{"hdp": 10, "over": "1.91", "under": "1.91"}],
        },
        "Bet365",
        "Home",
        "Away",
        corner_debug,
    )
    assert {(item["market"], item["selection"], item.get("line")) for item in corners} == {
        ("MATCH_CORNERS", "Corners Over 10", 10.0),
        ("MATCH_CORNERS", "Corners Under 10", 10.0),
    }, corners

    goals = _normalize_market_with_integer_corners(
        {
            "name": "Totals",
            "odds": [{"hdp": 3, "over": "1.91", "under": "1.91"}],
        },
        "Bet365",
        "Home",
        "Away",
        {},
    )
    assert goals == [], "Integer goal totals must remain excluded"

    full_time_btts = _normalize_market_with_integer_corners(
        {
            "name": "Both Teams To Score",
            "odds": [{"yes": "1.57", "no": "2.25"}],
        },
        "Bet365",
        "Home",
        "Away",
        {},
    )
    assert {(item["market"], item["selection"], item["odds"]) for item in full_time_btts} == {
        ("BTTS", "Yes", 1.57),
        ("BTTS", "No", 2.25),
    }, full_time_btts

    second_half_btts = _normalize_market_with_integer_corners(
        {
            "name": "Both Teams To Score 2H",
            "odds": [{"yes": "2.75", "no": "1.40"}],
        },
        "Bet365",
        "Home",
        "Away",
        {},
    )
    assert second_half_btts == [], "Second-half BTTS must never be emitted as full-time BTTS"

    labelled = _explicit_team_market_labels(
        [
            {
                "market": "TEAM_SHOTS_ON_TARGET",
                "selection": "Borussia Dortmund Under 5.5",
                "odds": 1.833,
                "line": 5.5,
                "team": "Borussia Dortmund",
            }
        ]
    )
    assert labelled[0]["selection"] == "Borussia Dortmund Shots on Target Under 5.5", labelled


if __name__ == "__main__":
    _self_check()
    base.normalize_market = _normalize_market_with_integer_corners
    raise SystemExit(base.main(sys.argv[1:]))
