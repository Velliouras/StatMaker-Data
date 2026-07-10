#!/usr/bin/env python3
"""Finalize app-facing Domestic artifacts with complete score metadata.

The API-Football cache is the source of truth. This step copies full-time and
half-time scores into every app-facing match so the Android importer can ingest
historical statistics without relying on Football-Data CSV rows.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import api_football_fetch_fixture_stats as stats_fetch
import build_statmaker_domestic_enriched as enriched_build

ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "data" / "statmaker" / "domestic_live_july_registry.json"
REPORT_PATH = ROOT / "reports" / "domestic_live_july_artifact_contract.json"


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fixture_id(value: Any) -> str:
    return str(value or "").strip()


def score_value(item: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in item and item.get(key) is not None:
            return item.get(key)
    return None


def complete_score_contract(match: Dict[str, Any], cache_item: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(match)
    home = score_value(cache_item, "home_goals", "home_score", "fthg")
    away = score_value(cache_item, "away_goals", "away_score", "ftag")
    hthg = score_value(cache_item, "hthg")
    htag = score_value(cache_item, "htag")
    score = cache_item.get("score") if isinstance(cache_item.get("score"), dict) else {}
    halftime = score.get("halftime") if isinstance(score.get("halftime"), dict) else {}
    fulltime = score.get("fulltime") if isinstance(score.get("fulltime"), dict) else {}
    if hthg is None:
        hthg = halftime.get("home")
    if htag is None:
        htag = halftime.get("away")
    if home is None:
        home = fulltime.get("home")
    if away is None:
        away = fulltime.get("away")

    result.update({
        "home_goals": home,
        "away_goals": away,
        "home_score": home,
        "away_score": away,
        "fthg": home,
        "ftag": away,
        "hthg": hthg,
        "htag": htag,
        "goals": {"home": home, "away": away},
        "score": {
            **score,
            "halftime": {"home": hthg, "away": htag},
            "fulltime": {"home": home, "away": away},
        },
    })
    return result


def finalize_league(league: Dict[str, Any]) -> Dict[str, Any]:
    cache_path = stats_fetch.cache_path_for(league)
    artifact_path = enriched_build.output_path_for(league)
    cache = load_json(cache_path, {})
    artifact = load_json(artifact_path, {})
    cached = {
        fixture_id(item.get("fixture_id")): item
        for item in cache.get("fixtures", []) or []
        if isinstance(item, dict) and fixture_id(item.get("fixture_id"))
    }

    completed = []
    missing_cache = 0
    missing_score = 0
    for match in artifact.get("matches", []) or []:
        if not isinstance(match, dict):
            continue
        source = cached.get(fixture_id(match.get("fixture_id")))
        if source is None:
            missing_cache += 1
            completed.append(match)
            continue
        finalized = complete_score_contract(match, source)
        if finalized.get("home_goals") is None or finalized.get("away_goals") is None:
            missing_score += 1
        completed.append(finalized)

    artifact["matches"] = completed
    artifact.setdefault("data_contract", {})["score_contract"] = (
        "full-time and half-time scores copied from API-Football cache"
    )
    write_json(artifact_path, artifact)
    return {
        "leagueCode": league.get("leagueCode"),
        "country": league.get("country"),
        "league": league.get("competition"),
        "artifact": str(artifact_path.relative_to(ROOT)).replace("\\", "/"),
        "matches": len(completed),
        "missingCache": missing_cache,
        "missingScore": missing_score,
    }


def main() -> int:
    registry = load_json(REGISTRY_PATH, {})
    leagues = registry.get("leagues", []) if isinstance(registry, dict) else []
    if not leagues:
        raise SystemExit("Domestic live/July registry is empty")
    reports = [finalize_league(league) for league in leagues if isinstance(league, dict)]
    payload = {
        "leagueCount": len(reports),
        "matches": sum(int(row.get("matches") or 0) for row in reports),
        "missingCache": sum(int(row.get("missingCache") or 0) for row in reports),
        "missingScore": sum(int(row.get("missingScore") or 0) for row in reports),
        "leagues": reports,
    }
    write_json(REPORT_PATH, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
