#!/usr/bin/env python3
"""Repair exact API-Football league IDs for unresolved Domestic competitions.

The repair is fail-closed:
- only leagues still unresolved after the previous archive rehydrate are inspected;
- API-Football /leagues is queried by exact country + target season;
- a configured ID changes only when exactly one returned competition has the same
  normalized competition/display name;
- source configs and the runtime registry are updated together;
- the stale roster entry is removed so the next roster repair re-discovers it.
"""
from __future__ import annotations

import argparse
import json
import os
import unicodedata
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import api_football_daily_quota_guard as quota_guard
import api_football_fetch_fixture_stats as stats_fetch
import domestic_live_july_pipeline as pipeline

ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "data" / "statmaker" / "domestic_live_july_registry.json"
ROSTER_PATH = ROOT / "data" / "statmaker" / "domestic_rosters.json"
REHYDRATE_REPORT_PATH = ROOT / "reports" / "domestic_archive_betting_rehydrate.json"
REPORT_PATH = ROOT / "reports" / "domestic_api_football_league_identity_repair.json"
CONFIG_PATHS = [
    ROOT / "config" / "domestic_leagues.json",
    ROOT / "config" / "api_football_enrichment_leagues.json",
    ROOT / "config" / "nordic_extra_leagues.json",
    ROOT / "config" / "july_extra_leagues_2026.json",
]
DEFAULT_MAX_REQUESTS = 10


