#!/usr/bin/env python3
"""Refresh missing API-Football fixture mappings only for near-kickoff Domestic matches.

This is intentionally tiny and bounded:
- only unmapped matches inside the confirmed-lineup window are considered;
- at most one API-Football league-fixture request per run by default;
- recent failed mapping attempts are not retried immediately;
- legacy team names can be normalized here without weakening global matching.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from typing import Any, Dict, List

from enrich_domestic_match_context import (
    ApiBudget,
    ALIASES_PATH,
    CACHE_PATH,
    ODDS_PATH,
    REGISTRY_PATH,
    build_alias_lookup,
    candidate_matches,
    fixture_record,
    iso_utc,
    load,
    norm,
    parse_season,
    registry_by_code,
    save,
    team_key,
    now_utc,
)

REPORT_PATH = ODDS_PATH.parents[1] / "reports" / "domestic_lineup_mapping_watch.json"
LINEUP_LOOKAHEAD_MINUTES = 150
LINEUP_PAST_GRACE_MINUTES = 20
RETRY_MINUTES = 30
DEFAULT_REQUEST_CAP = 1


def minutes_since(value: Any, now: dt.datetime) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return max(0.0, (now - parsed.astimezone(dt.timezone.utc)).total_seconds() / 60.0)


def install_legacy_aliases(aliases: Dict[str, Dict[str, str]]) -> None:
    # API-Football historical/current naming can still expose Shenzhen Peng City
    # under its former name Sichuan Jiuniu. Keep the override narrow to China.
    chn = aliases.setdefault("CHN", {})
    chn[norm("Sichuan Jiuniu")] = norm("Shenzhen Peng City")


def main() -> int:
    try:
        request_cap = max(0, int(os.getenv("DOMESTIC_LINEUP_MAPPING_API_REQUEST_CAP", str(DEFAULT_REQUEST_CAP))))
    except ValueError:
        request_cap = DEFAULT_REQUEST_CAP

    now = now_utc()
    feed = load(ODDS_PATH, {})
    registry = registry_by_code(load(REGISTRY_PATH, {}))
    aliases = build_alias_lookup(load(ALIASES_PATH, {}))
    install_legacy_aliases(aliases)
    cache = load(CACHE_PATH, {"schemaVersion": 1, "matchMappings": {}, "teamLineups": {}})
    mappings = cache.setdefault("matchMappings", {})

    report: Dict[str, Any] = {
        "generatedAt": iso_utc(now),
        "requestCap": request_cap,
        "requestsUsed": 0,
        "nearKickoffUnmapped": 0,
        "eligibleForRetry": 0,
        "fixtureMappingsAdded": 0,
        "calls": [],
        "policy": {
            "lookaheadMinutes": LINEUP_LOOKAHEAD_MINUTES,
            "pastGraceMinutes": LINEUP_PAST_GRACE_MINUTES,
            "retryMinutes": RETRY_MINUTES,
            "legacyAliasOverrides": {"CHN": {"Sichuan Jiuniu": "Shenzhen Peng City"}},
        },
    }

    if not isinstance(feed, dict) or not feed.get("leagues") or request_cap <= 0:
        report["status"] = "skipped"
        save(REPORT_PATH, report)
        return 0

    rows = candidate_matches(feed, aliases, now)
    eligible: List[Dict[str, Any]] = []
    for row in rows:
        minutes_to_kickoff = (row["kickoff"] - now).total_seconds() / 60.0
        if minutes_to_kickoff < -LINEUP_PAST_GRACE_MINUTES or minutes_to_kickoff > LINEUP_LOOKAHEAD_MINUTES:
            continue
        mapping = mappings.get(row["key"]) or {}
        if mapping.get("fixtureId"):
            continue
        report["nearKickoffUnmapped"] += 1
        elapsed = minutes_since(mapping.get("lastMappingAttemptAt"), now)
        if elapsed is not None and elapsed < RETRY_MINUTES:
            continue
        eligible.append(row)

    report["eligibleForRetry"] = len(eligible)
    if not eligible:
        report["status"] = "ok"
        save(REPORT_PATH, report)
        return 0

    api_key = os.getenv("API_FOOTBALL_KEY", "").strip()
    if not api_key:
        report["status"] = "missing_api_key"
        save(REPORT_PATH, report)
        return 0

    api = ApiBudget(api_key, request_cap, report)
    by_league: Dict[str, List[Dict[str, Any]]] = {}
    for row in eligible:
        by_league.setdefault(row["leagueCode"], []).append(row)

    for code, league_rows in sorted(by_league.items(), key=lambda item: min(row["kickoff"] for row in item[1])):
        if api.remaining <= 0:
            break
        meta = registry.get(code, {})
        league_id = meta.get("apiFootballLeagueId") or meta.get("api_football_league_id")
        season = parse_season(meta.get("targetApiSeason") or meta.get("season") or league_rows[0]["league"].get("season"))
        if not league_id or season is None:
            continue

        dates = [row["kickoff"].date() for row in league_rows]
        items = api.get("/fixtures", {
            "league": league_id,
            "season": season,
            "from": min(dates).isoformat(),
            "to": max(dates).isoformat(),
            "timezone": "UTC",
        })
        attempt_at = iso_utc(now)
        for row in league_rows:
            mappings.setdefault(row["key"], {})["lastMappingAttemptAt"] = attempt_at
        if items is None:
            continue

        available = [fixture_record(item, code, aliases) for item in items]
        available = [item for item in available if item is not None]
        for row in league_rows:
            home_key = team_key(row["home"], code, aliases)
            away_key = team_key(row["away"], code, aliases)
            matches = [item for item in available if item["homeKey"] == home_key and item["awayKey"] == away_key]
            if not matches:
                continue
            best = min(matches, key=lambda item: abs((item["kickoff"] - row["kickoff"]).total_seconds()))
            if abs((best["kickoff"] - row["kickoff"]).total_seconds()) > 12 * 3600:
                continue
            mappings[row["key"]] = {
                **(mappings.get(row["key"]) or {}),
                "fixtureId": best["fixtureId"],
                "kickoff": iso_utc(best["kickoff"]),
                "homeTeamId": best["homeId"],
                "awayTeamId": best["awayId"],
                "homeTeam": row["home"],
                "awayTeam": row["away"],
                "leagueCode": code,
                "lastMappingAttemptAt": attempt_at,
            }
            report["fixtureMappingsAdded"] += 1

    cache["generatedAt"] = iso_utc(now)
    report["requestsUsed"] = api.used
    report["requestCapRespected"] = api.used <= request_cap
    report["status"] = "ok"
    save(CACHE_PATH, cache)
    save(REPORT_PATH, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
