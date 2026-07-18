#!/usr/bin/env python3
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any, Dict, List

import domestic_live_july_pipeline as pipeline
import update_domestic_odds_api_io as odds

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT / "data" / "statmaker" / "domestic_live_july_registry.json"
OUT = ROOT / "odds" / "odds_api_io" / "domestic_odds.json"
REPORT = ROOT / "reports" / "domestic_31_extra_odds_safe.json"
EXTRA_CODES = {"SRB","BGR","SVN","HUN","CZE","SVK","ISL","LVA","LTU","EST","FIN2","NOR2"}


def load(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def validate_exact(rows: List[Dict[str, Any]]) -> None:
    for league in rows:
        code = str(league.get("leagueCode") or "")
        if code not in EXTRA_CODES:
            raise SystemExit(f"Unexpected refreshed league: {code}")
        for match in league.get("matches", []) or []:
            if match.get("teamMappingStatus") != "matched" or match.get("usableForStats") is not True:
                raise SystemExit(f"Unmatched team mapping in {code}: {match.get('id')}")
            for market in match.get("markets", []) or []:
                if market.get("exactBookmakerOdds") is not True:
                    raise SystemExit(f"Non-exact odds detected in {code}")
                if not str(market.get("bookmaker") or "").strip():
                    raise SystemExit(f"Missing bookmaker in {code}")
                try:
                    price = float(market.get("odds"))
                except (TypeError, ValueError):
                    raise SystemExit(f"Invalid odds in {code}")
                if price <= 1.0:
                    raise SystemExit(f"Invalid decimal odds in {code}: {price}")


def main() -> int:
    api_key = os.getenv("ODDS_API_IO_KEY", "").strip()
    if not api_key:
        raise SystemExit("ODDS_API_IO_KEY is required")

    live_payload = load(LIVE, {})
    live_rows = list(live_payload.get("leagues", []) or [])
    if len(live_rows) != 31:
        raise SystemExit(f"Expected 31 live registry leagues, found {len(live_rows)}")
    by_code = {str(x.get("leagueCode") or ""): x for x in live_rows}
    if not EXTRA_CODES.issubset(by_code):
        raise SystemExit("Live registry is missing one or more extra leagues")

    selected: List[Dict[str, Any]] = []
    for code in sorted(EXTRA_CODES):
        row = dict(by_code[code])
        row["season"] = row.get("targetAppSeason") or row.get("app_season") or row.get("season")
        selected.append(row)

    # Build only the 12 new leagues. Existing odds are never passed into the fetcher
    # and therefore cannot be rewritten by provider/API failures.
    fetch_config = {
        "version": 31,
        "horizonDays": 31,
        "leagues": selected,
        "groups": {"extra_12": sorted(EXTRA_CODES)},
    }
    debug: Dict[str, Any] = {"warnings": [], "apiCalls": []}
    aliases = pipeline.generated_aliases(live_rows)
    original_loader = odds.load_aliases
    odds.load_aliases = lambda: aliases
    try:
        fresh = odds.build_output(
            fetch_config,
            selected,
            api_key,
            False,
            os.getenv("ODDS_API_IO_BOOKMAKERS", odds.DEFAULT_BOOKMAKERS).strip(),
            debug,
        )
    finally:
        odds.load_aliases = original_loader

    fresh_rows = list(fresh.get("leagues", []) or [])
    validate_exact(fresh_rows)
    fresh_by_code = {str(x.get("leagueCode") or ""): x for x in fresh_rows}

    previous = load(OUT, {})
    previous_rows = list(previous.get("leagues", []) or [])
    previous_non_extra = [copy.deepcopy(x) for x in previous_rows if str(x.get("leagueCode") or "") not in EXTRA_CODES]
    previous_non_extra_digest = canonical(previous_non_extra)
    previous_extra = {str(x.get("leagueCode") or ""): copy.deepcopy(x) for x in previous_rows if str(x.get("leagueCode") or "") in EXTRA_CODES}

    merged_extra: List[Dict[str, Any]] = []
    added_or_refreshed: List[str] = []
    preserved_extra: List[str] = []
    for code in sorted(EXTRA_CODES):
        new = fresh_by_code.get(code)
        old = previous_extra.get(code)
        if new and (new.get("matches") or []):
            merged_extra.append(new)
            added_or_refreshed.append(code)
        elif old and (old.get("matches") or []):
            merged_extra.append(old)
            preserved_extra.append(code)
        elif new is not None:
            merged_extra.append(new)
        elif old is not None:
            merged_extra.append(old)
        else:
            meta = by_code[code]
            merged_extra.append({
                "leagueCode": code,
                "country": meta.get("country"),
                "competition": meta.get("competition"),
                "season": meta.get("targetAppSeason") or meta.get("app_season") or meta.get("season"),
                "apiFootballLeagueId": meta.get("apiFootballLeagueId") or meta.get("api_football_league_id"),
                "enabledForStats": True,
                "enabledForOdds": True,
                "enabledForBetting": True,
                "matches": [],
            })

    merged = dict(previous)
    merged["generatedAt"] = fresh.get("generatedAt") or odds.now_utc()
    merged["leagues"] = previous_non_extra + merged_extra
    merged["registry"] = {
        "registryVersion": 31,
        "registryLeagueCount": 31,
        "enabledLeagueCount": 31,
        "enabledForOddsCount": 31,
        "enabledForBettingCount": 31,
        "csvImport": "inactive_archive_only",
        "statsSource": "api-football",
        "oddsSource": "odds-api-io",
    }
    merged["debug"] = {
        **(previous.get("debug") or {}),
        "safeExtra12RefreshAt": merged["generatedAt"],
        "safeExtra12AddedOrRefreshed": added_or_refreshed,
        "safeExtra12Preserved": preserved_extra,
        "safeExtra12RateLimitRemaining": debug.get("rateLimitRemaining"),
        "safeExtra12Warnings": debug.get("warnings", []),
    }

    # Absolute production guard: all pre-existing non-extra league objects must be
    # unchanged. The rebuild may only add/replace EXTRA_CODES.
    final_non_extra = [x for x in merged.get("leagues", []) or [] if str(x.get("leagueCode") or "") not in EXTRA_CODES]
    if canonical(final_non_extra) != previous_non_extra_digest:
        raise SystemExit("Guard failed: an existing Domestic betting league changed")

    save(OUT, merged)
    save(REPORT, {
        "mode": "extra-12-only-safe",
        "existingLeagueObjectsPreserved": True,
        "requested": sorted(EXTRA_CODES),
        "addedOrRefreshed": added_or_refreshed,
        "preservedExtra": preserved_extra,
        "rateLimitRemaining": debug.get("rateLimitRemaining"),
        "warnings": debug.get("warnings", []),
        "leagueReports": debug.get("leagueReports", []),
        "leaguesMissing": debug.get("leaguesMissing", []),
        "unmatchedTeams": odds.unique_unmatched_teams(debug.get("unmatchedTeams", [])),
    })
    print(json.dumps({
        "existingLeagueObjectsPreserved": True,
        "addedOrRefreshed": added_or_refreshed,
        "preservedExtra": preserved_extra,
        "rateLimitRemaining": debug.get("rateLimitRemaining"),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
