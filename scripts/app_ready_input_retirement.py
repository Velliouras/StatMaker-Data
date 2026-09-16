#!/usr/bin/env python3
import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

ASIAN_COUNTRIES = {
    "china",
    "japan",
    "saudi arabia",
    "united arab emirates",
    "south korea",
    "korea republic",
    "republic of korea",
}
ASIAN_LEAGUE_CODES = {"CHN", "JPN", "SAU", "UAE", "KOR"}

ODDS_PATH = Path("odds/odds_api_io/domestic_odds.json")
INDEX_PATH = Path("data/statmaker/domestic_enriched/index.json")
MANIFEST_PATH = Path("data/statmaker/update_manifest.json")


def norm(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[\\._-]+", " ", text)
    return re.sub(r"\\s+", " ", text)


def market_key(value: Any) -> str:
    text = str(value or "").strip().upper()
    return re.sub(r"[^A-Z0-9]+", "_", text).strip("_")


def is_asian_scope(row: Dict[str, Any]) -> bool:
    country = norm(row.get("country"))
    code = str(row.get("leagueCode") or row.get("league_code") or "").strip().upper()
    return country in ASIAN_COUNTRIES or code in ASIAN_LEAGUE_CODES


def is_retired_market(value: Any) -> bool:
    key = market_key(value)
    return "ASIAN" in key or "HANDICAP" in key


def load_json(path: Path) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit(f"{path}: expected JSON object")
    return payload


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def sanitize_odds(payload: Dict[str, Any]) -> Tuple[int, int, int, int]:
    leagues = payload.get("leagues")
    if not isinstance(leagues, list):
        raise SystemExit(f"{ODDS_PATH}: missing leagues[]")

    kept_leagues: List[Dict[str, Any]] = []
    removed_leagues = removed_matches = removed_markets = kept_matches = 0

    for raw_league in leagues:
        if not isinstance(raw_league, dict):
            continue
        if is_asian_scope(raw_league):
            removed_leagues += 1
            matches = raw_league.get("matches")
            if isinstance(matches, list):
                removed_matches += len(matches)
            continue

        league = raw_league
        matches = league.get("matches")
        if isinstance(matches, list):
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
            league = dict(raw_league)
            league["matches"] = new_matches
        kept_leagues.append(league)

    payload["leagues"] = kept_leagues
    return removed_leagues, removed_matches, removed_markets, kept_matches


def sanitize_index(payload: Dict[str, Any]) -> int:
    leagues = payload.get("leagues")
    if not isinstance(leagues, list):
        raise SystemExit(f"{INDEX_PATH}: missing leagues[]")
    kept = [
        row for row in leagues
        if isinstance(row, dict) and not is_asian_scope(row)
    ]
    removed = len(leagues) - len(kept)
    payload["leagues"] = kept
    if "league_count" in payload:
        payload["league_count"] = len(kept)
    if "leagueCount" in payload:
        payload["leagueCount"] = len(kept)
    return removed


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
    asian_odds = []
    retired_markets = []
    source_dates = []

    for league in odds.get("leagues", []):
        if not isinstance(league, dict):
            continue
        if is_asian_scope(league):
            asian_odds.append(
                f"{league.get('country','')}:{league.get('leagueCode','')}"
            )
        for match in league.get("matches", []) or []:
            if not isinstance(match, dict):
                continue
            date = str(match.get("date") or "").strip()[:10]
            if len(date) == 10:
                source_dates.append(date)
            for market in match.get("markets", []) or []:
                if isinstance(market, dict) and is_retired_market(market.get("market")):
                    retired_markets.append(str(market.get("market") or ""))

    asian_index = [
        f"{row.get('country','')}:{row.get('league_code') or row.get('leagueCode') or ''}"
        for row in index.get("leagues", [])
        if isinstance(row, dict) and is_asian_scope(row)
    ]

    if asian_odds:
        raise SystemExit(f"Asian odds scopes remain: {asian_odds[:10]}")
    if asian_index:
        raise SystemExit(f"Asian enriched-index scopes remain: {asian_index[:10]}")
    if retired_markets:
        raise SystemExit(f"Retired markets remain: {sorted(set(retired_markets))[:20]}")

    return {
        "oddsLeagueCount": len(odds.get("leagues", [])),
        "indexLeagueCount": len(index.get("leagues", [])),
        "sourceMatchCount": sum(
            len(row.get("matches", []) or [])
            for row in odds.get("leagues", [])
            if isinstance(row, dict)
        ),
        "sourceDateMin": min(source_dates) if source_dates else "",
        "sourceDateMax": max(source_dates) if source_dates else "",
        "sourceDates": sorted(set(source_dates)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Apply the only allowed App-Ready delta to current canonical inputs: remove Asian scopes and all Asian/handicap markets before the exact Saturday engine sees them."
    )
    parser.add_argument("--root", default=".")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()

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

    removed_odds_leagues = removed_matches = removed_markets = removed_index_leagues = 0
    kept_matches = 0

    if not args.check_only:
        removed_odds_leagues, removed_matches, removed_markets, kept_matches = sanitize_odds(odds)
        removed_index_leagues = sanitize_index(index)
        write_json(odds_path, odds)
        write_json(index_path, index)
        update_manifest(root, manifest, [INDEX_PATH, ODDS_PATH])
        if str(manifest.get("contentVersion") or "") != original_content_version:
            raise SystemExit("App-Ready input policy must not rewrite canonical mainContentVersion")
        write_json(manifest_path, manifest)
        odds = load_json(odds_path)
        index = load_json(index_path)

    summary = validate(odds, index)
    print(
        "APP_READY_INPUT_RETIREMENT_OK",
        f"main={original_content_version[:12]}",
        f"removed_asian_odds_leagues={removed_odds_leagues}",
        f"removed_asian_index_leagues={removed_index_leagues}",
        f"removed_matches={removed_matches}",
        f"removed_markets={removed_markets}",
        f"kept_matches={summary['sourceMatchCount'] if args.check_only else kept_matches}",
        f"date_min={summary['sourceDateMin']}",
        f"date_max={summary['sourceDateMax']}",
    )
    if summary["sourceDates"]:
        print("APP_READY_INPUT_DATES", ",".join(summary["sourceDates"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
