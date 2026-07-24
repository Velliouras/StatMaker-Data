#!/usr/bin/env python3
"""One-shot shadow coverage audit for replacing Odds-API.io with API-Football odds.

Tests real upcoming fixtures from the authoritative 27-league Domestic scope plus
UEFA Champions/Europa/Conference League. It does not write production odds feeds.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "https://v3.football.api-sports.io"
SCOPE_PATH = ROOT / "config" / "statmaker_final_domestic_scope.json"
DOMESTIC_CONFIG_PATH = ROOT / "config" / "domestic_leagues.json"
OUT_PATH = ROOT / "reports" / "api_football_odds_scope_coverage.json"
PREFERRED_BOOKMAKERS = [(8, "Bet365"), (4, "Pinnacle"), (16, "Unibet")]
HORIZON_DAYS = 14
REQUEST_DELAY_SECONDS = 0.7


def load_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    return " ".join(text.replace("/", " ").replace("-", " ").split())


def api_get(api_key: str, path: str, params: Mapping[str, Any]) -> Dict[str, Any]:
    url = f"{BASE_URL}{path}?{urlencode({k: v for k, v in params.items() if v is not None})}"
    req = Request(
        url,
        headers={
            "x-apisports-key": api_key,
            "Accept": "application/json",
            "User-Agent": "StatMaker-Data odds-scope-coverage-probe",
        },
    )
    with urlopen(req, timeout=45) as response:
        payload = json.loads(response.read().decode("utf-8"))
    time.sleep(REQUEST_DELAY_SECONDS)
    return payload if isinstance(payload, dict) else {}


def response_rows(payload: Mapping[str, Any]) -> List[Dict[str, Any]]:
    rows = payload.get("response", [])
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def fixture_id(row: Mapping[str, Any]) -> int | None:
    fixture = row.get("fixture") if isinstance(row.get("fixture"), dict) else {}
    try:
        return int(fixture.get("id"))
    except (TypeError, ValueError):
        return None


def fixture_date(row: Mapping[str, Any]) -> str:
    fixture = row.get("fixture") if isinstance(row.get("fixture"), dict) else {}
    return str(fixture.get("date") or "")


def fixture_teams(row: Mapping[str, Any]) -> str:
    teams = row.get("teams") if isinstance(row.get("teams"), dict) else {}
    home = teams.get("home") if isinstance(teams.get("home"), dict) else {}
    away = teams.get("away") if isinstance(teams.get("away"), dict) else {}
    return f"{home.get('name') or '?'} vs {away.get('name') or '?'}"


def within_horizon(value: str, today: dt.date) -> bool:
    try:
        day = dt.date.fromisoformat(value[:10])
    except ValueError:
        return False
    return today <= day <= today + dt.timedelta(days=HORIZON_DAYS)


def collect_bet_names(odds_rows: Sequence[Mapping[str, Any]]) -> tuple[str | None, List[str]]:
    for row in odds_rows:
        bookmakers = row.get("bookmakers") if isinstance(row.get("bookmakers"), list) else []
        for bookmaker in bookmakers:
            if not isinstance(bookmaker, dict):
                continue
            name = str(bookmaker.get("name") or "").strip()
            bets = bookmaker.get("bets") if isinstance(bookmaker.get("bets"), list) else []
            names = sorted({str(bet.get("name") or "").strip() for bet in bets if isinstance(bet, dict) and str(bet.get("name") or "").strip()})
            if names:
                return name or None, names
    return None, []


def has_exact(names: Iterable[str], *targets: str) -> bool:
    haystack = {normalize(name) for name in names}
    return any(normalize(target) in haystack for target in targets)


def market_flags(names: Sequence[str]) -> Dict[str, bool]:
    return {
        "matchWinner": has_exact(names, "Match Winner"),
        "goalsOverUnder": has_exact(names, "Goals Over/Under"),
        "btts": has_exact(names, "Both Teams Score"),
        "teamGoals": has_exact(names, "Total - Home") and has_exact(names, "Total - Away"),
        "matchCorners": has_exact(names, "Corners Over Under"),
        "teamCorners": has_exact(names, "Home Corners Over/Under") and has_exact(names, "Away Corners Over/Under"),
        "matchCards": has_exact(names, "Cards Over/Under"),
        "teamCards": (
            has_exact(names, "Home Team Total Cards", "Home Team Yellow Cards")
            and has_exact(names, "Away Team Total Cards", "Away Team Yellow Cards")
        ),
        "matchShots": has_exact(names, "Total Shots"),
        "teamShots": has_exact(names, "Shots. Home Total") and has_exact(names, "Shots. Away Total"),
        "matchShotsOnTarget": has_exact(names, "Total ShotOnGoal"),
        "teamShotsOnTarget": has_exact(names, "Home Total ShotOnGoal", "Home Shots On Target") and has_exact(names, "Away Total ShotOnGoal", "Away Shots On Target"),
    }


def tested_family_count(flags: Mapping[str, bool]) -> int:
    return sum(1 for value in flags.values() if value)


def fetch_one_coverage(api_key: str, league_id: int, label: str, code: str, today: dt.date) -> Dict[str, Any]:
    fixture_payload = api_get(api_key, "/fixtures", {"league": league_id, "next": 8, "timezone": "UTC"})
    fixtures = [row for row in response_rows(fixture_payload) if within_horizon(fixture_date(row), today)]
    result: Dict[str, Any] = {
        "code": code,
        "label": label,
        "apiFootballLeagueId": league_id,
        "fixtureFoundIn14DayOddsHorizon": bool(fixtures),
        "fixture": None,
        "bookmaker": None,
        "betCount": 0,
        "marketFlags": {},
        "betNames": [],
    }
    if not fixtures:
        return result

    # Try the earliest fixture(s); odds availability may differ by fixture/bookmaker.
    for fixture in fixtures[:3]:
        fid = fixture_id(fixture)
        if fid is None:
            continue
        for bookmaker_id, bookmaker_name in PREFERRED_BOOKMAKERS:
            odds_payload = api_get(api_key, "/odds", {"fixture": fid, "bookmaker": bookmaker_id})
            odds_rows = response_rows(odds_payload)
            actual_bookmaker, bet_names = collect_bet_names(odds_rows)
            if bet_names:
                result.update({
                    "fixture": {
                        "id": fid,
                        "date": fixture_date(fixture),
                        "teams": fixture_teams(fixture),
                    },
                    "bookmaker": actual_bookmaker or bookmaker_name,
                    "betCount": len(bet_names),
                    "marketFlags": market_flags(bet_names),
                    "betNames": bet_names,
                })
                return result
    return result


def domestic_rows() -> List[Dict[str, Any]]:
    scope = load_json(SCOPE_PATH, {})
    allowed = {str(code).strip().upper().replace("ROM", "ROU") for code in scope.get("includedLeagueCodes", [])}
    config = load_json(DOMESTIC_CONFIG_PATH, {})
    rows: List[Dict[str, Any]] = []
    for row in config.get("leagues", []) or []:
        if not isinstance(row, dict):
            continue
        code = str(row.get("leagueCode") or "").strip().upper().replace("ROM", "ROU")
        if code not in allowed:
            continue
        try:
            league_id = int(row.get("apiFootballLeagueId"))
        except (TypeError, ValueError):
            continue
        rows.append({
            "code": code,
            "label": f"{row.get('country') or ''} - {row.get('competition') or code}".strip(" -"),
            "leagueId": league_id,
        })
    # One row per final code, stable final-scope order.
    by_code = {row["code"]: row for row in rows}
    return [by_code[code] for code in scope.get("includedLeagueCodes", []) if code in by_code]


def discover_uefa(api_key: str) -> List[Dict[str, Any]]:
    targets = [
        ("CL", "UEFA Champions League"),
        ("EL", "UEFA Europa League"),
        ("CONF", "UEFA Europa Conference League"),
    ]
    discovered: List[Dict[str, Any]] = []
    for code, search in targets:
        payload = api_get(api_key, "/leagues", {"search": search})
        candidates = response_rows(payload)
        chosen = None
        target_norm = normalize(search)
        for row in candidates:
            league = row.get("league") if isinstance(row.get("league"), dict) else {}
            name = str(league.get("name") or "")
            if normalize(name) == target_norm:
                chosen = league
                break
        if chosen is None:
            for row in candidates:
                league = row.get("league") if isinstance(row.get("league"), dict) else {}
                if target_norm in normalize(league.get("name")):
                    chosen = league
                    break
        if chosen is not None:
            try:
                league_id = int(chosen.get("id"))
            except (TypeError, ValueError):
                continue
            discovered.append({"code": code, "label": str(chosen.get("name") or search), "leagueId": league_id})
    return discovered


def summarize(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    tested = [row for row in rows if row.get("betCount", 0)]
    families = list(market_flags([]).keys())
    counts = {
        family: sum(1 for row in tested if bool((row.get("marketFlags") or {}).get(family)))
        for family in families
    }
    return {
        "scopeCount": len(rows),
        "fixturesIn14DayHorizon": sum(1 for row in rows if row.get("fixtureFoundIn14DayOddsHorizon")),
        "fixturesWithPreferredBookmakerOdds": len(tested),
        "familyCoverageCountsAmongTestedFixtures": counts,
        "allCoreFamiliesPresentSomewhere": all(counts[name] > 0 for name in [
            "matchWinner", "goalsOverUnder", "btts", "teamGoals", "matchCorners", "teamCorners",
            "matchCards", "teamCards", "matchShots", "teamShots", "matchShotsOnTarget", "teamShotsOnTarget",
        ]),
    }


def main() -> int:
    api_key = os.getenv("API_FOOTBALL_KEY", "").strip()
    if not api_key:
        raise SystemExit("API_FOOTBALL_KEY is required")

    today = dt.datetime.now(dt.timezone.utc).date()
    domestic_meta = domestic_rows()
    domestic_results = [
        fetch_one_coverage(api_key, row["leagueId"], row["label"], row["code"], today)
        for row in domestic_meta
    ]
    uefa_meta = discover_uefa(api_key)
    uefa_results = [
        fetch_one_coverage(api_key, row["leagueId"], row["label"], row["code"], today)
        for row in uefa_meta
    ]

    report = {
        "generatedAt": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "provider": "API-Football / API-Sports PRO",
        "productionProviderChanged": False,
        "horizonDays": HORIZON_DAYS,
        "preferredBookmakers": [name for _, name in PREFERRED_BOOKMAKERS],
        "domestic": {
            "summary": summarize(domestic_results),
            "leagues": domestic_results,
        },
        "uefa": {
            "summary": summarize(uefa_results),
            "competitions": uefa_results,
        },
        "decisionRule": (
            "Do not remove Odds-API.io from production until real final-scope and UEFA fixtures show acceptable "
            "coverage for the StatMaker market families actually used by proposals."
        ),
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "domesticSummary": report["domestic"]["summary"],
        "uefaSummary": report["uefa"]["summary"],
        "report": str(OUT_PATH.relative_to(ROOT)),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
