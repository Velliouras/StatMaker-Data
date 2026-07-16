#!/usr/bin/env python3
"""Canonical exact-odds expansion for StatMaker Domestic markets.

The module is installed after ``domestic_odds_expansion.install``. It maps only
bookmaker outcomes that are present in the Odds-API.io payload. It never derives,
estimates, converts, or fabricates a price.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

VERSION = "domestic-market-expansion-v15"

NEW_MARKETS = {
    "HALF_TIME_1X2",
    "HALF_TIME_DOUBLE_CHANCE",
    "TEAM_FIRST_HALF_GOALS",
    "SECOND_HALF_GOALS",
    "TEAM_SECOND_HALF_GOALS",
    "DRAW_NO_BET",
    "MATCH_YELLOW_CARDS",
    "TEAM_YELLOW_CARDS",
    "RED_CARD",
    "MOST_CORNERS",
    "CORNER_HANDICAP",
    "MOST_SHOTS",
    "MOST_SHOTS_ON_TARGET",
    "GOAL_IN_BOTH_HALVES",
    "TEAM_SCORE_BOTH_HALVES",
    "EUROPEAN_HANDICAP",
    "ODD_EVEN_GOALS",
    "GOAL_BANDS",
    "WINNING_MARGIN",
    "HALF_TIME_FULL_TIME",
    "CORRECT_SCORE",
    "BTTS_OVER_UNDER",
    "RESULT_OVER_UNDER",
}

# Families confirmed in the current Odds-API.io Domestic archive. The generic
# classifier below also supports the remaining catalogue entries when a provider
# starts returning them under an explicit market name.
CONFIRMED_PROVIDER_NAMES = {
    "draw no bet": "DRAW_NO_BET",
    "half time result": "HALF_TIME_1X2",
    "ml ht": "HALF_TIME_1X2",
    "totals 2h": "SECOND_HALF_GOALS",
    "european handicap": "EUROPEAN_HANDICAP",
    "correct score": "CORRECT_SCORE",
    "half time full time": "HALF_TIME_FULL_TIME",
    "corner handicap": "CORNER_HANDICAP",
    "corners spread": "CORNER_HANDICAP",
    "corners 2 way": "MOST_CORNERS",
    "most corners": "MOST_CORNERS",
    "most shots": "MOST_SHOTS",
    "most shots on target": "MOST_SHOTS_ON_TARGET",
    "exact total goals": "GOAL_BANDS",
    "number of goals in match": "GOAL_BANDS",
}


def _norm(odds: Any, value: Any) -> str:
    return odds.normalize_text(value or "", drop_suffixes=True)


def _raw_name(odds: Any, market: Dict[str, Any]) -> str:
    return str(odds.raw_market_name(market) or "").strip()


def _rows(odds: Any, market: Dict[str, Any]) -> List[Dict[str, Any]]:
    return list(odds.outcome_rows(market) or [])


def _first_not_none(*values: Optional[float]) -> Optional[float]:
    for value in values:
        if value is not None:
            return value
    return None


def _price(odds: Any, row: Dict[str, Any]) -> Optional[float]:
    return _first_not_none(
        odds.row_price(row),
        odds.to_float(row.get("over")),
        odds.to_float(row.get("under")),
    )


def _line(odds: Any, market: Dict[str, Any], row: Dict[str, Any]) -> Optional[float]:
    return _first_not_none(
        odds.row_line(row),
        odds.row_line(market),
        odds.line_from_text(_raw_name(odds, market)),
    )


def _team(odds: Any, market_name: str, row: Dict[str, Any], home: str, away: str) -> Optional[str]:
    return odds.team_from_market_or_row(market_name, row, home, away)


def _add(
    odds: Any,
    out: List[Dict[str, Any]],
    market: str,
    selection: str,
    price: Optional[float],
    bookmaker: str,
    *,
    line: Optional[float] = None,
    team: Optional[str] = None,
) -> None:
    if not selection.strip():
        return
    odds.add_market(out, market, selection.strip(), price, bookmaker, line=line, team=team)


def _dedupe(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    seen = set()
    for row in rows:
        key = (
            row.get("market"),
            row.get("selection"),
            row.get("bookmaker"),
            row.get("line"),
            row.get("team"),
            row.get("odds"),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    return result


def _selection_from_label(odds: Any, label: str, home: str, away: str) -> Optional[str]:
    normalized = _norm(odds, label)
    if normalized in {"1", "home", "home win"} or normalized == _norm(odds, home):
        return "Home"
    if normalized in {"x", "draw", "tie"}:
        return "Draw"
    if normalized in {"2", "away", "away win"} or normalized == _norm(odds, away):
        return "Away"
    return None


def _three_way(
    odds: Any,
    market: Dict[str, Any],
    canonical: str,
    bookmaker: str,
    home: str,
    away: str,
    *,
    line_required: bool = False,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in _rows(odds, market):
        line = _line(odds, market, row) if line_required else None
        if line_required and line is None:
            continue
        home_price = odds.row_side_price(row, "home")
        draw_price = odds.row_side_price(row, "draw")
        away_price = odds.row_side_price(row, "away")
        if home_price is not None or draw_price is not None or away_price is not None:
            _add(odds, out, canonical, "Home", home_price, bookmaker, line=line, team=home)
            _add(odds, out, canonical, "Draw", draw_price, bookmaker, line=line)
            _add(odds, out, canonical, "Away", away_price, bookmaker, line=line, team=away)
            continue
        label = str(odds.row_name(row) or "").strip()
        side = _selection_from_label(odds, label, home, away)
        if side:
            _add(
                odds,
                out,
                canonical,
                side,
                _price(odds, row),
                bookmaker,
                line=line,
                team=home if side == "Home" else away if side == "Away" else None,
            )
    return _dedupe(out)


def _double_chance(
    odds: Any,
    market: Dict[str, Any],
    canonical: str,
    bookmaker: str,
    home: str,
    away: str,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    home_norm = _norm(odds, home)
    away_norm = _norm(odds, away)
    for row in _rows(odds, market):
        direct = {
            "1X": odds.to_float(row.get("1X") or row.get("1x")),
            "12": odds.to_float(row.get("12")),
            "X2": odds.to_float(row.get("X2") or row.get("x2") or row.get("2X") or row.get("2x")),
        }
        if any(value is not None for value in direct.values()):
            for selection, value in direct.items():
                _add(odds, out, canonical, selection, value, bookmaker)
            continue
        label = _norm(odds, odds.row_name(row))
        price = _price(odds, row)
        if price is None:
            continue
        has_home = "home" in label or (home_norm and home_norm in label)
        has_away = "away" in label or (away_norm and away_norm in label)
        has_draw = "draw" in label or "tie" in label
        if label in {"1x", "home or draw", "home draw"} or (has_home and has_draw):
            _add(odds, out, canonical, "1X", price, bookmaker)
        elif label in {"x2", "2x", "draw or away", "away or draw"} or (has_away and has_draw):
            _add(odds, out, canonical, "X2", price, bookmaker)
        elif label in {"12", "home or away", "no draw"} or (has_home and has_away and not has_draw):
            _add(odds, out, canonical, "12", price, bookmaker)
    return _dedupe(out)


def _yes_no(
    odds: Any,
    market: Dict[str, Any],
    canonical: str,
    bookmaker: str,
    *,
    team_market: bool = False,
    home: str = "",
    away: str = "",
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    raw = _raw_name(odds, market)
    for row in _rows(odds, market):
        team = _team(odds, raw, row, home, away) if team_market else None
        if team_market and not team:
            continue
        yes = odds.row_side_price(row, "yes")
        no = odds.row_side_price(row, "no")
        if yes is not None or no is not None:
            _add(odds, out, canonical, "Yes", yes, bookmaker, team=team)
            _add(odds, out, canonical, "No", no, bookmaker, team=team)
            continue
        label = _norm(odds, odds.row_name(row))
        price = _price(odds, row)
        if label in {"yes", "y"} or " yes" in f" {label}":
            _add(odds, out, canonical, "Yes", price, bookmaker, team=team)
        elif label in {"no", "n"} or " no" in f" {label}":
            _add(odds, out, canonical, "No", price, bookmaker, team=team)
    return _dedupe(out)


def _totals(
    odds: Any,
    market: Dict[str, Any],
    canonical: str,
    bookmaker: str,
    home: str,
    away: str,
    *,
    team_market: bool = False,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    raw = _raw_name(odds, market)
    for row in _rows(odds, market):
        line = _line(odds, market, row)
        if line is None:
            continue
        team = _team(odds, raw, row, home, away) if team_market else None
        if team_market and not team:
            continue
        over = odds.row_side_price(row, "over")
        under = odds.row_side_price(row, "under")
        prefix = team or {
            "TEAM_FIRST_HALF_GOALS": "1H Team Goals",
            "SECOND_HALF_GOALS": "2H Goals",
            "TEAM_SECOND_HALF_GOALS": "2H Team Goals",
            "MATCH_YELLOW_CARDS": "Yellow Cards",
            "TEAM_YELLOW_CARDS": "Team Yellow Cards",
        }.get(canonical, canonical.replace("_", " ").title())
        if over is not None or under is not None:
            _add(odds, out, canonical, f"{prefix} Over {line:g}", over, bookmaker, line=line, team=team)
            _add(odds, out, canonical, f"{prefix} Under {line:g}", under, bookmaker, line=line, team=team)
            continue
        label = _norm(odds, odds.row_name(row))
        if "over" in label:
            _add(odds, out, canonical, f"{prefix} Over {line:g}", _price(odds, row), bookmaker, line=line, team=team)
        elif "under" in label:
            _add(odds, out, canonical, f"{prefix} Under {line:g}", _price(odds, row), bookmaker, line=line, team=team)
    return _dedupe(out)


def _canonical_label(raw_label: str) -> str:
    return re.sub(r"\s+", " ", str(raw_label or "").strip())


def _label_rows(
    odds: Any,
    market: Dict[str, Any],
    canonical: str,
    bookmaker: str,
    *,
    line_from_label: bool = False,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in _rows(odds, market):
        label = _canonical_label(odds.row_name(row))
        price = _price(odds, row)
        line = _line(odds, market, row) if line_from_label else None
        if label and price is not None:
            _add(odds, out, canonical, label, price, bookmaker, line=line)
    return _dedupe(out)


def _goal_band_label(label: str) -> Optional[str]:
    text = _canonical_label(label).lower().replace("goals", "").strip()
    range_match = re.search(r"(\d+)\s*[-–]\s*(\d+)", text)
    if range_match:
        return f"{int(range_match.group(1))}-{int(range_match.group(2))}"
    plus_match = re.search(r"(\d+)\s*\+", text)
    if plus_match:
        return f"{int(plus_match.group(1))}+"
    single_match = re.fullmatch(r"(?:exactly\s*)?(\d+)", text)
    if single_match:
        value = int(single_match.group(1))
        return f"{value}-{value}"
    if text in {"no goal", "no goals"}:
        return "0-0"
    return None


def _goal_bands(odds: Any, market: Dict[str, Any], bookmaker: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in _rows(odds, market):
        raw_label = str(odds.row_name(row) or "").strip()
        label = _goal_band_label(raw_label)
        price = _price(odds, row)
        if label and price is not None:
            _add(odds, out, "GOAL_BANDS", label, price, bookmaker)
    return _dedupe(out)


def classify_new_market(odds: Any, market: Dict[str, Any]) -> Optional[str]:
    raw = _norm(odds, _raw_name(odds, market))
    if not raw:
        return None
    if raw in CONFIRMED_PROVIDER_NAMES:
        return CONFIRMED_PROVIDER_NAMES[raw]

    half = any(token in raw for token in ("half time", "halftime", "first half", "1st half", " ht"))
    second = any(token in raw for token in ("second half", "2nd half", "2h"))
    team = any(token in raw for token in ("team", "home", "away"))

    if "most shots on target" in raw or "shots on target result" in raw:
        return "MOST_SHOTS_ON_TARGET"
    if "most shots" in raw or "shots result" in raw:
        return "MOST_SHOTS"
    if "most corners" in raw or "corners result" in raw or "corner match bet" in raw:
        return "MOST_CORNERS"
    if "corner" in raw and any(token in raw for token in ("handicap", "spread")):
        return "CORNER_HANDICAP"

    if half and "double chance" in raw:
        return "HALF_TIME_DOUBLE_CHANCE"
    if half and any(token in raw for token in ("result", "1x2", "winner", "moneyline", "ml ht")):
        return "HALF_TIME_1X2"
    if half and team and any(token in raw for token in ("goal", "total", "over under")):
        return "TEAM_FIRST_HALF_GOALS"
    if second and team and any(token in raw for token in ("goal", "total", "over under")):
        return "TEAM_SECOND_HALF_GOALS"
    if second and any(token in raw for token in ("goal", "total", "over under")):
        return "SECOND_HALF_GOALS"

    if "draw no bet" in raw or raw == "dnb":
        return "DRAW_NO_BET"
    if "european handicap" in raw or "3 way handicap" in raw or "3way handicap" in raw:
        return "EUROPEAN_HANDICAP"
    if "winning margin" in raw or "margin of victory" in raw:
        return "WINNING_MARGIN"
    if "correct score" in raw or "exact score" in raw:
        return "CORRECT_SCORE"
    if any(token in raw for token in ("half time full time", "halftime fulltime", "ht ft")):
        return "HALF_TIME_FULL_TIME"

    if "yellow" in raw and any(token in raw for token in ("card", "booking")):
        return "TEAM_YELLOW_CARDS" if team else "MATCH_YELLOW_CARDS"
    if "red card" in raw:
        return "RED_CARD"

    if "both halves" in raw and team:
        return "TEAM_SCORE_BOTH_HALVES"
    if "both halves" in raw or "each half" in raw:
        return "GOAL_IN_BOTH_HALVES"
    if "odd even" in raw or "odd/even" in raw:
        return "ODD_EVEN_GOALS"
    if any(token in raw for token in ("goal band", "goals range", "total goals range", "exact total goals")):
        return "GOAL_BANDS"
    if ("both teams" in raw or "btts" in raw) and any(token in raw for token in ("over", "under", "total")):
        return "BTTS_OVER_UNDER"
    if any(token in raw for token in ("result and", "result plus", "win and")) and any(
        token in raw for token in ("over", "under", "total")
    ):
        return "RESULT_OVER_UNDER"
    return None


def normalize_new_market(
    odds: Any,
    market: Dict[str, Any],
    bookmaker: str,
    home: str,
    away: str,
) -> Optional[List[Dict[str, Any]]]:
    family = classify_new_market(odds, market)
    if family is None:
        return None
    if family in {"HALF_TIME_1X2", "MOST_CORNERS", "MOST_SHOTS", "MOST_SHOTS_ON_TARGET"}:
        return _three_way(odds, market, family, bookmaker, home, away)
    if family == "HALF_TIME_DOUBLE_CHANCE":
        return _double_chance(odds, market, family, bookmaker, home, away)
    if family == "DRAW_NO_BET":
        return [row for row in _three_way(odds, market, family, bookmaker, home, away) if row.get("selection") != "Draw"]
    if family in {"EUROPEAN_HANDICAP", "CORNER_HANDICAP"}:
        return _three_way(odds, market, family, bookmaker, home, away, line_required=True)
    if family in {"TEAM_FIRST_HALF_GOALS", "TEAM_SECOND_HALF_GOALS", "TEAM_YELLOW_CARDS"}:
        return _totals(odds, market, family, bookmaker, home, away, team_market=True)
    if family in {"SECOND_HALF_GOALS", "MATCH_YELLOW_CARDS"}:
        return _totals(odds, market, family, bookmaker, home, away)
    if family in {"RED_CARD", "GOAL_IN_BOTH_HALVES"}:
        return _yes_no(odds, market, family, bookmaker)
    if family == "TEAM_SCORE_BOTH_HALVES":
        return _yes_no(odds, market, family, bookmaker, team_market=True, home=home, away=away)
    if family == "GOAL_BANDS":
        return _goal_bands(odds, market, bookmaker)
    if family in {"ODD_EVEN_GOALS", "WINNING_MARGIN", "HALF_TIME_FULL_TIME", "CORRECT_SCORE"}:
        return _label_rows(odds, market, family, bookmaker)
    if family in {"BTTS_OVER_UNDER", "RESULT_OVER_UNDER"}:
        return _label_rows(odds, market, family, bookmaker, line_from_label=True)
    return []


def _record_expansion_debug(debug: Dict[str, Any], raw: str, family: str, count: int) -> None:
    bucket = debug.setdefault("expandedExactMarketCounts", {})
    bucket[family] = int(bucket.get(family, 0)) + count
    raw_bucket = debug.setdefault("expandedExactRawMarkets", {})
    item = raw_bucket.setdefault(raw, {"family": family, "payloads": 0, "selections": 0})
    item["payloads"] += 1
    item["selections"] += count


def install(odds_module: Any, pipeline_module: Any = None) -> None:
    """Install exact-market normalization once."""
    if getattr(odds_module, "_statmaker_market_v15_installed", False):
        return
    original = odds_module.normalize_market

    def expanded(
        market: Dict[str, Any],
        bookmaker: str,
        home: str,
        away: str,
        debug: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        normalized = normalize_new_market(odds_module, market, bookmaker, home, away)
        if normalized is None:
            return original(market, bookmaker, home, away, debug)
        family = classify_new_market(odds_module, market) or "OTHER"
        raw = _raw_name(odds_module, market)
        odds_module.record_raw_market(debug, raw, family)
        _record_expansion_debug(debug, raw, family, len(normalized))
        if not normalized:
            odds_module.record_skipped_market(
                debug,
                raw,
                "recognized expanded market but no exact outcome rows were normalized",
                family_override=family,
            )
        return normalized

    odds_module.SUPPORTED_MARKETS.update(NEW_MARKETS)
    for market in sorted(NEW_MARKETS):
        if market not in odds_module.EMITTED_MARKET_COUNT_KEYS:
            odds_module.EMITTED_MARKET_COUNT_KEYS.append(market)
    odds_module.normalize_market = expanded
    odds_module._statmaker_market_v15_installed = True
