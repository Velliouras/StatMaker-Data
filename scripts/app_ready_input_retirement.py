#!/usr/bin/env python3
import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

ODDS_PATH = Path("odds/odds_api_io/domestic_odds.json")
INDEX_PATH = Path("data/statmaker/domestic_enriched/index.json")
MANIFEST_PATH = Path("data/statmaker/update_manifest.json")


def market_key(value: Any) -> str:
    text = str(value or "").strip().upper()
    return re.sub(r"[^A-Z0-9]+", "_", text).strip("_")


def is_retired_market(value: Any) -> bool:
    key = market_key(value)
    return "ASIAN" in key or "HANDICAP" in key


def load_json(path: Path) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit(f"{path}: expected JSON object")
    return payload


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def sanitize_odds(payload: Dict[str, Any]) -> Tuple[int, int, int]:
    leagues = payload.get("leagues")
    if not isinstance(leagues, list):
        raise SystemExit(f"{ODDS_PATH}: missing leagues[]")
    removed_matches = removed_markets = kept_matches = 0
    for league in leagues:
        if not isinstance(league, dict):
            continue
        matches = league.get("matches")
        if not isinstance(matches, list):
            continue
        new_matches: List[Dict[str, Any]] = []
        for raw_match in matches:
            if not isinstance(raw_match, dict):
                continue
            markets = raw_match.get("markets")
            if not isinstance(markets, list):
                new_matches.append(raw_match)
                kept_matches += 1
                continue
            kept_market_rows = [
                market for market in markets
                if isinstance(market, dict) and not is_retired_market(market.get("market"))
            ]
            removed_markets += len(markets) - len(kept_market_rows)
            if kept_market_rows:
                match = dict(raw_match)
                match["markets"] = kept_market_rows
                new_matches.append(match)
                kept_matches += 1
            else:
                removed_matches += 1
        league["matches"] = new_matches
    return removed_matches, removed_markets, kept_matches


def update_manifest(root: Path, payload: Dict[str, Any], paths: Iterable[Path]) -> None:
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list):
        raise SystemExit(f"{MANIFEST_PATH}: missing artifacts[]")
    by_path = {str(path).replace("\\", "/"): root / path for path in paths}
    seen = set()
    for item in artifacts:
        if not isinstance(item, dict):
            continue
        rel = str(item.get("path") or "")
        path = by_path.get(rel)
        if path is None:
            continue
        item["sha256"] = sha256_file(path)
        item["bytes"] = path.stat().st_size
        seen.add(rel)
    missing = sorted(set(by_path) - seen)
    if missing:
        raise SystemExit(f"{MANIFEST_PATH}: descriptors missing for {missing}")


def validate(odds: Dict[str, Any], index: Dict[str, Any]) -> Dict[str, Any]:
    retired_markets = []
    source_dates = []
    odds_leagues = odds.get("leagues", [])
    index_leagues = index.get("leagues", [])
    if not isinstance(odds_leagues, list) or not isinstance(index_leagues, list):
        raise SystemExit("App-Ready inputs require leagues[] in odds and enriched index")
    for league in odds_leagues:
        if not isinstance(league, dict):
            continue
        for match in league.get("matches", []) or []:
            if not isinstance(match, dict):
                continue
            date = str(match.get("date") or "").strip()[:10]
            if len(date) == 10:
                source_dates.append(date)
            for market in match.get("markets", []) or []:
                if isinstance(market, dict) and is_retired_market(market.get("market")):
                    retired_markets.append(str(market.get("market") or ""))
    if retired_markets:
        raise SystemExit(f"Retired markets remain: {sorted(set(retired_markets))[:20]}")
    return {
        "oddsLeagueCount": len(odds_leagues),
        "indexLeagueCount": len(index_leagues),
        "sourceMatchCount": sum(
            len(row.get("matches", []) or []) for row in odds_leagues if isinstance(row, dict)
        ),
        "sourceDateMin": min(source_dates) if source_dates else "",
        "sourceDateMax": max(source_dates) if source_dates else "",
        "sourceDates": sorted(set(source_dates)),
    }


def self_check() -> int:
    odds = {"leagues": [
        {"country":"Japan","leagueCode":"JPN","matches":[{"date":"2026-09-21","markets":[
            {"market":"RESULT_1X2"},{"market":"ASIAN_HANDICAP"},{"market":"ASIAN_GOALS"}]}]},
        {"country":"South Korea","leagueCode":"KOR","matches":[{"date":"2026-09-21","markets":[
            {"market":"MATCH_GOALS"}]}]},
    ]}
    index = {"leagues":[
        {"country":"Japan","leagueCode":"JPN"},
        {"country":"South Korea","leagueCode":"KOR"},
    ]}
    removed_matches, removed_markets, kept_matches = sanitize_odds(odds)
    validate(odds, index)
    if [x.get("leagueCode") for x in odds["leagues"]] != ["JPN","KOR"]:
        raise SystemExit("Asian geographic leagues were incorrectly removed")
    if [x.get("leagueCode") for x in index["leagues"]] != ["JPN","KOR"]:
        raise SystemExit("Asian enriched-index leagues were incorrectly removed")
    remaining=[m.get("market") for l in odds["leagues"] for x in l.get("matches",[]) for m in x.get("markets",[])]
    if remaining != ["RESULT_1X2","MATCH_GOALS"]:
        raise SystemExit(f"Market-only retirement failed: {remaining}")
    if (removed_matches,removed_markets,kept_matches)!=(0,2,2):
        raise SystemExit("Unexpected market-only retirement counts")
    print("APP_READY_MARKET_ONLY_RETIREMENT_SELF_CHECK_OK asian_leagues_preserved=2 retired_markets=2")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Remove only Asian/Handicap market types. All countries and leagues remain in scope."
    )
    parser.add_argument("--root", default=".")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        return self_check()

    root = Path(args.root).resolve()
    odds_path = root / ODDS_PATH
    index_path = root / INDEX_PATH
    manifest_path = root / MANIFEST_PATH
    odds = load_json(odds_path)
    index = load_json(index_path)
    manifest = load_json(manifest_path)
    original_content_version = str(manifest.get("contentVersion") or "")
    if not original_content_version:
        raise SystemExit("main update manifest has no contentVersion")

    removed_matches = removed_markets = kept_matches = 0
    if not args.check_only:
        removed_matches, removed_markets, kept_matches = sanitize_odds(odds)
        write_json(odds_path, odds)
        # Index is never filtered. Refresh descriptors for the real index and sanitized odds.
        update_manifest(root, manifest, [INDEX_PATH, ODDS_PATH])
        if str(manifest.get("contentVersion") or "") != original_content_version:
            raise SystemExit("App-Ready input policy must not rewrite canonical mainContentVersion")
        write_json(manifest_path, manifest)
        odds = load_json(odds_path)

    summary = validate(odds, index)
    print(
        "APP_READY_INPUT_RETIREMENT_OK",
        f"main={original_content_version[:12]}",
        "removed_country_scopes=0",
        "removed_league_scopes=0",
        f"removed_matches={removed_matches}",
        f"removed_markets={removed_markets}",
        f"kept_matches={summary['sourceMatchCount'] if args.check_only else kept_matches}",
        f"index_leagues={summary['indexLeagueCount']}",
        f"date_min={summary['sourceDateMin']}",
        f"date_max={summary['sourceDateMax']}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
