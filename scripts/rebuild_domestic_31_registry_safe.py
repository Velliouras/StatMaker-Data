#!/usr/bin/env python3
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
DOMESTIC = ROOT / "config" / "domestic_leagues.json"
ENRICHMENT = ROOT / "config" / "api_football_enrichment_leagues.json"
LIVE = ROOT / "data" / "statmaker" / "domestic_live_july_registry.json"
JULY = ROOT / "config" / "july_extra_leagues_2026.json"
NORDIC = ROOT / "config" / "nordic_extra_leagues.json"

BASE_19 = {
    "ARG", "BRA", "IRL", "USA", "CHN", "NOR", "BRA2", "SWE2", "FIN", "SWE",
    "MEX", "ROM", "DNK", "POL", "RUS", "SWZ", "AUT2", "AUT", "SC0",
}
EXTRA_12 = {"SRB", "BGR", "SVN", "HUN", "CZE", "SVK", "ISL", "LVA", "LTU", "EST", "FIN2", "NOR2"}
TARGET_31 = BASE_19 | EXTRA_12


def load(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save(path: Path, payload: Dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def upsert(items: List[Dict[str, Any]], entry: Dict[str, Any], key: str) -> None:
    code = str(entry[key]).upper()
    for i, current in enumerate(items):
        if str(current.get(key) or "").upper() == code:
            items[i] = {**current, **entry}
            return
    items.append(entry)


def add_group(groups: Dict[str, Any], name: str, code: str) -> None:
    bucket = groups.setdefault(name, [])
    if code not in bucket:
        bucket.append(code)


def extras() -> List[Dict[str, Any]]:
    rows = list(load(JULY).get("leagues", []) or [])
    rows += list(load(NORDIC).get("leagues", []) or [])
    seen = {str(row.get("leagueCode") or "").upper() for row in rows}
    if seen != EXTRA_12:
        raise SystemExit(f"Extra league set mismatch: {sorted(seen)}")
    return rows


def domestic_entry(extra: Dict[str, Any]) -> Dict[str, Any]:
    calendar = bool(extra.get("calendarYear", str(extra.get("leagueCode")) in {"ISL", "LVA", "LTU", "EST", "FIN2", "NOR2"}))
    app_season = str(extra.get("appSeason") or ("2026" if calendar else "2026-2027"))
    return {
        "leagueCode": extra["leagueCode"],
        "continent": extra.get("continent", "Europe"),
        "country": extra["country"],
        "competition": extra["competition"],
        "season": app_season,
        "apiFootballSeason": "2026",
        "group": extra.get("group", "scandinavia" if extra["leagueCode"] in {"FIN2", "NOR2"} else "central_europe"),
        "apiFootballLeagueId": int(extra["apiFootballLeagueId"]),
        "enabled": True,
        "enabledForStats": True,
        "enabledForOdds": True,
        "enabledForBetting": True,
        "providerLeagueSlug": None,
        "searchTerms": extra.get("searchTerms") or [f"{extra['country']} {extra['competition']}", extra["competition"]],
    }


def enrichment_entry(extra: Dict[str, Any]) -> Dict[str, Any]:
    code = str(extra["leagueCode"])
    calendar = bool(extra.get("calendarYear", code in {"ISL", "LVA", "LTU", "EST", "FIN2", "NOR2"}))
    history_season = "2026" if calendar else "2025"
    history_app = "2026" if calendar else "2025-2026"
    return {
        "leagueCode": code,
        "continent": extra.get("continent", "Europe"),
        "country": extra["country"],
        "display_name": extra["competition"],
        "football_data_code": code,
        "api_football_league_id": int(extra["apiFootballLeagueId"]),
        "season": history_season,
        "app_season": history_app,
        "enabled": True,
        "priority_group": extra.get("group", "scandinavia" if code in {"FIN2", "NOR2"} else "central_europe"),
    }


def live_entry(extra: Dict[str, Any]) -> Dict[str, Any]:
    code = str(extra["leagueCode"])
    calendar = bool(extra.get("calendarYear", code in {"ISL", "LVA", "LTU", "EST", "FIN2", "NOR2"}))
    history_season = "2026" if calendar else "2025"
    history_app = "2026" if calendar else "2025-2026"
    target_app = str(extra.get("appSeason") or ("2026" if calendar else "2026-2027"))
    return {
        "leagueCode": code,
        "continent": extra.get("continent", "Europe"),
        "country": extra["country"],
        "competition": extra["competition"],
        "display_name": extra["competition"],
        "football_data_code": code,
        "api_football_league_id": int(extra["apiFootballLeagueId"]),
        "apiFootballLeagueId": int(extra["apiFootballLeagueId"]),
        "season": history_season,
        "historyApiSeason": history_season,
        "app_season": history_app,
        "targetApiSeason": "2026",
        "targetAppSeason": target_app,
        "lifecycle": "active" if calendar else "starts_in_july",
        "statsVisible": True,
        "bettingRequiresExactOdds": True,
        "enabled": True,
        "enabledForStats": True,
        "enabledForOdds": True,
        "enabledForBetting": True,
        "group": extra.get("group", "scandinavia" if code in {"FIN2", "NOR2"} else "central_europe"),
        "priority_group": extra.get("group", "scandinavia" if code in {"FIN2", "NOR2"} else "central_europe"),
        "providerLeagueSlug": None,
        "searchTerms": extra.get("searchTerms") or [f"{extra['country']} {extra['competition']}", extra["competition"]],
    }


def main() -> int:
    domestic = load(DOMESTIC)
    enrichment = load(ENRICHMENT)
    live = load(LIVE)

    original_live = copy.deepcopy(live.get("leagues", []) or [])
    original_by_code = {str(row.get("leagueCode") or ""): row for row in original_live}
    if set(original_by_code) != BASE_19:
        raise SystemExit(f"Refusing rebuild: base live registry is not the expected 19 leagues: {sorted(original_by_code)}")

    domestic_leagues = domestic.setdefault("leagues", [])
    enrichment_leagues = enrichment.setdefault("leagues", [])
    groups = domestic.setdefault("groups", {})

    new_live = list(original_live)
    for extra in extras():
        code = str(extra["leagueCode"])
        upsert(domestic_leagues, domestic_entry(extra), "leagueCode")
        upsert(enrichment_leagues, enrichment_entry(extra), "leagueCode")
        group = str(extra.get("group") or ("scandinavia" if code in {"FIN2", "NOR2"} else "central_europe"))
        add_group(groups, group, code)
        add_group(groups, "all_blue_yellow", code)
        add_group(groups, "all_initial", code)
        upsert(new_live, live_entry(extra), "leagueCode")

    final_by_code = {str(row.get("leagueCode") or ""): row for row in new_live}
    if set(final_by_code) != TARGET_31:
        raise SystemExit(f"Final registry mismatch: {sorted(final_by_code)}")

    for code in BASE_19:
        if final_by_code[code] != original_by_code[code]:
            raise SystemExit(f"Guard failed: existing live registry row changed: {code}")

    domestic["version"] = max(int(domestic.get("version") or 0), 5)
    enrichment["version"] = max(int(enrichment.get("version") or 0), 4)
    live["leagueCount"] = 31
    live["selectionPolicy"] = "all configured leagues active now or starting no later than 2026-07-31"
    live["leagues"] = new_live

    save(DOMESTIC, domestic)
    save(ENRICHMENT, enrichment)
    save(LIVE, live)
    print(json.dumps({"leagueCount": 31, "preserved": sorted(BASE_19), "added": sorted(EXTRA_12)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
