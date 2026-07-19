#!/usr/bin/env python3
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any, Dict, List

import domestic_live_july_pipeline as pipeline
import update_domestic_odds_api_io as odds
import update_domestic_odds_api_io_push_aware as push_aware

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT / "data" / "statmaker" / "domestic_live_july_registry.json"
CONFIG = ROOT / "config" / "domestic_leagues.json"
OUT = ROOT / "odds" / "odds_api_io" / "domestic_odds.json"
REPORT = ROOT / "reports" / "verified_new_domestic_odds_safe.json"

TARGET_CODES = {
    "CHN",
    "SRB",
    "BGR",
    "SVN",
    "CZE",
    "SVK",
    "LVA",
    "LTU",
    "FIN2",
}


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
        if code not in TARGET_CODES:
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
    live_by_code = {str(x.get("leagueCode") or ""): x for x in live_rows}

    config_payload = load(CONFIG, {})
    config_rows = list(config_payload.get("leagues", []) or [])
    config_by_code = {str(x.get("leagueCode") or ""): x for x in config_rows}

    missing = sorted(TARGET_CODES - set(live_by_code))
    if missing:
        raise SystemExit(f"Live registry is missing target leagues: {missing}")

    selected: List[Dict[str, Any]] = []
    for code in sorted(TARGET_CODES):
        row = dict(live_by_code[code])
        configured = config_by_code.get(code, {})
        verified_slug = str(configured.get("providerLeagueSlug") or "").strip()
        if not verified_slug:
            raise SystemExit(f"Verified providerLeagueSlug missing for {code}")
        row["providerLeagueSlug"] = verified_slug
        if configured.get("searchTerms"):
            row["searchTerms"] = configured.get("searchTerms")
        row["season"] = row.get("targetAppSeason") or row.get("app_season") or row.get("season")
        selected.append(row)

    fetch_config = {
        "version": config_payload.get("version") or 6,
        "horizonDays": int(config_payload.get("horizonDays") or 21),
        "leagues": selected,
        "groups": {"verified_new_domestic": sorted(TARGET_CODES)},
    }

    odds.COUNTRY_ALIASES["czech republic"] = ["czech republic", "czechia", "czech"]

    aliases = pipeline.generated_aliases(live_rows)
    debug: Dict[str, Any] = {"warnings": [], "apiCalls": []}

    original_loader = odds.load_aliases
    original_normalizer = odds.normalize_market
    odds.load_aliases = lambda: aliases
    odds.normalize_market = push_aware._normalize_market_with_integer_corners
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
        odds.normalize_market = original_normalizer

    fresh_rows = list(fresh.get("leagues", []) or [])
    validate_exact(fresh_rows)
    fresh_by_code = {str(x.get("leagueCode") or ""): x for x in fresh_rows}

    previous = load(OUT, {})
    previous_rows = list(previous.get("leagues", []) or [])
    previous_non_target = [
        copy.deepcopy(x)
        for x in previous_rows
        if str(x.get("leagueCode") or "") not in TARGET_CODES
    ]
    previous_non_target_digest = canonical(previous_non_target)
    previous_target = {
        str(x.get("leagueCode") or ""): copy.deepcopy(x)
        for x in previous_rows
        if str(x.get("leagueCode") or "") in TARGET_CODES
    }

    merged_targets: List[Dict[str, Any]] = []
    refreshed: List[str] = []
    preserved: List[str] = []
    empty_fresh: List[str] = []

    for code in sorted(TARGET_CODES):
        new = fresh_by_code.get(code)
        old = previous_target.get(code)
        if new and (new.get("matches") or []):
            merged_targets.append(new)
            refreshed.append(code)
        elif old and (old.get("matches") or []):
            merged_targets.append(old)
            preserved.append(code)
            if new is not None:
                empty_fresh.append(code)
        elif new is not None:
            merged_targets.append(new)
            empty_fresh.append(code)
        elif old is not None:
            merged_targets.append(old)
            preserved.append(code)
        else:
            meta = live_by_code[code]
            merged_targets.append({
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
    merged["leagues"] = previous_non_target + merged_targets
    merged["debug"] = {
        **(previous.get("debug") or {}),
        "safeVerifiedNewRefreshAt": merged["generatedAt"],
        "safeVerifiedNewTargets": sorted(TARGET_CODES),
        "safeVerifiedNewRefreshed": refreshed,
        "safeVerifiedNewPreserved": preserved,
        "safeVerifiedNewEmptyFresh": empty_fresh,
        "safeVerifiedNewRateLimitRemaining": debug.get("rateLimitRemaining"),
        "safeVerifiedNewWarnings": debug.get("warnings", []),
    }

    final_non_target = [
        x for x in merged.get("leagues", []) or []
        if str(x.get("leagueCode") or "") not in TARGET_CODES
    ]
    if canonical(final_non_target) != previous_non_target_digest:
        raise SystemExit("Guard failed: a non-target Domestic odds object changed")

    save(OUT, merged)
    save(REPORT, {
        "mode": "verified-new-domestic-odds-safe",
        "bettingEngineTouched": False,
        "nonTargetLeagueObjectsPreserved": True,
        "requested": sorted(TARGET_CODES),
        "refreshed": refreshed,
        "preserved": preserved,
        "emptyFresh": empty_fresh,
        "rateLimitRemaining": debug.get("rateLimitRemaining"),
        "warnings": debug.get("warnings", []),
        "leagueReports": debug.get("leagueReports", []),
        "leaguesMissing": debug.get("leaguesMissing", []),
        "unmatchedTeams": odds.unique_unmatched_teams(debug.get("unmatchedTeams", [])),
    })

    print(json.dumps({
        "requested": sorted(TARGET_CODES),
        "refreshed": refreshed,
        "preserved": preserved,
        "emptyFresh": empty_fresh,
        "rateLimitRemaining": debug.get("rateLimitRemaining"),
        "nonTargetLeagueObjectsPreserved": True,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
