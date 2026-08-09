#!/usr/bin/env python3
"""Build market-specific Domestic proposal-readiness from cached Stats + exact Odds.

No API calls are made. The artifact answers only whether a fixture/market has the
technical data needed for the Android betting engine to evaluate a proposal.
Actual proposal visibility still requires a pattern match in the app.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, Tuple

import update_domestic_odds_api_io as odds_util

ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = ROOT / "data" / "statmaker" / "domestic_enriched" / "index.json"
ODDS_PATH = ROOT / "odds" / "odds_api_io" / "domestic_odds.json"
SCOPE_PATH = ROOT / "config" / "statmaker_final_domestic_scope.json"
OUT_PATH = ROOT / "data" / "statmaker" / "domestic_proposal_readiness.json"
REPORT_PATH = ROOT / "reports" / "domestic_proposal_readiness.json"

MIN_VISIBLE_ODD = 1.20
QUALITY_SAMPLE = 10

# market -> (historical requirement, scope)
MARKET_REQUIREMENTS: Dict[str, Tuple[str, str]] = {
    "1X2": ("scores", "match"),
    "MATCH_GOALS": ("scores", "match"),
    "FIRST_HALF_GOALS": ("half_scores", "match"),
    "BTTS": ("scores", "match"),
    "TEAM_TOTAL_GOALS": ("scores", "team"),
    "MATCH_CORNERS": ("corners", "match"),
    "TEAM_CORNERS": ("corners", "team"),
    "MATCH_CARDS": ("yellow_cards", "match"),
    "TEAM_CARDS": ("yellow_cards", "team"),
    "MATCH_SHOTS": ("shots_total", "match"),
    "TEAM_SHOTS": ("shots_total", "team"),
    "MATCH_SHOTS_ON_TARGET": ("shots_on_target", "match"),
    "TEAM_SHOTS_ON_TARGET": ("shots_on_target", "team"),
    "DOUBLE_CHANCE": ("scores", "match"),
    # Asian families are separate app/filter markets, but their readiness comes
    # from the same exact historical observations used by their conventional
    # counterparts. Quarter-line settlement is handled by the Android engine.
    "ASIAN_HANDICAP": ("scores", "match"),
    "ASIAN_HANDICAP_1H": ("half_scores", "match"),
    "ASIAN_GOALS": ("scores", "match"),
    "ASIAN_GOALS_1H": ("half_scores", "match"),
    "ASIAN_CORNERS": ("corners", "match"),
    "ASIAN_CORNER_HANDICAP": ("corners", "match"),
}

STAT_FIELDS = {
    "corners": ("HC", "AC"),
    "yellow_cards": ("HY", "AY"),
    "shots_total": ("HS", "AS"),
    "shots_on_target": ("HST", "AST"),
}


def read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def normalize_code(value: Any) -> str:
    code = str(value or "").strip().upper()
    return "ROU" if code == "ROM" else code


def normalize_team(value: Any) -> str:
    return odds_util.normalize_text(value, drop_suffixes=True)


def valid_exact_market(market: Dict[str, Any]) -> bool:
    if market.get("exactBookmakerOdds") is not True:
        return False
    if not str(market.get("bookmaker") or "").strip():
        return False
    try:
        price = float(market.get("odds"))
    except (TypeError, ValueError):
        return False
    return price >= MIN_VISIBLE_ODD and str(market.get("market") or "") in MARKET_REQUIREMENTS


def _increment_pair(
    support: Dict[str, Dict[str, Dict[str, int]]],
    home: str,
    away: str,
    requirement: str,
    home_value: Any,
    away_value: Any,
) -> None:
    both = home_value is not None and away_value is not None
    if home and home_value is not None:
        support[home][requirement]["team"] += 1
    if away and away_value is not None:
        support[away][requirement]["team"] += 1
    if both:
        if home:
            support[home][requirement]["match"] += 1
        if away:
            support[away][requirement]["match"] += 1


def historical_support(matches: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Dict[str, int]]]:
    support: Dict[str, Dict[str, Dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: {"team": 0, "match": 0})
    )
    for match in matches:
        if not isinstance(match, dict):
            continue
        home = normalize_team(match.get("home_team"))
        away = normalize_team(match.get("away_team"))

        home_goals = match.get("home_goals")
        away_goals = match.get("away_goals")
        _increment_pair(support, home, away, "scores", home_goals, away_goals)

        _increment_pair(support, home, away, "half_scores", match.get("hthg"), match.get("htag"))

        stats = match.get("normalized_stats") if isinstance(match.get("normalized_stats"), dict) else {}
        for requirement, (home_field, away_field) in STAT_FIELDS.items():
            _increment_pair(
                support,
                home,
                away,
                requirement,
                stats.get(home_field),
                stats.get(away_field),
            )
    return support


def market_support(
    support: Dict[str, Dict[str, Dict[str, int]]],
    home_team: str,
    away_team: str,
    market_id: str,
) -> Dict[str, Any]:
    requirement, scope = MARKET_REQUIREMENTS[market_id]
    home = normalize_team(home_team)
    away = normalize_team(away_team)
    home_sample = int(support.get(home, {}).get(requirement, {}).get(scope, 0))
    away_sample = int(support.get(away, {}).get(requirement, {}).get(scope, 0))
    best_sample = max(home_sample, away_sample)
    return {
        "historicalRequirement": requirement,
        "scope": scope,
        "homeSample": home_sample,
        "awaySample": away_sample,
        "bestTeamSample": best_sample,
        "hardHistoryValid": best_sample >= 1,
        "qualitySampleReady": best_sample >= QUALITY_SAMPLE,
    }


def build_payload(
    index_payload: Dict[str, Any],
    odds_payload: Dict[str, Any],
    scope_payload: Dict[str, Any],
) -> Dict[str, Any]:
    stats_universe = {normalize_code(code) for code in scope_payload.get("statsUniverseLeagueCodes", []) or []}
    index_rows = {
        normalize_code(row.get("league_code")): row
        for row in index_payload.get("leagues", []) or []
        if isinstance(row, dict)
    }

    league_results = []
    total_odds_fixtures = 0
    total_ready_fixtures = 0
    ready_market_counts: Dict[str, int] = defaultdict(int)

    for odds_league in odds_payload.get("leagues", []) or []:
        if not isinstance(odds_league, dict):
            continue
        code = normalize_code(odds_league.get("leagueCode"))
        if stats_universe and code not in stats_universe:
            continue

        index_row = index_rows.get(code)
        stats_artifact: Dict[str, Any] = {}
        if index_row:
            output_path = ROOT / str(index_row.get("output_path") or "")
            stats_artifact = read_json(output_path, {})
        history_matches = stats_artifact.get("matches", []) if isinstance(stats_artifact, dict) else []
        support = historical_support(history_matches or [])

        fixture_rows = []
        league_ready_markets = set()
        for match in odds_league.get("matches", []) or []:
            if not isinstance(match, dict):
                continue
            total_odds_fixtures += 1
            home = str(match.get("canonicalHomeTeam") or match.get("homeTeam") or "").strip()
            away = str(match.get("canonicalAwayTeam") or match.get("awayTeam") or "").strip()
            exact_by_market: Dict[str, int] = defaultdict(int)
            for market in match.get("markets", []) or []:
                if isinstance(market, dict) and valid_exact_market(market):
                    exact_by_market[str(market.get("market"))] += 1

            readiness = {}
            ready_markets = []
            for market_id in sorted(exact_by_market):
                support_row = market_support(support, home, away, market_id)
                data_ready = support_row["hardHistoryValid"]
                readiness[market_id] = {
                    **support_row,
                    "exactSelectionCount": exact_by_market[market_id],
                    "proposalReadyForEvaluation": data_ready,
                }
                if data_ready:
                    ready_markets.append(market_id)
                    league_ready_markets.add(market_id)
                    ready_market_counts[market_id] += 1

            fixture_ready = bool(ready_markets)
            if fixture_ready:
                total_ready_fixtures += 1
            fixture_rows.append({
                "id": match.get("id") or match.get("matchId"),
                "date": match.get("date") or str(match.get("kickoff") or "")[:10],
                "kickoff": match.get("kickoff"),
                "homeTeam": home,
                "awayTeam": away,
                "exactMarketIds": sorted(exact_by_market),
                "readyMarketIds": ready_markets,
                "proposalReadyForEvaluation": fixture_ready,
                "marketReadiness": readiness,
            })

        league_results.append({
            "leagueCode": code,
            "country": odds_league.get("country") or (index_row or {}).get("country"),
            "competition": odds_league.get("competition") or (index_row or {}).get("league"),
            "statsArtifactPresent": bool(stats_artifact),
            "historicalMatchCount": len(history_matches or []),
            "oddsFixtureCount": len(fixture_rows),
            "proposalReadyFixtureCount": sum(1 for row in fixture_rows if row["proposalReadyForEvaluation"]),
            "readyMarketIds": sorted(league_ready_markets),
            "fixtures": fixture_rows,
        })

    active_stats_codes = set(index_rows)
    odds_codes = {normalize_code(row.get("leagueCode")) for row in odds_payload.get("leagues", []) or [] if isinstance(row, dict)}
    return {
        "schemaVersion": 1,
        "generatedAt": odds_util.now_utc(),
        "contract": {
            "statsUniverseLeagueCount": len(stats_universe),
            "hardVisibilityHistorySample": 1,
            "qualitySampleBenchmark": QUALITY_SAMPLE,
            "minimumVisibleOdd": MIN_VISIBLE_ODD,
            "actualProposalStillRequiresPatternMatch": True,
            "marketSpecificReadiness": True,
        },
        "summary": {
            "activeStatsLeagueCount": len(active_stats_codes),
            "oddsArtifactLeagueCount": len(odds_codes & stats_universe) if stats_universe else len(odds_codes),
            "oddsFixtureCount": total_odds_fixtures,
            "proposalReadyFixtureCount": total_ready_fixtures,
            "readyMarketFixtureCounts": dict(sorted(ready_market_counts.items())),
        },
        "leagues": sorted(league_results, key=lambda row: (str(row.get("country") or ""), str(row.get("competition") or ""))),
    }


def main() -> int:
    index_payload = read_json(INDEX_PATH, {})
    odds_payload = read_json(ODDS_PATH, {})
    scope_payload = read_json(SCOPE_PATH, {})
    payload = build_payload(index_payload, odds_payload, scope_payload)
    write_json(OUT_PATH, payload)
    write_json(REPORT_PATH, payload)
    print(json.dumps(payload.get("summary", {}), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
