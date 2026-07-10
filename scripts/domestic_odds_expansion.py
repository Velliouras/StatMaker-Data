#!/usr/bin/env python3
"""Production adapter for exact Double Chance and canonical team aliases.

This module extends the shared Odds-API.io normalization boundary. It does not
create a second betting engine and never estimates prices. Every emitted market
retains the bookmaker's exact decimal odd.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

TEAM_NAME_PREFIX_TOKENS = {
    "club", "clube", "deportivo", "deportes", "sporting", "atletico",
    "association", "asociacion", "fotbal", "fotboll", "football",
    "sociedad", "racing", "royal", "real", "cd", "cs", "acs", "asc",
    "rks", "wks", "kks", "ks", "lkp", "gks", "afk",
}


def simplified_team_name(odds_module: Any, value: Any) -> str:
    words = odds_module.normalize_text(value, drop_suffixes=True).split()
    while len(words) > 1 and words[0] in TEAM_NAME_PREFIX_TOKENS:
        words.pop(0)
    while len(words) > 1 and words[-1].isdigit() and len(words[-1]) == 4:
        words.pop()
    return " ".join(words)


def canonical_team_info(
    odds_module: Any,
    name: str,
    league_code: str,
    aliases: Dict[str, Dict[str, str]],
    debug: Dict[str, Any],
) -> Tuple[str, Optional[str]]:
    normalized = odds_module.normalize_text(name, drop_suffixes=True)
    simplified = simplified_team_name(odds_module, normalized)
    league_aliases = aliases.get(league_code, {})
    for candidate in dict.fromkeys([normalized, simplified]):
        if candidate and candidate in league_aliases:
            canonical = league_aliases[candidate]
            return canonical, canonical
    odds_module.record_unmatched_team(debug, league_code, str(name or "").strip(), normalized)
    provider_name = str(name or "").strip()
    return provider_name, None


def generated_aliases(
    odds_module: Any,
    stats_fetch_module: Any,
    load_json: Any,
    registry: Sequence[Dict[str, Any]],
) -> Dict[str, Dict[str, str]]:
    aliases = odds_module.load_aliases()
    for league in registry:
        code = str(league.get("leagueCode") or "")
        bucket = aliases.setdefault(code, {})
        cache = load_json(stats_fetch_module.cache_path_for(league), {})
        canonical_names: Set[str] = {
            str(fixture.get(key) or "").strip()
            for fixture in cache.get("fixtures", []) or []
            if isinstance(fixture, dict)
            for key in ("home_team", "away_team")
            if str(fixture.get(key) or "").strip()
        }
        owners: Dict[str, Set[str]] = {}
        for canonical in canonical_names:
            for variant in {
                odds_module.normalize_text(canonical, drop_suffixes=True),
                simplified_team_name(odds_module, canonical),
            }:
                if variant:
                    owners.setdefault(variant, set()).add(canonical)
        for variant, candidates in owners.items():
            if len(candidates) == 1:
                bucket.setdefault(variant, next(iter(candidates)))
    return aliases


def normalize_double_chance(
    odds_module: Any,
    market: Dict[str, Any],
    bookmaker: str,
    home: str,
    away: str,
    debug: Dict[str, Any],
) -> List[Dict[str, Any]]:
    raw_name = odds_module.raw_market_name(market)
    audit = odds_module.record_market_audit(
        debug,
        raw_name,
        {
            "bookmaker": bookmaker,
            "fixture": f"{home} - {away}",
            "marketSample": odds_module.compact(market, 700),
        },
        classification_text=odds_module.provider_market_text(market),
    )
    family = audit["family"]
    odds_module.record_raw_market(debug, raw_name, family)
    if audit["status"] != "supported":
        odds_module.record_skipped_market(
            debug,
            raw_name,
            audit["reason"],
            family_override=family,
        )
        return []

    rows = odds_module.outcome_rows(market)
    out: List[Dict[str, Any]] = []
    if not rows:
        odds_module.record_skipped_market(debug, raw_name, "no outcome rows")
        return out

    for row in rows:
        direct_1x = odds_module.to_float(row.get("1X") or row.get("1x"))
        direct_12 = odds_module.to_float(row.get("12"))
        direct_x2 = odds_module.to_float(
            row.get("X2") or row.get("x2") or row.get("2X") or row.get("2x")
        )
        if direct_1x is not None or direct_12 is not None or direct_x2 is not None:
            odds_module.add_market(out, "DOUBLE_CHANCE", "1X", direct_1x, bookmaker)
            odds_module.add_market(out, "DOUBLE_CHANCE", "12", direct_12, bookmaker)
            odds_module.add_market(out, "DOUBLE_CHANCE", "X2", direct_x2, bookmaker)
            continue

        label = odds_module.normalize_text(odds_module.row_name(row), drop_suffixes=True)
        price = odds_module.to_float(row.get("under") or row.get("over")) or odds_module.row_price(row)
        if not label or price is None:
            continue
        if label in {"1x", "home or draw", "home draw"} or label.endswith(" or draw"):
            odds_module.add_market(out, "DOUBLE_CHANCE", "1X", price, bookmaker)
        elif label in {"x2", "2x", "draw or away", "away or draw"} or label.startswith("draw or "):
            odds_module.add_market(out, "DOUBLE_CHANCE", "X2", price, bookmaker)
        elif label in {"12", "home or away", "no draw"} or (" or " in label and "draw" not in label):
            odds_module.add_market(out, "DOUBLE_CHANCE", "12", price, bookmaker)
        else:
            odds_module.record_skipped_market(
                debug,
                raw_name,
                "unrecognized Double Chance row",
                odds_module.row_name(row),
            )
    return out


def install(odds_module: Any, pipeline_module: Any) -> None:
    """Install the adapter once into the shared Domestic odds ingestion path."""
    if getattr(odds_module, "_statmaker_domestic_expansion_installed", False):
        return

    original_normalize_market = odds_module.normalize_market

    def expanded_normalize_market(
        market: Dict[str, Any],
        bookmaker: str,
        home: str,
        away: str,
        debug: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        family = odds_module.market_family_from_name(odds_module.raw_market_name(market))
        if family == "DOUBLE_CHANCE":
            return normalize_double_chance(
                odds_module,
                market,
                bookmaker,
                home,
                away,
                debug,
            )
        return original_normalize_market(market, bookmaker, home, away, debug)

    def expanded_canonical_team_info(
        name: str,
        league_code: str,
        aliases: Dict[str, Dict[str, str]],
        debug: Dict[str, Any],
    ) -> Tuple[str, Optional[str]]:
        return canonical_team_info(odds_module, name, league_code, aliases, debug)

    def expanded_generated_aliases(
        registry: Sequence[Dict[str, Any]],
    ) -> Dict[str, Dict[str, str]]:
        return generated_aliases(
            odds_module,
            pipeline_module.stats_fetch,
            pipeline_module.load_json,
            registry,
        )

    odds_module.SUPPORTED_MARKETS.add("DOUBLE_CHANCE")
    if "DOUBLE_CHANCE" not in odds_module.EMITTED_MARKET_COUNT_KEYS:
        odds_module.EMITTED_MARKET_COUNT_KEYS.append("DOUBLE_CHANCE")
    odds_module.normalize_market = expanded_normalize_market
    odds_module.canonical_team_info = expanded_canonical_team_info
    pipeline_module.generated_aliases = expanded_generated_aliases
    odds_module._statmaker_domestic_expansion_installed = True
