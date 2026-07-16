#!/usr/bin/env python3
"""Exact Domestic market normalization for the expanded StatMaker catalogue.

Only prices present in Odds-API.io bookmaker payloads are emitted. No implied,
converted, synthetic, or estimated odds are created.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional

VERSION = "domestic-market-expansion-v16"

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

ALIASES = {
    "draw no bet": "DRAW_NO_BET",
    "dnb": "DRAW_NO_BET",
    "half time result": "HALF_TIME_1X2",
    "ml ht": "HALF_TIME_1X2",
    "half time 1x2": "HALF_TIME_1X2",
    "1st half result": "HALF_TIME_1X2",
    "first half result": "HALF_TIME_1X2",
    "double chance ht": "HALF_TIME_DOUBLE_CHANCE",
    "double chance 1h": "HALF_TIME_DOUBLE_CHANCE",
    "half time double chance": "HALF_TIME_DOUBLE_CHANCE",
    "1st half double chance": "HALF_TIME_DOUBLE_CHANCE",
    "first half double chance": "HALF_TIME_DOUBLE_CHANCE",
    "totals 2h": "SECOND_HALF_GOALS",
    "2nd half goal line": "SECOND_HALF_GOALS",
    "second half goal line": "SECOND_HALF_GOALS",
    "team total ht home": "TEAM_FIRST_HALF_GOALS",
    "team total ht away": "TEAM_FIRST_HALF_GOALS",
    "team total 1h home": "TEAM_FIRST_HALF_GOALS",
    "team total 1h away": "TEAM_FIRST_HALF_GOALS",
    "1st half team total home": "TEAM_FIRST_HALF_GOALS",
    "1st half team total away": "TEAM_FIRST_HALF_GOALS",
    "team total 2h home": "TEAM_SECOND_HALF_GOALS",
    "team total 2h away": "TEAM_SECOND_HALF_GOALS",
    "2nd half team total home": "TEAM_SECOND_HALF_GOALS",
    "2nd half team total away": "TEAM_SECOND_HALF_GOALS",
    "bookings totals": "MATCH_YELLOW_CARDS",
    "number of cards in match": "MATCH_YELLOW_CARDS",
    "yellow cards totals": "MATCH_YELLOW_CARDS",
    "team cards home": "TEAM_YELLOW_CARDS",
    "team cards away": "TEAM_YELLOW_CARDS",
    "team bookings home": "TEAM_YELLOW_CARDS",
    "team bookings away": "TEAM_YELLOW_CARDS",
    "red card": "RED_CARD",
    "red card in match": "RED_CARD",
    "sending off": "RED_CARD",
    "most corners": "MOST_CORNERS",
    "corner match bet": "MOST_CORNERS",
    "corners result": "MOST_CORNERS",
    "corner handicap": "CORNER_HANDICAP",
    "corners spread": "CORNER_HANDICAP",
    "most shots": "MOST_SHOTS",
    "most shots on target": "MOST_SHOTS_ON_TARGET",
    "goal in both halves": "GOAL_IN_BOTH_HALVES",
    "goal scored in both halves": "GOAL_IN_BOTH_HALVES",
    "team to score in both halves": "TEAM_SCORE_BOTH_HALVES",
    "team score both halves": "TEAM_SCORE_BOTH_HALVES",
    "european handicap": "EUROPEAN_HANDICAP",
    "3 way handicap": "EUROPEAN_HANDICAP",
    "3way handicap": "EUROPEAN_HANDICAP",
    "odd even": "ODD_EVEN_GOALS",
    "odd even goals": "ODD_EVEN_GOALS",
    "exact total goals": "GOAL_BANDS",
    "number of goals in match": "GOAL_BANDS",
    "goal bands": "GOAL_BANDS",
    "winning margin": "WINNING_MARGIN",
    "margin of victory": "WINNING_MARGIN",
    "half time full time": "HALF_TIME_FULL_TIME",
    "halftime fulltime": "HALF_TIME_FULL_TIME",
    "ht ft": "HALF_TIME_FULL_TIME",
    "correct score": "CORRECT_SCORE",
    "exact score": "CORRECT_SCORE",
}


def _norm(odds: Any, value: Any) -> str:
    return odds.normalize_text(value or "", drop_suffixes=True)


def _raw(odds: Any, market: Dict[str, Any]) -> str:
    return str(odds.raw_market_name(market) or "").strip()


def _rows(odds: Any, market: Dict[str, Any]) -> List[Dict[str, Any]]:
    return list(odds.outcome_rows(market) or [])


def _to_float(odds: Any, value: Any) -> Optional[float]:
    return odds.to_float(value)


def _first_price(odds: Any, row: Dict[str, Any], *preferred: str) -> Optional[float]:
    for key in preferred:
        value = _to_float(odds, row.get(key))
        if value is not None:
            return value
    value = odds.row_price(row)
    if value is not None:
        return value
    for key in ("under", "over", "home", "draw", "away", "yes", "no", "odd", "even"):
        value = _to_float(odds, row.get(key))
        if value is not None:
            return value
    return None


def _line(odds: Any, market: Dict[str, Any], row: Dict[str, Any]) -> Optional[float]:
    for value in (
        odds.row_line(row),
        odds.row_line(market),
        odds.line_from_text(_raw(odds, market)),
    ):
        if value is not None:
            return value
    return None


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
    if not str(selection or "").strip():
        return
    odds.add_market(
        out,
        market,
        str(selection).strip(),
        price,
        bookmaker,
        line=line,
        team=team,
    )


def _dedupe(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
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
        if key not in seen:
            seen.add(key)
            out.append(row)
    return out


def _team_from_name(odds: Any, raw: str, row: Dict[str, Any], home: str, away: str) -> Optional[str]:
    normalized = _norm(odds, raw)
    if "home" in normalized:
        return home
    if "away" in normalized:
        return away
    return odds.team_from_market_or_row(raw, row, home, away)


def _label_side(odds: Any, label: str, home: str, away: str) -> Optional[str]:
    text = _norm(odds, label)
    if not text:
        return None
    if text in {"1", "home", "home win"} or text == _norm(odds, home) or text.startswith("1 "):
        return "Home"
    if text in {"x", "draw", "tie"} or text.startswith("draw ") or text.startswith("tie "):
        return "Draw"
    if text in {"2", "away", "away win"} or text == _norm(odds, away) or text.startswith("2 "):
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
        side = _label_side(odds, str(odds.row_name(row) or ""), home, away)
        if side:
            preferred = {
                "Home": ("home", "under", "over", "away", "draw"),
                "Draw": ("draw", "under", "over", "away", "home"),
                "Away": ("away", "under", "over", "home", "draw"),
            }[side]
            _add(
                odds,
                out,
                canonical,
                side,
                _first_price(odds, row, *preferred),
                bookmaker,
                line=line,
                team=home if side == "Home" else away if side == "Away" else None,
            )
            continue
        home_price = odds.row_side_price(row, "home")
        draw_price = odds.row_side_price(row, "draw")
        away_price = odds.row_side_price(row, "away")
        _add(odds, out, canonical, "Home", home_price, bookmaker, line=line, team=home)
        _add(odds, out, canonical, "Draw", draw_price, bookmaker, line=line)
        _add(odds, out, canonical, "Away", away_price, bookmaker, line=line, team=away)
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
            "1X": _to_float(odds, row.get("1X") or row.get("1x")),
            "12": _to_float(odds, row.get("12")),
            "X2": _to_float(odds, row.get("X2") or row.get("x2") or row.get("2X") or row.get("2x")),
        }
        if any(value is not None for value in direct.values()):
            for selection, value in direct.items():
                _add(odds, out, canonical, selection, value, bookmaker)
            continue
        label = _norm(odds, odds.row_name(row))
        price = _first_price(odds, row)
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
    raw = _raw(odds, market)
    for row in _rows(odds, market):
        line = _line(odds, market, row)
        if line is None:
            continue
        team = _team_from_name(odds, raw, row, home, away) if team_market else None
        if team_market and not team:
            continue
        prefix = team or {
            "TEAM_FIRST_HALF_GOALS": "1H Team Goals",
            "SECOND_HALF_GOALS": "2H Goals",
            "TEAM_SECOND_HALF_GOALS": "2H Team Goals",
            "MATCH_YELLOW_CARDS": "Yellow Cards",
            "TEAM_YELLOW_CARDS": "Team Yellow Cards",
        }.get(canonical, canonical.replace("_", " ").title())
        over = odds.row_side_price(row, "over")
        under = odds.row_side_price(row, "under")
        _add(odds, out, canonical, f"{prefix} Over {line:g}", over, bookmaker, line=line, team=team)
        _add(odds, out, canonical, f"{prefix} Under {line:g}", under, bookmaker, line=line, team=team)
    return _dedupe(out)


def _yes_no(
    odds: Any,
    market: Dict[str, Any],
    canonical: str,
    bookmaker: str,
    home: str = "",
    away: str = "",
    *,
    team_market: bool = False,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    raw = _raw(odds, market)
    for row in _rows(odds, market):
        team = _team_from_name(odds, raw, row, home, away) if team_market else None
        if team_market and not team:
            continue
        yes = odds.row_side_price(row, "yes")
        no = odds.row_side_price(row, "no")
        if yes is not None or no is not None:
            _add(odds, out, canonical, "Yes", yes, bookmaker, team=team)
            _add(odds, out, canonical, "No", no, bookmaker, team=team)
            continue
        label = _norm(odds, odds.row_name(row))
        if label in {"yes", "y"} or label.endswith(" yes"):
            _add(odds, out, canonical, "Yes", _first_price(odds, row), bookmaker, team=team)
        elif label in {"no", "n"} or label.endswith(" no"):
            _add(odds, out, canonical, "No", _first_price(odds, row), bookmaker, team=team)
    return _dedupe(out)


def _odd_even(odds: Any, market: Dict[str, Any], bookmaker: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in _rows(odds, market):
        odd = _to_float(odds, row.get("odd"))
        even = _to_float(odds, row.get("even"))
        _add(odds, out, "ODD_EVEN_GOALS", "Odd", odd, bookmaker)
        _add(odds, out, "ODD_EVEN_GOALS", "Even", even, bookmaker)
        label = _norm(odds, odds.row_name(row))
        if label == "odd":
            _add(odds, out, "ODD_EVEN_GOALS", "Odd", _first_price(odds, row), bookmaker)
        elif label == "even":
            _add(odds, out, "ODD_EVEN_GOALS", "Even", _first_price(odds, row), bookmaker)
    return _dedupe(out)


def _label_rows(
    odds: Any,
    market: Dict[str, Any],
    canonical: str,
    bookmaker: str,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in _rows(odds, market):
        label = re.sub(r"\s+", " ", str(odds.row_name(row) or "").strip())
        _add(odds, out, canonical, label, _first_price(odds, row), bookmaker, line=_line(odds, market, row))
    return _dedupe(out)


def _goal_band(label: str) -> Optional[str]:
    text = re.sub(r"\s+", " ", str(label or "").lower()).strip()
    text = text.replace("goals", "").replace("goal", "").strip()
    match = re.search(r"^under\s+(\d+)$", text)
    if match:
        ceiling = int(match.group(1))
        return f"0-{max(0, ceiling - 1)}"
    match = re.search(r"^over\s+(\d+)$", text)
    if match:
        return f"{int(match.group(1)) + 1}+"
    match = re.search(r"(\d+)\s*(?:-|–|or)\s*(\d+)", text)
    if match:
        return f"{int(match.group(1))}-{int(match.group(2))}"
    match = re.search(r"(\d+)\s*\+", text)
    if match:
        return f"{int(match.group(1))}+"
    match = re.fullmatch(r"(?:exactly\s*)?(\d+)", text)
    if match:
        value = int(match.group(1))
        return f"{value}-{value}"
    if text in {"no", "none"}:
        return "0-0"
    return None


def _goal_bands(odds: Any, market: Dict[str, Any], bookmaker: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in _rows(odds, market):
        label = _goal_band(str(odds.row_name(row) or ""))
        if label:
            _add(odds, out, "GOAL_BANDS", label, _first_price(odds, row), bookmaker)
    return _dedupe(out)


def classify_new_market(odds: Any, market: Dict[str, Any]) -> Optional[str]:
    raw = _norm(odds, _raw(odds, market))
    if not raw:
        return None
    if raw == "corners 2 way":
        return None
    if raw in ALIASES:
        return ALIASES[raw]

    half = any(token in raw for token in ("half time", "halftime", "first half", "1st half", " 1h"))
    second = any(token in raw for token in ("second half", "2nd half", " 2h"))
    team = any(token in raw for token in ("team", "home", "away"))

    if half and "double chance" in raw:
        return "HALF_TIME_DOUBLE_CHANCE"
    if half and team and any(token in raw for token in ("goal", "total", "over under")):
        return "TEAM_FIRST_HALF_GOALS"
    if second and team and any(token in raw for token in ("goal", "total", "over under")):
        return "TEAM_SECOND_HALF_GOALS"
    if second and any(token in raw for token in ("goal", "total", "over under")):
        return "SECOND_HALF_GOALS"
    if "yellow" in raw and any(token in raw for token in ("card", "booking")):
        return "TEAM_YELLOW_CARDS" if team else "MATCH_YELLOW_CARDS"
    if "red card" in raw or "sending off" in raw:
        return "RED_CARD"
    if "most shots on target" in raw:
        return "MOST_SHOTS_ON_TARGET"
    if "most shots" in raw:
        return "MOST_SHOTS"
    if "most corners" in raw:
        return "MOST_CORNERS"
    if "corner" in raw and any(token in raw for token in ("handicap", "spread")):
        return "CORNER_HANDICAP"
    if "both halves" in raw and team:
        return "TEAM_SCORE_BOTH_HALVES"
    if "both halves" in raw or "each half" in raw:
        return "GOAL_IN_BOTH_HALVES"
    if "odd even" in raw:
        return "ODD_EVEN_GOALS"
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
        return _yes_no(odds, market, family, bookmaker, home, away, team_market=True)
    if family == "ODD_EVEN_GOALS":
        return _odd_even(odds, market, bookmaker)
    if family == "GOAL_BANDS":
        return _goal_bands(odds, market, bookmaker)
    if family in {"WINNING_MARGIN", "HALF_TIME_FULL_TIME", "CORRECT_SCORE", "BTTS_OVER_UNDER", "RESULT_OVER_UNDER"}:
        return _label_rows(odds, market, family, bookmaker)
    return []


def _record(debug: Dict[str, Any], raw: str, family: str, count: int) -> None:
    counts = debug.setdefault("expandedExactMarketCounts", {})
    counts[family] = int(counts.get(family, 0)) + count
    details = debug.setdefault("expandedExactRawMarkets", {})
    item = details.setdefault(raw, {"family": family, "payloads": 0, "selections": 0})
    item["payloads"] += 1
    item["selections"] += count


def install(odds_module: Any, pipeline_module: Any = None) -> None:
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
        raw = _raw(odds_module, market)
        odds_module.record_raw_market(debug, raw, family)
        _record(debug, raw, family, len(normalized))
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
