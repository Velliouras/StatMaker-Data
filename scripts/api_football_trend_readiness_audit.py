#!/usr/bin/env python3
"""Zero-call audit of Domestic cache readiness for Last 2/3/5 trend windows."""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/api_football_enrichment_leagues.json"
CACHE_ROOT = ROOT / "data/api_football/fixture_stats"
REPORT_JSON = ROOT / "reports/api_football_trend_readiness.json"
REPORT_CSV = ROOT / "reports/api_football_trend_readiness.csv"
REPORT_MD = ROOT / "reports/api_football_trend_readiness.md"
WINDOWS = (2, 3, 5)

FIELD_GROUPS = {
    "corners": ("home_corners", "away_corners"),
    "cards": ("home_yellow_cards", "away_yellow_cards", "home_red_cards", "away_red_cards"),
    "shots": ("home_total_shots", "away_total_shots"),
    "shots_on_target": ("home_shots_on_target", "away_shots_on_target"),
    "saves": ("home_goalkeeper_saves", "away_goalkeeper_saves"),
    "possession": ("home_ball_possession", "away_ball_possession"),
    "passes": ("home_total_passes", "away_total_passes"),
    "accurate_passes": ("home_passes_accurate", "away_passes_accurate"),
    "xg": ("home_expected_goals", "away_expected_goals"),
}


def slug(value: Any) -> str:
    text = str(value or "").strip().lower()
    out = []
    for ch in text:
        if ch.isalnum():
            out.append(ch)
        elif not out or out[-1] != "-":
            out.append("-")
    return "".join(out).strip("-")


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def cache_path(league: dict[str, Any]) -> Path:
    return (
        CACHE_ROOT
        / slug(league.get("country"))
        / slug(league.get("display_name"))
        / str(league.get("season"))
        / "fixture_stats.json"
    )


def has_pair(stats: dict[str, Any], fields: tuple[str, ...]) -> bool:
    return any(stats.get(field) is not None for field in fields)


def audit_league(league: dict[str, Any]) -> dict[str, Any]:
    path = cache_path(league)
    cache = load_json(path, {})
    fixtures = cache.get("fixtures") if isinstance(cache, dict) else []
    fixtures = fixtures if isinstance(fixtures, list) else []

    team_matches: dict[str, int] = defaultdict(int)
    field_team_matches: dict[str, dict[str, int]] = {
        field: defaultdict(int) for field in FIELD_GROUPS
    }
    stats_fixtures = 0

    for fixture in fixtures:
        if not isinstance(fixture, dict):
            continue
        home = str(fixture.get("home_team") or "").strip()
        away = str(fixture.get("away_team") or "").strip()
        stats = fixture.get("normalized_stats")
        if not home or not away or not isinstance(stats, dict):
            continue
        stats_fixtures += 1
        team_matches[home] += 1
        team_matches[away] += 1
        for field, keys in FIELD_GROUPS.items():
            if has_pair(stats, keys):
                field_team_matches[field][home] += 1
                field_team_matches[field][away] += 1

    counts = sorted(team_matches.values())
    teams = len(counts)
    min_matches = min(counts) if counts else 0
    med_matches = float(median(counts)) if counts else 0.0
    max_matches = max(counts) if counts else 0

    readiness = {
        f"last_{window}": bool(teams and all(count >= window for count in counts))
        for window in WINDOWS
    }
    teams_ready = {
        f"teams_ready_last_{window}": sum(1 for count in counts if count >= window)
        for window in WINDOWS
    }

    field_readiness: dict[str, Any] = {}
    for field, per_team in field_team_matches.items():
        field_counts = [per_team.get(team, 0) for team in team_matches]
        field_readiness[field] = {
            f"last_{window}": bool(teams and all(count >= window for count in field_counts))
            for window in WINDOWS
        }
        field_readiness[field]["teams_with_any"] = sum(1 for count in field_counts if count > 0)
        field_readiness[field]["min_matches_per_team"] = min(field_counts) if field_counts else 0

    if readiness["last_5"]:
        status = "ready_last_5"
    elif readiness["last_3"]:
        status = "ready_last_3"
    elif readiness["last_2"]:
        status = "ready_last_2"
    elif stats_fixtures:
        status = "partial"
    else:
        status = "no_stats_cache"

    return {
        "country": league.get("country"),
        "league": league.get("display_name"),
        "league_code": league.get("leagueCode"),
        "api_football_league_id": league.get("api_football_league_id"),
        "season": str(league.get("season")),
        "priority_group": league.get("priority_group"),
        "cache_exists": path.exists(),
        "cache_path": str(path.relative_to(ROOT)).replace("\\", "/"),
        "fixtures_in_cache": len(fixtures),
        "fixtures_with_statistics": stats_fixtures,
        "teams": teams,
        "min_stats_matches_per_team": min_matches,
        "median_stats_matches_per_team": med_matches,
        "max_stats_matches_per_team": max_matches,
        **readiness,
        **teams_ready,
        "status": status,
        "field_readiness": field_readiness,
    }


def main() -> int:
    config = load_json(CONFIG, {})
    leagues = [item for item in config.get("leagues", []) if item.get("enabled")]
    rows = [audit_league(league) for league in leagues]
    order = {"partial": 0, "ready_last_2": 1, "ready_last_3": 2, "no_stats_cache": 3, "ready_last_5": 4}
    rows.sort(key=lambda row: (order.get(row["status"], 9), str(row["country"]), str(row["league"])))

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "live_api_calls_made": 0,
        "readiness_definition": "A league is ready for Last N only when every team represented in cached statistics has at least N matches with normalized statistics.",
        "leagues": rows,
    }
    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    fields = [
        "country", "league", "league_code", "api_football_league_id", "season", "priority_group",
        "cache_exists", "fixtures_in_cache", "fixtures_with_statistics", "teams",
        "min_stats_matches_per_team", "median_stats_matches_per_team", "max_stats_matches_per_team",
        "last_2", "last_3", "last_5", "teams_ready_last_2", "teams_ready_last_3",
        "teams_ready_last_5", "status", "cache_path",
    ]
    with REPORT_CSV.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in fields})

    lines = [
        "# API-Football Domestic trend readiness",
        "",
        f"Generated: `{payload['generated_at']}`",
        "",
        "Zero live API calls. Readiness means every represented team has enough cached matches with normalized statistics.",
        "",
        "| Country | League | Season | Stats fixtures | Teams | Min/team | Median/team | Last 2 | Last 3 | Last 5 | Status |",
        "|---|---|---:|---:|---:|---:|---:|:---:|:---:|:---:|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row['country']} | {row['league']} | {row['season']} | {row['fixtures_with_statistics']} | "
            f"{row['teams']} | {row['min_stats_matches_per_team']} | {row['median_stats_matches_per_team']:.1f} | "
            f"{'yes' if row['last_2'] else 'no'} | {'yes' if row['last_3'] else 'no'} | "
            f"{'yes' if row['last_5'] else 'no'} | {row['status']} |"
        )
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"trend readiness written leagues={len(rows)} live_api_calls=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
