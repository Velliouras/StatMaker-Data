#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

import api_football_fetch_fixture_stats as stats

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "api_football_enrichment_leagues.json"
REPORT = ROOT / "reports" / "domestic_31_extra_stats_safe.json"
EXTRA_CODES = ["SRB","BGR","SVN","HUN","CZE","SVK","ISL","LVA","LTU","EST","FIN2","NOR2"]
PER_LEAGUE_REQUEST_CAP = 8


def load(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    api_key = os.getenv("API_FOOTBALL_KEY", "").strip()
    if not api_key:
        raise SystemExit("API_FOOTBALL_KEY is required")

    cfg = load(CONFIG)
    by_code = {str(x.get("leagueCode") or "").upper(): x for x in cfg.get("leagues", []) or []}
    missing = [c for c in EXTRA_CODES if c not in by_code]
    if missing:
        raise SystemExit(f"Missing extra leagues from enrichment config: {missing}")

    rows: List[Dict[str, Any]] = []
    for code in EXTRA_CODES:
        league = by_code[code]
        request_state = {"count": 0}
        cache_path = stats.cache_path_for(league)
        existing = stats.load_json(cache_path, {})
        existing_by_id = stats.cached_fixture_map(existing)
        notes: List[str] = []

        all_fixtures, completed, query_used, query_notes = stats.fetch_fixtures_with_fallback(
            api_key, league, request_state, PER_LEAGUE_REQUEST_CAP
        )
        notes.extend(query_notes)

        # Always retain metadata for every completed fixture returned. This is safe and
        # gives canonical team names for odds mapping even when the stats request budget
        # is not enough to enrich every fixture in one pass.
        completed = sorted(completed, key=lambda f: str(((f.get("fixture") or {}).get("date") or "")))
        for fixture in completed:
            fixture_id = stats.fixture_identity(fixture)
            if fixture_id is None:
                continue
            current = existing_by_id.get(fixture_id, {})
            existing_by_id[fixture_id] = stats.merge_cached_fixture(current, fixture, query_used)

        fetched = 0
        already = 0
        # Prioritize newest completed matches. Re-running this safe pass later fills the
        # next missing fixtures because already enriched fixtures are skipped.
        for fixture in reversed(completed):
            fixture_id = stats.fixture_identity(fixture)
            if fixture_id is None:
                continue
            item = existing_by_id[fixture_id]
            if stats.has_cached_stats(item):
                already += 1
                continue
            if request_state["count"] >= PER_LEAGUE_REQUEST_CAP:
                break
            payload = stats.api_get(
                api_key,
                "fixtures/statistics",
                {"fixture": fixture_id},
                request_state,
                PER_LEAGUE_REQUEST_CAP,
            )
            raw = stats.response_items(payload)
            item["raw_statistics"] = raw
            item["normalized_stats"] = stats.normalize_statistics(raw, fixture)
            existing_by_id[fixture_id] = item
            fetched += 1

        stats.write_json(cache_path, stats.cache_payload(league, existing_by_id.values()))
        rows.append({
            "leagueCode": code,
            "country": league.get("country"),
            "league": league.get("display_name"),
            "apiFootballLeagueId": league.get("api_football_league_id"),
            "season": league.get("season"),
            "completedFixturesReturned": len(completed),
            "cacheFixtureCount": len(existing_by_id),
            "statsFetchedThisPass": fetched,
            "alreadyHadStats": already,
            "requestsUsed": request_state["count"],
            "requestCap": PER_LEAGUE_REQUEST_CAP,
            "fixtureQuery": query_used,
            "notes": notes,
            "cachePath": str(cache_path.relative_to(ROOT)).replace("\\", "/"),
        })

    save(REPORT, {
        "mode": "extra-12-only-safe",
        "base19Touched": False,
        "extraLeagueCount": 12,
        "perLeagueRequestCap": PER_LEAGUE_REQUEST_CAP,
        "maxPossibleRequests": PER_LEAGUE_REQUEST_CAP * len(EXTRA_CODES),
        "leagues": rows,
    })
    print(json.dumps({"extraLeagueCount": 12, "rows": rows}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
