#!/usr/bin/env python3
"""Build the app-wide normalized Domestic statistics export for live/July leagues."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List

import api_football_fetch_fixture_stats as stats_fetch

ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "data" / "statmaker" / "domestic_live_july_registry.json"
OUTPUT_PATH = ROOT / "data" / "api_football" / "domestic_normalized_fixture_stats.json"
REPORT_PATH = ROOT / "reports" / "domestic_live_july_normalized_stats.json"
TRANSITION_PATH = ROOT / ".git" / "statmaker_normalized_stats_transition.json"

FIELDS = (
    "HS", "AS", "HST", "AST", "HC", "AC", "HF", "AF", "HY", "AY", "HR", "AR",
    "HPossession", "APossession", "HSaves", "ASaves", "HPasses", "APasses",
    "HPassesAccurate", "APassesAccurate", "HxG", "AxG",
    "HShotsOffGoal", "AShotsOffGoal", "HBlockedShots", "ABlockedShots",
    "HShotsInsideBox", "AShotsInsideBox", "HShotsOutsideBox", "AShotsOutsideBox",
    "HOffsides", "AOffsides", "HPassAccuracy", "APassAccuracy",
    "HGoalsPrevented", "AGoalsPrevented", "HFreeKicks", "AFreeKicks",
)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_stats(item: Dict[str, Any]) -> Dict[str, Any]:
    source = item.get("normalized_stats") if isinstance(item.get("normalized_stats"), dict) else {}
    return {field: source.get(field) for field in FIELDS}


def normalize_code(value: Any) -> str:
    code = str(value or "").strip().upper()
    return "ROU" if code == "ROM" else code


def export_fixture(league: Dict[str, Any], item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "fixture_id": item.get("fixture_id"),
        "league_id": league.get("api_football_league_id"),
        "league_code": normalize_code(league.get("leagueCode")),
        "country": league.get("country"),
        "league": league.get("competition"),
        "season": league.get("app_season"),
        "api_football_season": league.get("historyApiSeason"),
        "date": str(item.get("date") or "")[:10],
        "home_team": item.get("home_team"),
        "away_team": item.get("away_team"),
        "stats": normalize_stats(item),
    }


def fixture_identity(item: Dict[str, Any]) -> str:
    league_id = str(item.get("league_id") or "").strip()
    fixture_id = str(item.get("fixture_id") or "").strip()
    if league_id and fixture_id:
        return f"api:{league_id}:{fixture_id}"
    return "fallback:" + "|".join(
        str(item.get(key) or "").strip().lower()
        for key in ("league_code", "date", "home_team", "away_team")
    )


def build_transition(previous: Dict[str, Any], current: Dict[str, Any]) -> Dict[str, Any]:
    old_rows = {
        fixture_identity(item): item
        for item in previous.get("fixtures", []) or []
        if isinstance(item, dict)
    }
    new_rows = {
        fixture_identity(item): item
        for item in current.get("fixtures", []) or []
        if isinstance(item, dict)
    }
    changed = sorted(
        key for key in old_rows.keys() | new_rows.keys()
        if old_rows.get(key) != new_rows.get(key)
    )
    return {
        "removedFixtures": [old_rows[key] for key in changed if key in old_rows],
        "upserts": [new_rows[key] for key in changed if key in new_rows],
    }


def write_transition(previous_payload: Dict[str, Any], previous_sha: str, current_payload: Dict[str, Any], current_sha: str) -> None:
    if not previous_sha or previous_sha == current_sha or not previous_payload.get("fixtures"):
        TRANSITION_PATH.unlink(missing_ok=True)
        return
    delta = build_transition(previous_payload, current_payload)
    transition = {
        "schemaVersion": 1,
        "fromSha256": previous_sha,
        "toSha256": current_sha,
        "removedFixtures": delta["removedFixtures"],
        "upserts": delta["upserts"],
    }
    write_json(TRANSITION_PATH, transition)


def main() -> int:
    registry = load_json(REGISTRY_PATH, {})
    leagues = registry.get("leagues", []) if isinstance(registry, dict) else []
    if not leagues:
        raise SystemExit("Domestic live/July registry is empty")

    previous_payload = load_json(OUTPUT_PATH, {})
    previous_sha = sha256_file(OUTPUT_PATH) if OUTPUT_PATH.is_file() else ""

    fixtures: List[Dict[str, Any]] = []
    league_reports: List[Dict[str, Any]] = []
    for league in leagues:
        if not isinstance(league, dict):
            continue
        cache_path = stats_fetch.cache_path_for(league)
        cache = load_json(cache_path, {})
        rows = [item for item in cache.get("fixtures", []) or [] if isinstance(item, dict)]
        exported = [export_fixture(league, item) for item in rows]
        fixtures.extend(exported)
        league_reports.append({
            "leagueCode": normalize_code(league.get("leagueCode")),
            "country": league.get("country"),
            "league": league.get("competition"),
            "season": league.get("app_season"),
            "cachePath": str(cache_path.relative_to(ROOT)).replace("\\", "/"),
            "fixtures": len(exported),
            "fixturesWithAnyStats": sum(
                1 for fixture in exported if any(value is not None for value in fixture["stats"].values())
            ),
        })

    fixtures.sort(key=lambda item: (
        str(item.get("date") or ""),
        str(item.get("league_code") or ""),
        str(item.get("fixture_id") or ""),
    ))
    output = {
        "schemaVersion": 3,
        "provider": "api-football",
        "sourceContract": "all available team statistics for active and July-starting Domestic leagues",
        "leagueCount": len(league_reports),
        "fixtureCount": len(fixtures),
        "fields": list(FIELDS),
        "fixtures": fixtures,
    }
    report = {
        "leagueCount": len(league_reports),
        "fixtureCount": len(fixtures),
        "fixturesWithAnyStats": sum(
            1 for fixture in fixtures if any(value is not None for value in fixture["stats"].values())
        ),
        "fieldNonNullCounts": {
            field: sum(1 for fixture in fixtures if fixture["stats"].get(field) is not None)
            for field in FIELDS
        },
        "leagues": league_reports,
    }
    write_json(OUTPUT_PATH, output)
    current_sha = sha256_file(OUTPUT_PATH)
    write_transition(previous_payload, previous_sha, output, current_sha)
    write_json(REPORT_PATH, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
