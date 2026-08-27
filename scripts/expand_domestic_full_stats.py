#!/usr/bin/env python3
"""Normalize every team-stat field present in the API-Football cache.

This is a zero-API-call cache migration. The upstream fixture-stat fetch keeps
raw provider statistics, so this step can safely add all currently observed
team-stat families without refetching or fabricating values.
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import api_football_fetch_fixture_stats as stats_fetch
import domestic_live_july_pipeline as pipeline

ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = ROOT / "reports" / "domestic_live_july_full_stats_expansion.json"

EXTRA_FIELD_MAP: Dict[str, Tuple[str, str]] = {
    "shots off goal": ("HShotsOffGoal", "AShotsOffGoal"),
    "blocked shots": ("HBlockedShots", "ABlockedShots"),
    "shots insidebox": ("HShotsInsideBox", "AShotsInsideBox"),
    "shots inside box": ("HShotsInsideBox", "AShotsInsideBox"),
    "shots outsidebox": ("HShotsOutsideBox", "AShotsOutsideBox"),
    "shots outside box": ("HShotsOutsideBox", "AShotsOutsideBox"),
    "offsides": ("HOffsides", "AOffsides"),
    "passes %": ("HPassAccuracy", "APassAccuracy"),
    "pass accuracy": ("HPassAccuracy", "APassAccuracy"),
    "goals prevented": ("HGoalsPrevented", "AGoalsPrevented"),
    "goals_prevented": ("HGoalsPrevented", "AGoalsPrevented"),
    "free kicks": ("HFreeKicks", "AFreeKicks"),
}

EXTRA_FIELDS: Tuple[str, ...] = tuple(
    field
    for pair in EXTRA_FIELD_MAP.values()
    for field in pair
)


def normalize_key(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9_%]+", " ", text.lower()).strip()
    return re.sub(r"\s+", " ", text)


def parse_number(value: Any) -> Optional[float | int]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip().removesuffix("%").replace(",", ".")
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return int(number) if number.is_integer() else number


def side_for_block(
    block: Dict[str, Any],
    home_id: Any,
    away_id: Any,
    home_name: str,
    away_name: str,
) -> Optional[str]:
    team = block.get("team") if isinstance(block.get("team"), dict) else {}
    team_id = team.get("id")
    team_name = str(team.get("name") or "").strip().lower()
    if team_id == home_id or (team_name and team_name == home_name):
        return "home"
    if team_id == away_id or (team_name and team_name == away_name):
        return "away"
    return None


def expand_fixture(item: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, int]]:
    result = dict(item)
    normalized = dict(item.get("normalized_stats") or {})
    for field in EXTRA_FIELDS:
        normalized.setdefault(field, None)

    home_id = item.get("home_team_id")
    away_id = item.get("away_team_id")
    home_name = str(item.get("home_team") or "").strip().lower()
    away_name = str(item.get("away_team") or "").strip().lower()
    raw = item.get("raw_statistics") if isinstance(item.get("raw_statistics"), list) else []
    observed: Dict[str, int] = {}

    for block in raw:
        if not isinstance(block, dict):
            continue
        side = side_for_block(block, home_id, away_id, home_name, away_name)
        if side is None:
            continue
        for stat in block.get("statistics") or []:
            if not isinstance(stat, dict):
                continue
            stat_type = normalize_key(stat.get("type"))
            field_pair = EXTRA_FIELD_MAP.get(stat_type)
            if field_pair is None:
                continue
            field = field_pair[0] if side == "home" else field_pair[1]
            value = parse_number(stat.get("value"))
            normalized[field] = value
            observed[field] = observed.get(field, 0) + int(value is not None)

    result["normalized_stats"] = normalized
    return result, observed


def main() -> int:
    registry_payload = pipeline.load_json(pipeline.REGISTRY_PATH, {})
    leagues = registry_payload.get("leagues", []) if isinstance(registry_payload, dict) else []
    if not leagues:
        raise SystemExit("Domestic live/July registry is empty")

    totals = {field: 0 for field in EXTRA_FIELDS}
    reports: List[Dict[str, Any]] = []
    fixtures_scanned = 0
    fixtures_with_raw = 0

    for base_league in leagues:
        for league in pipeline.stats_artifact_variants(base_league):
            cache_path = stats_fetch.cache_path_for(league)
            cache = pipeline.load_json(cache_path, {})
            identity_error = stats_fetch.cache_identity_mismatch_reason(league, cache)
            if identity_error:
                raise SystemExit(
                    f"Refusing full-stats migration for {league.get('leagueCode')} "
                    f"{league.get('app_season')}: {identity_error}"
                )
            fixtures = [item for item in cache.get("fixtures", []) or [] if isinstance(item, dict)]
            expanded: List[Dict[str, Any]] = []
            league_counts = {field: 0 for field in EXTRA_FIELDS}
            for item in fixtures:
                fixtures_scanned += 1
                if item.get("raw_statistics"):
                    fixtures_with_raw += 1
                migrated, counts = expand_fixture(item)
                expanded.append(migrated)
                for field, count in counts.items():
                    league_counts[field] += count
                    totals[field] += count
            cache["fixtures"] = expanded
            cache["full_stats_contract"] = {
                "source": "API-Football raw fixture statistics",
                "extraFields": list(EXTRA_FIELDS),
                "noEstimatedValues": True,
            }
            pipeline.write_json(cache_path, cache)
            reports.append({
                "leagueCode": league.get("leagueCode"),
                "country": league.get("country"),
                "league": league.get("competition"),
                "season": league.get("app_season"),
                "statsRole": league.get("statsRole") or "historical_support",
                "fixtures": len(fixtures),
                "fieldNonNullCounts": league_counts,
            })

    report = {
        "generatedAt": pipeline.now_utc(),
        "apiCalls": 0,
        "leagueCount": len(reports),
        "fixturesScanned": fixtures_scanned,
        "fixturesWithRawStatistics": fixtures_with_raw,
        "extraFields": list(EXTRA_FIELDS),
        "fieldNonNullCounts": totals,
        "leagues": reports,
    }
    pipeline.write_json(REPORT_PATH, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