def load(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def norm(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def registry_by_code(payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {
        str(row.get("leagueCode") or "").strip().upper(): row
        for row in payload.get("leagues", []) or []
        if isinstance(row, dict) and str(row.get("leagueCode") or "").strip()
    }


def exact_catalog_match(
    meta: Dict[str, Any],
    items: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    expected = {
        norm(meta.get("competition")),
        norm(meta.get("display_name")),
    }
    expected.discard("")
    candidates: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        league = item.get("league") or {}
        country = item.get("country") or {}
        name = norm(league.get("name"))
        if name not in expected:
            continue
        expected_country = norm(meta.get("country"))
        actual_country = norm(country.get("name"))
        if expected_country and actual_country and expected_country != actual_country:
            continue
        try:
            league_id = int(league.get("id"))
        except (TypeError, ValueError):
            continue
        candidates.append({
            "id": league_id,
            "name": str(league.get("name") or ""),
            "country": str(country.get("name") or ""),
        })
    return candidates[0] if len(candidates) == 1 else None


def update_ids(node: Any, code: str, new_id: int) -> int:
    changed = 0
    if isinstance(node, list):
        for item in node:
            changed += update_ids(item, code, new_id)
        return changed
    if not isinstance(node, dict):
        return 0

    row_code = str(
        node.get("leagueCode")
        or node.get("league_code")
        or node.get("football_data_code")
        or ""
    ).strip().upper()
    if row_code == code:
        for key in ("apiFootballLeagueId", "api_football_league_id"):
            if key in node and node.get(key) != new_id:
                node[key] = new_id
                changed += 1

    for value in node.values():
        if isinstance(value, (dict, list)):
            changed += update_ids(value, code, new_id)
    return changed


def remove_roster_code(payload: Dict[str, Any], code: str) -> int:
    rows = payload.get("leagues", []) if isinstance(payload, dict) else []
    before = len(rows)
    payload["leagues"] = [
        row for row in rows
        if not isinstance(row, dict)
        or str(row.get("leagueCode") or "").strip().upper() != code
    ]
    removed = before - len(payload["leagues"])
    if removed:
        payload["leagueCount"] = len(payload["leagues"])
        payload["generatedAt"] = pipeline.now_utc()
    return removed


def target_cache_league(meta: Dict[str, Any]) -> Dict[str, Any]:
    row = dict(meta)
    target_season = str(
        meta.get("targetApiSeason")
        or meta.get("season")
        or ""
    ).strip()
    target_app_season = str(
        meta.get("targetAppSeason")
        or meta.get("app_season")
        or ""
    ).strip()
    if target_season:
        row["season"] = target_season
        row["historyApiSeason"] = target_season
    if target_app_season:
        row["app_season"] = target_app_season
    return row


def stale_stats_cache_identities(
    registry_payload: Dict[str, Any],
) -> List[Dict[str, Any]]:
    stale: List[Dict[str, Any]] = []
    for meta in registry_payload.get("leagues", []) or []:
        if not isinstance(meta, dict):
            continue
        code = str(meta.get("leagueCode") or "").strip().upper()
        if not code:
            continue
        cache_league = target_cache_league(meta)
        cache_path = stats_fetch.cache_path_for(cache_league)
        if not cache_path.exists():
            continue
        cache = load(cache_path, {})
        reason = stats_fetch.cache_identity_mismatch_reason(
            cache_league,
            cache,
        )
        if reason:
            stale.append({
                "leagueCode": code,
                "cachePath": str(cache_path.relative_to(ROOT)).replace("\\", "/"),
                "reason": reason,
            })
    return stale


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-requests", type=int, default=DEFAULT_MAX_REQUESTS)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    api_key = os.getenv("API_FOOTBALL_KEY", "").strip()
    if not api_key:
        print("ERROR: API_FOOTBALL_KEY is required.")
        return 2

    quota_guard.install(stats_fetch)
    registry_payload = load(REGISTRY_PATH, {})
    by_code = registry_by_code(registry_payload)
    unresolved_report = load(REHYDRATE_REPORT_PATH, {})
    unresolved_codes = sorted({
        str(row.get("leagueCode") or "").strip().upper()
        for row in unresolved_report.get("unresolvedHistoricalTeams", []) or []
        if isinstance(row, dict) and str(row.get("leagueCode") or "").strip()
    })

    request_state = {"count": 0}
    max_requests = max(0, int(args.max_requests))
    repairs: List[Dict[str, Any]] = []
    inspected: List[Dict[str, Any]] = []

    config_payloads = {
        path: load(path, {})
        for path in CONFIG_PATHS
        if path.exists()
    }
    roster_payload = load(ROSTER_PATH, {})

    for code in unresolved_codes:
        if request_state["count"] >= max_requests:
            break
        meta = by_code.get(code)
        if not meta:
            continue
        season = meta.get("targetApiSeason") or meta.get("season")
        country = str(meta.get("country") or "").strip()
        if not season or not country:
            continue
        try:
            payload = stats_fetch.api_get(
                api_key,
                "leagues",
                {"country": country, "season": int(season)},
                request_state,
                max_requests,
            )
        except stats_fetch.RequestLimitReached:
            break
        except Exception as exc:
            inspected.append({
                "leagueCode": code,
                "status": "api_error",
                "error": f"{type(exc).__name__}: {exc}",
            })
            continue

        items = stats_fetch.response_items(payload)
        candidate = exact_catalog_match(meta, items)
        configured = meta.get("apiFootballLeagueId") or meta.get("api_football_league_id")
        try:
            configured_id = int(configured)
        except (TypeError, ValueError):
            configured_id = None

        row = {
            "leagueCode": code,
            "country": country,
            "competition": meta.get("competition"),
            "season": str(season),
            "configuredId": configured_id,
            "exactCatalogMatch": candidate,
        }
        inspected.append(row)
        if candidate is None or candidate["id"] == configured_id:
            continue

        new_id = int(candidate["id"])
        config_changes = 0
        for config in config_payloads.values():
            config_changes += update_ids(config, code, new_id)
        registry_changes = update_ids(registry_payload, code, new_id)
        roster_removed = remove_roster_code(roster_payload, code)
        repairs.append({
            **row,
            "newId": new_id,
            "configFieldsChanged": config_changes,
            "registryFieldsChanged": registry_changes,
            "staleRosterEntriesRemoved": roster_removed,
            "policy": "unique exact API-Football country+season+competition catalog identity",
        })

    if repairs:
        for path, payload in config_payloads.items():
            save(path, payload)
        save(REGISTRY_PATH, registry_payload)
        save(ROSTER_PATH, roster_payload)

    stale_stats = stale_stats_cache_identities(registry_payload)

    report = {
        "generatedAt": pipeline.now_utc(),
        "source": "API-Football exact league catalog identity",
        "oddsApiCalls": 0,
        "apiFootballRequestsUsed": request_state["count"],
        "apiFootballMaxRequests": max_requests,
        "unresolvedLeagueCodesInspected": unresolved_codes,
        "repairCount": len(repairs),
        "repairs": repairs,
        "staleStatsCacheCount": len(stale_stats),
        "staleStatsCaches": stale_stats,
        "inspected": inspected,
        "apiFootballQuotaGuard": quota_guard.status(),
    }
    save(REPORT_PATH, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
