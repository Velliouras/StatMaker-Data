#!/usr/bin/env python3
"""Probe API-Football pre-match odds capabilities without changing production feeds.

The probe is intentionally small and cache-aware. It discovers the current pre-match
bet catalogue and bookmaker catalogue, then samples one upcoming Bet365 odds page.
Results are written to a repository report used to decide whether API-Football can
replace Odds-API.io for StatMaker production odds.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = ROOT / "reports" / "api_football_odds_capability_probe.json"
BASE_URL = "https://v3.football.api-sports.io"
DEFAULT_REFRESH_HOURS = 168
DEFAULT_SAMPLE_DAYS = 7


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso_z(value: dt.datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def load_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_timestamp(value: Any) -> dt.datetime | None:
    text = str(value or "").strip().replace("Z", "+00:00")
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def report_is_fresh(path: Path, refresh_hours: int) -> bool:
    payload = load_json(path, {})
    generated = parse_timestamp(payload.get("generatedAt") if isinstance(payload, dict) else None)
    if generated is None:
        return False
    return now_utc() - generated < dt.timedelta(hours=max(1, refresh_hours))


def api_get(api_key: str, endpoint: str, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    query = urlencode({key: value for key, value in (params or {}).items() if value is not None})
    url = f"{BASE_URL}/{endpoint.lstrip('/')}"
    if query:
        url += f"?{query}"
    request = Request(
        url,
        headers={
            "x-apisports-key": api_key,
            "Accept": "application/json",
            "User-Agent": "StatMaker-Data API-Football odds capability probe",
        },
        method="GET",
    )
    with urlopen(request, timeout=45) as response:
        return json.loads(response.read().decode("utf-8"))


def response_rows(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = payload.get("response") if isinstance(payload, dict) else []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def normalized_name(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().replace("/", " ").replace("-", " ").split())


def names_with_keywords(names: Iterable[str], *keywords: str) -> List[str]:
    required = [keyword.lower() for keyword in keywords]
    return sorted({name for name in names if all(keyword in normalized_name(name) for keyword in required)})


def family_candidates(names: Iterable[str]) -> Dict[str, List[str]]:
    names = list(dict.fromkeys(str(name).strip() for name in names if str(name).strip()))
    card_like = sorted({
        name for name in names
        if any(token in normalized_name(name) for token in ("card", "booking"))
    })
    shot_like = sorted({name for name in names if "shot" in normalized_name(name)})
    shot_on_target = sorted({
        name for name in shot_like
        if "target" in normalized_name(name) or "goal" in normalized_name(name)
    })
    total_shots = sorted({
        name for name in shot_like
        if name not in shot_on_target
    })
    corners = sorted({name for name in names if "corner" in normalized_name(name)})
    btts = sorted({
        name for name in names
        if "both teams" in normalized_name(name) and "score" in normalized_name(name)
    })
    team_totals = sorted({
        name for name in names
        if any(team_token in normalized_name(name) for team_token in ("home", "away", "team"))
        and any(total_token in normalized_name(name) for total_token in ("over under", "total", "goals"))
    })
    return {
        "cards": card_like,
        "shots": total_shots,
        "shotsOnTarget": shot_on_target,
        "corners": corners,
        "bothTeamsToScore": btts,
        "teamTotals": team_totals,
    }


def extract_sample_bets(rows: Iterable[Dict[str, Any]]) -> List[str]:
    names: set[str] = set()
    for row in rows:
        bookmakers = row.get("bookmakers") if isinstance(row.get("bookmakers"), list) else []
        for bookmaker in bookmakers:
            if not isinstance(bookmaker, dict):
                continue
            bets = bookmaker.get("bets") if isinstance(bookmaker.get("bets"), list) else []
            for bet in bets:
                if isinstance(bet, dict) and str(bet.get("name") or "").strip():
                    names.add(str(bet.get("name")).strip())
    return sorted(names)


def find_bookmaker_id(bookmakers: Iterable[Dict[str, Any]], name: str) -> int | None:
    target = normalized_name(name)
    for row in bookmakers:
        if normalized_name(row.get("name")) == target:
            try:
                return int(row.get("id"))
            except (TypeError, ValueError):
                return None
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe API-Football pre-match odds capabilities")
    parser.add_argument("--refresh-hours", type=int, default=DEFAULT_REFRESH_HOURS)
    parser.add_argument("--sample-days", type=int, default=DEFAULT_SAMPLE_DAYS)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if not args.force and report_is_fresh(REPORT_PATH, args.refresh_hours):
        print(f"Fresh capability report already exists: {REPORT_PATH}")
        return 0

    api_key = os.getenv("API_FOOTBALL_KEY", "").strip()
    if not api_key:
        print("ERROR: API_FOOTBALL_KEY is required.", file=sys.stderr)
        return 2

    requests_used = 0
    bets_payload = api_get(api_key, "odds/bets")
    requests_used += 1
    bookmaker_payload = api_get(api_key, "odds/bookmakers")
    requests_used += 1

    bet_rows = response_rows(bets_payload)
    bookmaker_rows = response_rows(bookmaker_payload)
    bet_names = sorted({str(row.get("name") or "").strip() for row in bet_rows if str(row.get("name") or "").strip()})
    bookmakers = [
        {"id": row.get("id"), "name": row.get("name")}
        for row in bookmaker_rows
        if row.get("id") is not None and str(row.get("name") or "").strip()
    ]

    bet365_id = find_bookmaker_id(bookmaker_rows, "Bet365")
    sample_payload: Dict[str, Any] | None = None
    sample_date: str | None = None
    if bet365_id is not None:
        today = now_utc().date()
        for offset in range(1, max(1, args.sample_days) + 1):
            date_value = (today + dt.timedelta(days=offset)).isoformat()
            payload = api_get(api_key, "odds", {"date": date_value, "bookmaker": bet365_id, "page": 1})
            requests_used += 1
            if response_rows(payload):
                sample_payload = payload
                sample_date = date_value
                break

    sample_rows = response_rows(sample_payload or {})
    sample_bet_names = extract_sample_bets(sample_rows)

    required_catalog = family_candidates(bet_names)
    required_sample = family_candidates(sample_bet_names)
    bookmaker_names = {normalized_name(row.get("name")) for row in bookmaker_rows}

    report = {
        "generatedAt": iso_z(now_utc()),
        "provider": "API-Football / API-Sports",
        "purpose": "Shadow capability audit before any production odds provider migration",
        "productionProviderChanged": False,
        "requestsUsedByProbe": requests_used,
        "catalog": {
            "betCount": len(bet_rows),
            "bets": [{"id": row.get("id"), "name": row.get("name")} for row in bet_rows],
            "requiredFamilyCandidates": required_catalog,
        },
        "bookmakers": {
            "count": len(bookmakers),
            "available": bookmakers,
            "required": {
                "Bet365": "bet365" in bookmaker_names,
                "Unibet": "unibet" in bookmaker_names,
                "Pinnacle": "pinnacle" in bookmaker_names,
            },
        },
        "bet365UpcomingSample": {
            "date": sample_date,
            "resultCount": len(sample_rows),
            "betNames": sample_bet_names,
            "requiredFamilyCandidates": required_sample,
        },
        "decisionGate": {
            "catalogHasCards": bool(required_catalog["cards"]),
            "catalogHasShots": bool(required_catalog["shots"]),
            "catalogHasShotsOnTarget": bool(required_catalog["shotsOnTarget"]),
            "catalogHasCorners": bool(required_catalog["corners"]),
            "catalogHasTeamTotals": bool(required_catalog["teamTotals"]),
            "sampleHasCards": bool(required_sample["cards"]),
            "sampleHasShots": bool(required_sample["shots"]),
            "sampleHasShotsOnTarget": bool(required_sample["shotsOnTarget"]),
            "sampleHasCorners": bool(required_sample["corners"]),
            "sampleHasTeamTotals": bool(required_sample["teamTotals"]),
            "note": "Do not migrate production solely from catalogue support; verify real fixture/league/UEFA coverage first.",
        },
    }
    write_json(REPORT_PATH, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
