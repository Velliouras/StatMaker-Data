#!/usr/bin/env python3
from __future__ import annotations

import copy
import datetime as dt
import json
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
ODDS_PATH = ROOT / "odds" / "odds_api_io" / "domestic_odds.json"
REPORT_PATH = ROOT / "reports" / "domestic_31_extra_odds_salvage.json"
EXTRA = {"SRB","BGR","SVN","HUN","CZE","SVK","ISL","LVA","LTU","EST","FIN2","NOR2"}
CANDIDATE_COMMITS = [
    "00f61823d6e8dfc4be45462dcba6d78445a23e77",
    "7a12c4bbf1bcbe320319cdc79a5080ac93cd667d",
    "b9512ada40731bb0132b8f9e6315dc74b374acaf",
]


def load(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def git_json(commit: str, path: str) -> Dict[str, Any]:
    try:
        raw = subprocess.check_output(["git", "show", f"{commit}:{path}"], text=True)
    except subprocess.CalledProcessError:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def market_valid(market: Dict[str, Any]) -> bool:
    if market.get("exactBookmakerOdds") is not True:
        return False
    if not str(market.get("bookmaker") or "").strip():
        return False
    try:
        return float(market.get("odds")) > 1.0
    except (TypeError, ValueError):
        return False


def valid_match(match: Dict[str, Any], today: str) -> bool:
    date = str(match.get("date") or match.get("kickoff") or "")[:10]
    if not date or date < today:
        return False
    if match.get("teamMappingStatus") != "matched" or match.get("usableForStats") is not True:
        return False
    markets = match.get("markets", []) or []
    return bool(markets) and all(isinstance(m, dict) and market_valid(m) for m in markets)


def match_key(match: Dict[str, Any]) -> str:
    return str(match.get("id") or match.get("key") or "|").strip() or "|".join([
        str(match.get("date") or "")[:10],
        str(match.get("homeTeam") or ""),
        str(match.get("awayTeam") or ""),
    ])


def source_extra_rows() -> Tuple[Dict[str, Dict[str, Any]], Dict[str, List[str]]]:
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    merged: Dict[str, Dict[str, Any]] = {}
    provenance: Dict[str, List[str]] = {}
    for commit in CANDIDATE_COMMITS:
        payload = git_json(commit, "odds/odds_api_io/domestic_odds.json")
        for league in payload.get("leagues", []) or []:
            if not isinstance(league, dict):
                continue
            code = str(league.get("leagueCode") or "")
            if code not in EXTRA:
                continue
            valid = [copy.deepcopy(m) for m in (league.get("matches", []) or []) if isinstance(m, dict) and valid_match(m, today)]
            if not valid:
                continue
            current = merged.get(code)
            if current is None:
                current = copy.deepcopy(league)
                current["matches"] = []
                merged[code] = current
            by_match = {match_key(m): m for m in current.get("matches", []) or []}
            for match in valid:
                by_match[match_key(match)] = match
            current["matches"] = sorted(by_match.values(), key=lambda m: (str(m.get("date") or ""), str(m.get("id") or "")))
            provenance.setdefault(code, []).append(commit)
    return merged, provenance


def inventory(feed: Dict[str, Any]) -> Dict[str, Any]:
    rows = feed.get("leagues", []) or []
    data = []
    for league in rows:
        matches = league.get("matches", []) or []
        data.append({
            "leagueCode": league.get("leagueCode"),
            "country": league.get("country"),
            "competition": league.get("competition"),
            "matchCount": len(matches),
            "marketCount": sum(len(m.get("markets", []) or []) for m in matches),
        })
    return {
        "leagueRowCount": len(rows),
        "leaguesWithMatches": sum(1 for x in data if x["matchCount"] > 0),
        "matchCount": sum(x["matchCount"] for x in data),
        "marketCount": sum(x["marketCount"] for x in data),
        "leagues": data,
    }


def main() -> int:
    feed = load(ODDS_PATH, {})
    before_non_extra = [copy.deepcopy(x) for x in (feed.get("leagues", []) or []) if str(x.get("leagueCode") or "") not in EXTRA]
    before_digest = canonical(before_non_extra)
    existing_extra = {str(x.get("leagueCode") or ""): copy.deepcopy(x) for x in (feed.get("leagues", []) or []) if str(x.get("leagueCode") or "") in EXTRA}

    salvaged, provenance = source_extra_rows()
    final_extra: List[Dict[str, Any]] = []
    salvaged_codes: List[str] = []
    for code in sorted(EXTRA):
        old = existing_extra.get(code)
        recovered = salvaged.get(code)
        if recovered and recovered.get("matches"):
            if old and old.get("matches"):
                by_match = {match_key(m): copy.deepcopy(m) for m in old.get("matches", []) or []}
                for m in recovered.get("matches", []) or []:
                    by_match[match_key(m)] = copy.deepcopy(m)
                recovered["matches"] = sorted(by_match.values(), key=lambda m: (str(m.get("date") or ""), str(m.get("id") or "")))
            final_extra.append(recovered)
            salvaged_codes.append(code)
        elif old is not None:
            final_extra.append(old)

    feed["leagues"] = before_non_extra + final_extra
    after_non_extra = [x for x in feed.get("leagues", []) or [] if str(x.get("leagueCode") or "") not in EXTRA]
    if canonical(after_non_extra) != before_digest:
        raise SystemExit("Guard failed: historical salvage changed an existing non-extra betting league")

    # Revalidate every recovered market before writing.
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    for league in final_extra:
        for match in league.get("matches", []) or []:
            if not valid_match(match, today):
                raise SystemExit(f"Invalid recovered exact odds in {league.get('leagueCode')}")

    feed.setdefault("debug", {})["extra12HistoricalExactSalvage"] = {
        "at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "sourceCommits": CANDIDATE_COMMITS,
        "salvagedCodes": salvaged_codes,
        "provenance": provenance,
        "existingNonExtraObjectsPreserved": True,
    }
    save(ODDS_PATH, feed)
    report = {
        "mode": "exact-only-history-salvage",
        "existingNonExtraObjectsPreserved": True,
        "candidateCommits": CANDIDATE_COMMITS,
        "salvagedCodes": salvaged_codes,
        "provenance": provenance,
        "inventory": inventory(feed),
    }
    save(REPORT_PATH, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
