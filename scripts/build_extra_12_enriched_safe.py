#!/usr/bin/env python3
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict, List

import build_statmaker_domestic_enriched as build

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "api_football_enrichment_leagues.json"
INDEX = ROOT / "data" / "statmaker" / "domestic_enriched" / "index.json"
REPORT = ROOT / "reports" / "domestic_31_extra_enriched_safe.json"
EXTRA_CODES = {"SRB","BGR","SVN","HUN","CZE","SVK","ISL","LVA","LTU","EST","FIN2","NOR2"}
BASE_19 = {"ARG","BRA","IRL","USA","CHN","NOR","BRA2","SWE2","FIN","SWE","MEX","ROM","DNK","POL","RUS","SWZ","AUT2","AUT","SC0"}
SCORE_KEYS = (
    "home_goals", "away_goals", "home_score", "away_score",
    "fthg", "ftag", "hthg", "htag", "goals", "score",
)


def load(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def add_scores_from_cache(artifact: Dict[str, Any], league: Dict[str, Any]) -> Dict[str, int]:
    """Copy score fields already present in the API-Football cache into app artifacts.

    DomesticEnrichedRepository requires a full-time score before importing a match.
    This changes data serialization only; no app/betting-engine code is touched.
    """
    cache = load(build.cache_path_for(league), {})
    cache_by_fixture = {
        str(item.get("fixture_id") or ""): item
        for item in (cache.get("fixtures", []) or [])
        if str(item.get("fixture_id") or "")
    }
    with_full_time = 0
    without_full_time = 0
    for match in artifact.get("matches", []) or []:
        cached = cache_by_fixture.get(str(match.get("fixture_id") or ""), {})
        for key in SCORE_KEYS:
            if key in cached:
                match[key] = copy.deepcopy(cached.get(key))
        home = match.get("home_goals")
        away = match.get("away_goals")
        if home is None or away is None:
            home = match.get("fthg")
            away = match.get("ftag")
        if home is None or away is None:
            score = match.get("score") if isinstance(match.get("score"), dict) else {}
            fulltime = score.get("fulltime") if isinstance(score.get("fulltime"), dict) else {}
            home = fulltime.get("home")
            away = fulltime.get("away")
        if home is None or away is None:
            without_full_time += 1
        else:
            with_full_time += 1
    return {"withFullTimeScore": with_full_time, "withoutFullTimeScore": without_full_time}


def main() -> int:
    cfg = load(CONFIG, {})
    selected = [x for x in cfg.get("leagues", []) or [] if str(x.get("leagueCode") or "") in EXTRA_CODES]
    if {str(x.get("leagueCode") or "") for x in selected} != EXTRA_CODES:
        raise SystemExit("Extra 12 enrichment config is incomplete")

    original_index = load(INDEX, {})
    original_rows = list(original_index.get("leagues", []) or [])
    original_by_code = {str(x.get("league_code") or ""): copy.deepcopy(x) for x in original_rows}
    missing_base = sorted(BASE_19 - set(original_by_code))
    if missing_base:
        raise SystemExit(f"Refusing to modify index: missing existing base leagues {missing_base}")

    new_rows: List[Dict[str, Any]] = []
    score_validation: Dict[str, Dict[str, int]] = {}
    for league in selected:
        code = str(league.get("leagueCode") or "")
        artifact, row = build.build_league_artifact(league, min_fixtures=15, min_coverage=0.65)
        score_counts = add_scores_from_cache(artifact, league)
        # Every completed historical fixture from API-Football should carry its score.
        # Refuse to publish a destructive artifact that would cause the Android importer
        # to delete an existing league and then skip scoreless rows.
        if score_counts["withoutFullTimeScore"]:
            raise SystemExit(
                f"Refusing scoreless enriched artifact for {code}: "
                f"{score_counts['withoutFullTimeScore']} matches missing full-time score"
            )
        output_path = build.output_path_for(league)
        build.write_json(output_path, artifact)
        score_validation[code] = score_counts
        new_rows.append(row)

    merged_by_code = {str(x.get("league_code") or ""): x for x in original_rows}
    for row in new_rows:
        merged_by_code[str(row.get("league_code") or "")] = row

    for code in BASE_19:
        if merged_by_code.get(code) != original_by_code.get(code):
            raise SystemExit(f"Guard failed: existing enriched index row changed: {code}")

    merged_rows = sorted(merged_by_code.values(), key=lambda r: (str(r.get("country")), str(r.get("league")), str(r.get("league_code"))))
    if len(merged_rows) != 31:
        raise SystemExit(f"Expected exactly 31 enriched index rows, got {len(merged_rows)}")

    output_index = dict(original_index)
    output_index["generated_at"] = build.now_utc()
    output_index["league_count"] = len(merged_rows)
    output_index["leagues"] = merged_rows
    save(INDEX, output_index)
    save(REPORT, {
        "mode": "extra-12-only-safe",
        "base19Preserved": sorted(BASE_19),
        "scoreContractValidated": True,
        "scoreValidation": score_validation,
        "extraRows": new_rows,
        "finalIndexLeagueCount": len(merged_rows),
    })
    print(json.dumps({
        "finalIndexLeagueCount": len(merged_rows),
        "scoreValidation": score_validation,
        "extraRows": new_rows,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
