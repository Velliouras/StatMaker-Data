#!/usr/bin/env python3
"""Shared Domestic Odds-API.io ingestion extensions.

The canonical ``markets`` list remains the only app betting input. In parallel,
all bookmaker market payloads returned by Odds-API.io are retained in a separate
provider archive. No price is estimated or converted, and unsupported market
families never enter the betting engine.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

TEAM_NAME_PREFIX_TOKENS = {
    "club", "clube", "deportivo", "deportes", "sporting", "atletico",
    "association", "asociacion", "fotbal", "fotboll", "football",
    "sociedad", "racing", "royal", "real", "cd", "cs", "acs", "asc",
    "rks", "wks", "kks", "ks", "lkp", "gks", "afk",
}

VERIFIED_PROVIDER_SLUGS = {
    "ARG": "argentina-liga-profesional",
    "BRA": "brazil-serie-a",
    "BRA2": "brazil-serie-b",
    "IRL": "ireland-premier-division",
    "USA": "usa-mls",
    "CHN": "china-chinese-super-league",
    "NOR": "norway-eliteserien",
    "SWE": "sweden-allsvenskan",
    "SWE2": "sweden-superettan",
    "FIN": "finland-veikkausliiga",
    "MEX": "mexico-liga-mx-apertura",
    "ROM": "romania-superliga",
    "DNK": "denmark-superliga",
    "POL": "poland-ekstraklasa",
    "RUS": "russia-premier-league",
    "SWZ": "switzerland-super-league",
    "AUT": "austria-bundesliga",
    "AUT2": "austria-2-liga",
    "SC0": "scotland-premiership",
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


def strict_provider_league_match(
    odds_module: Any,
    original_matcher: Any,
    config_league: Dict[str, Any],
    provider_leagues: Sequence[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    code = str(config_league.get("leagueCode") or "").upper()
    verified_slug = VERIFIED_PROVIDER_SLUGS.get(code)
    if verified_slug:
        return next(
            (
                item for item in provider_leagues
                if str(item.get("slug") or "") == verified_slug
                and odds_module.provider_country_matches(config_league, item)
            ),
            None,
        )
    result = original_matcher(config_league, provider_leagues)
    if result is None:
        return None
    provider_text = odds_module.normalize_text(f"{result.get('name', '')} {result.get('slug', '')}")
    target_text = odds_module.normalize_text(
        " ".join(str(value) for value in config_league.get("searchTerms", []) or [])
    )
    disallowed = {"women", "feminino", "u17", "u19", "u20", "u21", "u23", "youth", "reserve"}
    if any(token in provider_text and token not in target_text for token in disallowed):
        return None
    return result


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


def exact_provider_market_payloads(odds_module: Any, event_odds: Dict[str, Any]) -> List[Dict[str, Any]]:
    payloads: List[Dict[str, Any]] = []
    for bookmaker, markets in odds_module.bookmaker_blocks(event_odds):
        for market in markets:
            payloads.append({
                "bookmaker": bookmaker,
                "providerMarket": odds_module.raw_market_name(market),
                "exactProviderPayload": True,
                "market": market,
            })
    return payloads


def expanded_build_output(
    odds_module: Any,
    config: Dict[str, Any],
    selected: List[Dict[str, Any]],
    api_key: str,
    dry_run: bool,
    bookmakers: str,
    debug: Dict[str, Any],
) -> Dict[str, Any]:
    generated_at = odds_module.now_utc()
    aliases = odds_module.load_aliases()
    debug["generatedAt"] = generated_at
    debug["scriptVersion"] = odds_module.SCRIPT_VERSION
    debug["registry"] = odds_module.registry_summary(config)
    debug["dryRun"] = dry_run
    debug["bookmakersRequested"] = [x.strip() for x in bookmakers.split(",") if x.strip()]
    debug["leaguesRequested"] = [x.get("leagueCode") for x in selected]
    debug["marketAuditPolicy"] = {
        "auditOnlyFamilies": sorted(odds_module.AUDIT_ONLY_FAMILIES),
        "normalMarketsUnchanged": True,
        "extraApiCalls": 0,
        "allProviderMarketsArchivedSeparately": True,
    }
    output = odds_module.empty_output(generated_at, debug)
    archive: Dict[str, Any] = {
        "schemaVersion": 1,
        "source": "odds-api-io",
        "provider": "Odds-API.io",
        "generatedAt": generated_at,
        "dataContract": {
            "purpose": "Store every bookmaker market payload returned by Odds-API.io",
            "bettingInput": False,
            "canonicalBettingMarketsRemainIn": "odds/odds_api_io/domestic_odds.json",
            "estimatedPrices": False,
        },
        "leagues": [],
    }
    if dry_run:
        debug["marketAuditSelfCheck"] = odds_module.run_market_audit_self_check()
        debug.setdefault("warnings", []).append("Dry run: skipped Odds-API.io calls and production output writes.")
        output["debug"] = odds_module.output_debug(generated_at, debug)
        output["providerMarketsArchive"] = archive
        return output
    provider_leagues = odds_module.discover_provider_leagues(api_key, debug)
    if odds_module.should_stop_for_rate_limit(debug):
        debug.setdefault("warnings", []).append("Stopped after league discovery because rateLimitRemaining is below guard.")
        output["debug"] = odds_module.output_debug(generated_at, debug)
        output["providerMarketsArchive"] = archive
        return output
    for league in selected:
        league_code = str(league.get("leagueCode"))
        try:
            provider = odds_module.match_provider_league(league, provider_leagues)
            if not provider:
                debug.setdefault("leaguesMissing", []).append({
                    "leagueCode": league_code,
                    "country": league.get("country"),
                    "competition": league.get("competition"),
                    "apiFootballLeagueId": league.get("apiFootballLeagueId"),
                    "reason": "verified provider league slug not found",
                })
                continue
            slug = str(provider.get("slug") or "")
            debug.setdefault("leaguesMatched", []).append({
                "leagueCode": league_code,
                "country": league.get("country"),
                "competition": league.get("competition"),
                "apiFootballLeagueId": league.get("apiFootballLeagueId"),
                "providerLeagueSlug": slug,
                "providerName": provider.get("name"),
            })
            if odds_module.should_stop_for_rate_limit(debug):
                debug.setdefault("warnings", []).append("Stopped before events fetch because rateLimitRemaining is below guard.")
                break
            events = odds_module.fetch_events_for_league(api_key, slug, int(config.get("horizonDays") or 21), debug)
            event_ids = [odds_module.event_id(event) for event in events if odds_module.event_id(event)]
            odds_by_event = odds_module.fetch_odds(api_key, event_ids, bookmakers, debug) if event_ids else {}
            matches: List[Dict[str, Any]] = []
            archive_matches: List[Dict[str, Any]] = []
            events_without_markets = 0
            events_without_team_mapping = 0
            raw_market_payloads = 0
            for event in events:
                raw_event_odds = odds_by_event.get(odds_module.event_id(event)) or event
                match = odds_module.normalize_event_match(league, event, raw_event_odds, aliases, debug)
                if match is None:
                    continue
                provider_markets = exact_provider_market_payloads(odds_module, raw_event_odds)
                raw_market_payloads += len(provider_markets)
                if provider_markets:
                    archive_matches.append({
                        "id": match.get("id"),
                        "date": match.get("date"),
                        "kickoff": match.get("kickoff"),
                        "providerHomeTeam": match.get("providerHomeTeam"),
                        "providerAwayTeam": match.get("providerAwayTeam"),
                        "homeTeam": match.get("homeTeam"),
                        "awayTeam": match.get("awayTeam"),
                        "teamMappingStatus": match.get("teamMappingStatus"),
                        "providerMarkets": provider_markets,
                    })
                if match.get("markets") and match.get("usableForStats"):
                    matches.append(match)
                elif match.get("markets"):
                    events_without_team_mapping += 1
                else:
                    events_without_markets += 1
            output["leagues"].append({
                "leagueCode": league_code,
                "country": league.get("country"),
                "competition": league.get("competition"),
                "season": league.get("season"),
                "apiFootballLeagueId": league.get("apiFootballLeagueId"),
                "enabledForStats": bool(league.get("enabledForStats", True)),
                "enabledForOdds": bool(league.get("enabledForOdds", True)),
                "enabledForBetting": bool(league.get("enabledForBetting", True)),
                "providerLeagueSlug": slug,
                "providerName": provider.get("name"),
                "matches": matches,
            })
            archive["leagues"].append({
                "leagueCode": league_code,
                "country": league.get("country"),
                "competition": league.get("competition"),
                "season": league.get("season"),
                "providerLeagueSlug": slug,
                "providerName": provider.get("name"),
                "matches": archive_matches,
            })
            debug.setdefault("leagueReports", []).append({
                "leagueCode": league_code,
                "country": league.get("country"),
                "competition": league.get("competition"),
                "apiFootballLeagueId": league.get("apiFootballLeagueId"),
                "providerLeagueSlug": slug,
                "eventsFetched": len(events),
                "eventsWithOddsResponse": len(odds_by_event),
                "eventsWithoutMappedMarkets": events_without_markets,
                "eventsWithMarketsButUnmatchedTeams": events_without_team_mapping,
                "matchesEmitted": len(matches),
                "marketsEmitted": sum(len(match.get("markets", [])) for match in matches),
                "providerMatchesArchived": len(archive_matches),
                "providerMarketPayloadsArchived": raw_market_payloads,
                "matchedTeamPairs": sum(1 for match in matches if match.get("teamMappingStatus") == "matched"),
                "partialTeamPairs": 0,
                "unmatchedTeamPairs": 0,
            })
        except Exception as exc:
            debug.setdefault("warnings", []).append(f"{league_code}: {exc}")
        if odds_module.should_stop_for_rate_limit(debug):
            debug.setdefault("warnings", []).append("Stopped safely because rateLimitRemaining is below guard; partial output written.")
            break
    output["debug"] = odds_module.output_debug(generated_at, debug)
    output["providerMarketsArchive"] = archive
    return output


def install(odds_module: Any, pipeline_module: Any) -> None:
    """Install the shared extension once into Domestic odds ingestion."""
    if getattr(odds_module, "_statmaker_domestic_expansion_installed", False):
        return
    original_normalize_market = odds_module.normalize_market
    original_match_provider_league = odds_module.match_provider_league

    def expanded_normalize_market(
        market: Dict[str, Any],
        bookmaker: str,
        home: str,
        away: str,
        debug: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        raw_name = odds_module.raw_market_name(market)
        if "double chance" in odds_module.normalize_text(raw_name):
            return normalize_double_chance(odds_module, market, bookmaker, home, away, debug)
        return original_normalize_market(market, bookmaker, home, away, debug)

    def expanded_canonical_team_info(
        name: str,
        league_code: str,
        aliases: Dict[str, Dict[str, str]],
        debug: Dict[str, Any],
    ) -> Tuple[str, Optional[str]]:
        return canonical_team_info(odds_module, name, league_code, aliases, debug)

    def expanded_generated_aliases(registry: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, str]]:
        return generated_aliases(odds_module, pipeline_module.stats_fetch, pipeline_module.load_json, registry)

    def expanded_match_provider_league(
        config_league: Dict[str, Any],
        provider_leagues: Sequence[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        return strict_provider_league_match(odds_module, original_match_provider_league, config_league, provider_leagues)

    def installed_build_output(
        config: Dict[str, Any],
        selected: List[Dict[str, Any]],
        api_key: str,
        dry_run: bool,
        bookmakers: str,
        debug: Dict[str, Any],
    ) -> Dict[str, Any]:
        return expanded_build_output(odds_module, config, selected, api_key, dry_run, bookmakers, debug)

    odds_module.SUPPORTED_MARKETS.add("DOUBLE_CHANCE")
    if "DOUBLE_CHANCE" not in odds_module.EMITTED_MARKET_COUNT_KEYS:
        odds_module.EMITTED_MARKET_COUNT_KEYS.append("DOUBLE_CHANCE")
    odds_module.normalize_market = expanded_normalize_market
    odds_module.canonical_team_info = expanded_canonical_team_info
    odds_module.match_provider_league = expanded_match_provider_league
    odds_module.build_output = installed_build_output
    pipeline_module.generated_aliases = expanded_generated_aliases
    odds_module._statmaker_domestic_expansion_installed = True
