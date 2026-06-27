#!/usr/bin/env python3
"""Build app-facing StatMaker domestic enriched JSON files from API-Football caches.

Domestic stats/history are API-Football only. Football-Data CSV files are kept as
inactive archive/reference material and must not be used as runtime fallback.

This script does not call external APIs. It converts cached API-Football fixture
statistics into stable JSON artifacts that the Android app consumes from the
StatMaker-Data repository.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "api_football_enrichment_leagues.json"
CACHE_ROOT = ROOT / "data" / "api_football" / "fixture_stats"
OUTPUT_ROOT = ROOT / "data" / "statmaker" / "domestic_enriched"
REPORT_JSON = ROOT / "reports" / "statmaker_domestic_enriched_build.json"
REPORT_CSV = ROOT / "reports" / "statmaker_domestic_enriched_build.csv"
REPORT_MD = ROOT / "reports" / "statmaker_domestic_enriched_build.md"

STAT_FIELDS = [
    "HS", "AS",
    "HST", "AST",
    "HC", "AC",
    "HF", "AF",
    "HY", "AY",
    "HR", "AR",
    "HPossession", "APossession",
    "HSaves", "ASaves",
    "HPasses", "APasses",
    "HPassesAccurate", "APassesAccurate",
    "HxG", "AxG",
]

FIELD_GROUPS = {
    "shots_total": ("HS", "AS"),
    "shots_on_target": ("HST", "AST"),
    "corners": ("HC", "AC"),
    "fouls": ("HF", "AF"),
    "yellow_cards": ("HY", "AY"),
    "red_cards": ("HR", "AR"),
    "possession": ("HPossession", "APossession"),
    "passes": ("HPasses", "APasses"),
    "xg": ("HxG", "AxG"),
}

BB_REQUIRED_GROUPS = ("corners", "yellow_cards")
BB_SHOT_GROUPS = ("shots_total", "shots_on_target")


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def slug(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text or "unknown"


def league_code(league: Dict[str, Any]) -> str:
    return str(league.get("leagueCode") or league.get("football_data_code") or "UNKNOWN").strip()


def app_season(league: Dict[str, Any]) -> str:
    return str(league.get("app_season") or league.get("season") or "unknown")


def api_season(league: Dict[str, Any]) -> str:
    return str(league.get("season") or "unknown")


def file_stem_for(league: Dict[str, Any]) -> str:
    # Keep the API season in the filename so existing cache/artifact naming stays stable.
    # App-facing season is written inside the artifact as app_season/competition.season.
    return "_".join([
        slug(league.get("country")).replace("-", "_"),
        slug(league.get("display_name")).replace("-", "_"),
        api_season(league).replace("-", "_"),
    ])


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def cache_path_for(league: Dict[str, Any]) -> Path:
    return (
        CACHE_ROOT
        / slug(league.get("country"))
        / slug(league.get("display_name"))
        / api_season(league)
        / "fixture_stats.json"
    )


def output_path_for(league: Dict[str, Any]) -> Path:
    return OUTPUT_ROOT / f"{file_stem_for(league)}.json"


def parse_csv_filter(value: Optional[str]) -> Optional[set[str]]:
    if value is None:
        return None
    parts = [item.strip() for item in value.split(",") if item.strip()]
    if not parts:
        return None
    return {part.lower() for part in parts}


def selected_leagues(config: Dict[str, Any], countries: Optional[set[str]], league_ids: Optional[set[str]]) -> List[Dict[str, Any]]:
    leagues = [league for league in config.get("leagues", []) if bool(league.get("enabled"))]

    if countries:
        leagues = [league for league in leagues if str(league.get("country") or "").lower() in countries]

    if league_ids:
        leagues = [league for league in leagues if str(league.get("api_football_league_id") or "") in league_ids]

    return leagues


def normalize_stats(stats: Dict[str, Any]) -> Dict[str, Any]:
    return {field: stats.get(field) for field in STAT_FIELDS}


def has_any_stats(stats: Dict[str, Any]) -> bool:
    return any(stats.get(field) is not None for field in STAT_FIELDS)


def group_present(stats: Dict[str, Any], group: str) -> bool:
    fields = FIELD_GROUPS[group]
    return all(stats.get(field) is not None for field in fields)


def fixture_quality(stats: Dict[str, Any]) -> Dict[str, Any]:
    group_presence = {group: group_present(stats, group) for group in FIELD_GROUPS}
    has_shots = any(group_presence[group] for group in BB_SHOT_GROUPS)
    has_bb_core = all(group_presence[group] for group in BB_REQUIRED_GROUPS) and has_shots

    return {
        "has_any_stats": has_any_stats(stats),
        "has_bb_core_stats": has_bb_core,
        "groups": group_presence,
    }


def count_ratio(count: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return round(count / total, 4)


def build_readiness(matches: List[Dict[str, Any]], min_fixtures: int, min_coverage: float) -> Dict[str, Any]:
    total = len(matches)
    with_any = sum(1 for match in matches if match["quality"]["has_any_stats"])
    with_bb_core = sum(1 for match in matches if match["quality"]["has_bb_core_stats"])

    group_counts: Dict[str, int] = {}
    for group in FIELD_GROUPS:
        group_counts[group] = sum(1 for match in matches if match["quality"]["groups"].get(group))

    group_coverage = {group: count_ratio(count, total) for group, count in group_counts.items()}
    any_stats_coverage = count_ratio(with_any, total)
    bb_core_coverage = count_ratio(with_bb_core, total)

    has_required_groups = all(group_coverage.get(group, 0.0) >= min_coverage for group in BB_REQUIRED_GROUPS)
    has_shot_group = any(group_coverage.get(group, 0.0) >= min_coverage for group in BB_SHOT_GROUPS)

    bb_ready = bool(
        total >= min_fixtures
        and any_stats_coverage >= min_coverage
        and bb_core_coverage >= min_coverage
        and has_required_groups
        and has_shot_group
    )

    notes: List[str] = []
    if total < min_fixtures:
        notes.append(f"not enough completed fixtures: {total} < {min_fixtures}")
    if any_stats_coverage < min_coverage:
        notes.append(f"low any-stats coverage: {any_stats_coverage} < {min_coverage}")
    if bb_core_coverage < min_coverage:
        notes.append(f"low BB-core coverage: {bb_core_coverage} < {min_coverage}")
    if not has_required_groups:
        notes.append("required BB groups below threshold")
    if not has_shot_group:
        notes.append("no shot group reaches threshold")

    return {
        "min_fixtures": min_fixtures,
        "min_coverage": min_coverage,
        "completed_fixtures": total,
        "fixtures_with_any_stats": with_any,
        "fixtures_with_bb_core_stats": with_bb_core,
        "any_stats_coverage": any_stats_coverage,
        "bb_core_coverage": bb_core_coverage,
        "group_counts": group_counts,
        "group_coverage": group_coverage,
        "bb_ready_candidate": bb_ready,
        "notes": notes or ["ok"],
    }


def build_match(cache_item: Dict[str, Any], league: Dict[str, Any]) -> Dict[str, Any]:
    stats = normalize_stats(cache_item.get("normalized_stats") or {})
    source_league = cache_item.get("source_league") or {}

    return {
        "fixture_id": cache_item.get("fixture_id"),
        "date_utc": cache_item.get("date"),
        "country": league.get("country"),
        "league": league.get("display_name"),
        "league_code": league_code(league),
        "season": app_season(league),
        "app_season": app_season(league),
        "api_football_season": api_season(league),
        "api_football_league_id": league.get("api_football_league_id"),
        "round": source_league.get("round"),
        "home_team": cache_item.get("home_team"),
        "away_team": cache_item.get("away_team"),
        "status": cache_item.get("status"),
        "normalized_stats": stats,
        "quality": fixture_quality(stats),
    }


def build_league_artifact(league: Dict[str, Any], min_fixtures: int, min_coverage: float) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    cache_path = cache_path_for(league)
    cache = load_json(cache_path, {})
    cache_fixtures = cache.get("fixtures") if isinstance(cache, dict) else []
    cache_fixtures = cache_fixtures if isinstance(cache_fixtures, list) else []

    matches = [build_match(item, league) for item in cache_fixtures]
    matches = sorted(matches, key=lambda item: (str(item.get("date_utc") or ""), int(item.get("fixture_id") or 0)))

    readiness = build_readiness(matches, min_fixtures=min_fixtures, min_coverage=min_coverage)
    output_path = output_path_for(league)

    artifact = {
        "schema_version": 3,
        "generated_at": now_utc(),
        "data_contract": {
            "consumer": "StatMaker Android app",
            "active_source": "API-Football domestic history and fixture statistics. Football-Data CSV is inactive archive only.",
            "rule": "The app reads repository JSON artifacts and must not call API-Football directly.",
            "empty_state": "If readiness or market validation fails, show only: Δεν βρέθηκαν αγορές",
        },
        "source": {
            "provider": "api-football",
            "cache_path": str(cache_path.relative_to(ROOT)).replace("\\", "/"),
            "cache_generated_at": cache.get("generated_at") if isinstance(cache, dict) else None,
            "api_football_season": api_season(league),
            "csv_import": "inactive_archive_only",
        },
        "competition": {
            "league_code": league_code(league),
            "country": league.get("country"),
            "league": league.get("display_name"),
            "football_data_code": league.get("football_data_code"),
            "api_football_league_id": league.get("api_football_league_id"),
            "api_football_season": api_season(league),
            "season": app_season(league),
            "app_season": app_season(league),
            "priority_group": league.get("priority_group"),
            "data_level": "fixture_statistics_enriched",
        },
        "readiness": readiness,
        "matches": matches,
    }

    report_row = {
        "league_code": league_code(league),
        "country": league.get("country"),
        "league": league.get("display_name"),
        "app_season": app_season(league),
        "api_football_season": api_season(league),
        "api_football_league_id": league.get("api_football_league_id"),
        "priority_group": league.get("priority_group"),
        "output_path": str(output_path.relative_to(ROOT)).replace("\\", "/"),
        "cache_path": str(cache_path.relative_to(ROOT)).replace("\\", "/"),
        "completed_fixtures": readiness["completed_fixtures"],
        "fixtures_with_any_stats": readiness["fixtures_with_any_stats"],
        "fixtures_with_bb_core_stats": readiness["fixtures_with_bb_core_stats"],
        "any_stats_coverage": readiness["any_stats_coverage"],
        "bb_core_coverage": readiness["bb_core_coverage"],
        "bb_ready_candidate": readiness["bb_ready_candidate"],
        "notes": "; ".join(readiness["notes"]),
    }

    return artifact, report_row


def write_index(rows: List[Dict[str, Any]]) -> None:
    payload = {
        "schema_version": 3,
        "generated_at": now_utc(),
        "artifact_type": "statmaker_domestic_enriched_index",
        "active_source": "api-football",
        "csv_import": "inactive_archive_only",
        "league_count": len(rows),
        "leagues": rows,
    }
    write_json(OUTPUT_ROOT / "index.json", payload)


def write_reports(rows: List[Dict[str, Any]]) -> None:
    payload = {
        "generated_at": now_utc(),
        "artifact_type": "statmaker_domestic_enriched_build_report",
        "active_source": "api-football",
        "csv_import": "inactive_archive_only",
        "league_count": len(rows),
        "leagues": rows,
    }
    write_json(REPORT_JSON, payload)

    fieldnames = [
        "league_code",
        "country",
        "league",
        "app_season",
        "api_football_season",
        "api_football_league_id",
        "priority_group",
        "output_path",
        "cache_path",
        "completed_fixtures",
        "fixtures_with_any_stats",
        "fixtures_with_bb_core_stats",
        "any_stats_coverage",
        "bb_core_coverage",
        "bb_ready_candidate",
        "notes",
    ]

    REPORT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with REPORT_CSV.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in fieldnames})

    lines = [
        "# StatMaker domestic enriched build",
        "",
        f"Generated at: `{payload['generated_at']}`",
        "",
        "Active source: `API-Football`. Football-Data CSV: `inactive archive only`.",
        "",
        "| Code | Country | League | App season | API season | API league ID | Group | Completed | Any stats | BB-core stats | Any coverage | BB-core coverage | BB-ready candidate | Output | Notes |",
        "| --- | --- | --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |",
    ]

    for row in rows:
        values = [
            row.get("league_code"),
            row.get("country"),
            row.get("league"),
            row.get("app_season"),
            row.get("api_football_season"),
            row.get("api_football_league_id"),
            row.get("priority_group"),
            row.get("completed_fixtures"),
            row.get("fixtures_with_any_stats"),
            row.get("fixtures_with_bb_core_stats"),
            row.get("any_stats_coverage"),
            row.get("bb_core_coverage"),
            row.get("bb_ready_candidate"),
            row.get("output_path"),
            row.get("notes"),
        ]
        lines.append("| " + " | ".join(str(value if value is not None else "").replace("|", "\\|") for value in values) + " |")

    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build StatMaker domestic enriched JSON files from API-Football caches")
    parser.add_argument(
        "--countries",
        default="",
        help="Comma-separated countries to build. Empty means all enabled API-Football registry leagues.",
    )
    parser.add_argument(
        "--league-ids",
        default="",
        help="Optional comma-separated API-Football league ids to build.",
    )
    parser.add_argument("--min-fixtures", type=int, default=15, help="Minimum completed fixtures for BB readiness")
    parser.add_argument("--min-coverage", type=float, default=0.65, help="Minimum coverage ratio for BB readiness")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    countries = parse_csv_filter(args.countries)
    league_ids = parse_csv_filter(args.league_ids)
    config = load_json(CONFIG_PATH, {})
    leagues = selected_leagues(config, countries=countries, league_ids=league_ids)

    if not leagues:
        raise SystemExit("No matching enabled leagues found in API-Football domestic registry.")

    rows: List[Dict[str, Any]] = []

    for league in leagues:
        artifact, row = build_league_artifact(
            league,
            min_fixtures=args.min_fixtures,
            min_coverage=args.min_coverage,
        )
        output_path = output_path_for(league)
        write_json(output_path, artifact)
        rows.append(row)
        print(
            f"built {row['league_code']} {row['output_path']} fixtures={row['completed_fixtures']} "
            f"bb_ready={row['bb_ready_candidate']} bb_core={row['bb_core_coverage']}"
        )

    rows = sorted(rows, key=lambda row: (str(row.get("country")), str(row.get("league")), str(row.get("league_code"))))
    write_index(rows)
    write_reports(rows)

    print(f"StatMaker domestic enriched build written leagues={len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
