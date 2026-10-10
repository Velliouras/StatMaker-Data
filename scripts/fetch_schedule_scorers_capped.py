#!/usr/bin/env python3
"""Optional, EXPLICIT capped API-Football Schedule scorer fetch.

Default has ZERO network calls. Running with --execute is required to use
provider quota. All artifacts stay in StatMaker-Data; no Android PROD, betting,
odds, GitHub workflow or Actions are touched.

Example (after independently checking account quota and setting API_FOOTBALL_KEY):
 python scripts/fetch_schedule_scorers_capped.py --execute --phase leaders --max-requests 6
 python scripts/fetch_schedule_scorers_capped.py --execute --phase events --max-requests 12 --lookback 14
 python scripts/fetch_schedule_scorers_capped.py --phase build
No Actions are scheduled by this script.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from build_statmaker_schedule_scorers import build, TOP_LEAGUES

BASE = "https://v3.football.api-sports.io"
HARD_CAP = 40
RESERVE = 1500
TIMEOUT = 20


def read_index(root: Path) -> list[dict]:
    data = json.loads((root / "data/statmaker/domestic_enriched/index.json").read_text())
    rows = {}
    for entry in data.get("leagues", []):
        code = str(entry.get("league_code") or "")
        if not code or entry.get("lifecycle") != "active":
            continue
        old = rows.get(code)
        if old is None or (
            entry.get("stats_role") == "current_target" and
            old.get("stats_role") != "current_target"
        ) or (entry.get("stats_role") == old.get("stats_role") and
              str(entry.get("api_football_season")) > str(old.get("api_football_season"))):
            rows[code] = entry
    return list(rows.values())


def plan(root: Path, phase: str, lookback: int) -> list[tuple[str, dict, Path]]:
    archive = root / "data/api_football/schedule_scorers"
    leagues = read_index(root)
    result = []
    if phase in ("leaders", "both"):
        for entry in sorted(leagues, key=lambda r: r.get("league_code", "")):
            code = entry["league_code"]
            if code not in TOP_LEAGUES:
                continue
            season = int(entry["api_football_season"])
            lid = int(entry["api_football_league_id"])
            if lid <= 0:
                continue
            dest = archive / "leaders" / (code + "_" + str(season) + ".json")
            if dest.is_file() and (
                datetime.now(timezone.utc).timestamp() - dest.stat().st_mtime < 12 * 3600
            ):
                continue
            result.append(("players/topscorers",
                {"league": lid, "season": season},
                dest))
    if phase in ("events", "both"):
        today = datetime.now(timezone.utc).date()
        candidates = []
        for entry in leagues:
            path = root / str(entry.get("output_path", ""))
            if not path.is_file():
                continue
            if not path.resolve().is_relative_to(root / "data/statmaker/domestic_enriched"):
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            for match in data.get("matches", []):
                if match.get("status") not in {"FT", "AET", "PEN"}:
                    continue
                try:
                    date = datetime.fromisoformat(match["date_utc"].replace("Z", "+00:00"))
                except (KeyError, ValueError, TypeError):
                    continue
                if date.tzinfo is None or not (today - timedelta(days=lookback) <=
                                               date.date() <= today):
                    continue
                fid = match.get("fixture_id")
                if not isinstance(fid, int) or fid <= 0:
                    continue
                dest = archive / "events" / ("fixture_" + str(fid) + ".json")
                if dest.is_file():
                    continue
                candidates.append((date, str(entry["league_code"]), fid, dest))
        # Most recent completed games first, with no duplicates.
        seen = set()
        for date, code, fid, dest in sorted(candidates, reverse=True):
            if fid in seen:
                continue
            seen.add(fid)
            result.append(("fixtures/events", {"fixture": fid}, dest))
    return result


def get_data(api_key: str, endpoint: str, params: dict) -> tuple[list, int | None]:
    query = urlencode(params)
    request = Request(BASE + "/" + endpoint + "?" + query, headers={
        "x-apisports-key": api_key,
        "Accept": "application/json",
        "User-Agent": "StatMaker-Schedule-Scorers-Capped"
    })
    with urlopen(request, timeout=TIMEOUT) as response:
        raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError("Oversized API-Football response")
        payload = json.loads(raw.decode("utf-8"))
        remaining = response.headers.get("x-ratelimit-requests-remaining")
    if not isinstance(payload, dict) or payload.get("errors"):
        raise ValueError("Provider returned errors (keys only): " +
                         str(list((payload or {}).get("errors", {}).keys())))
    if not isinstance(payload.get("response"), list):
        raise ValueError("Missing provider response array")
    return payload["response"], int(remaining) if remaining is not None else None


def execute(root: Path, requests: list, maximum: int, api_key: str,
            reserve: int = RESERVE) -> dict:
    sent, saved, stopped = 0, 0, None
    entries = read_index(root)
    by_id = {int(e["api_football_league_id"]): e for e in entries
             if int(e.get("api_football_league_id") or 0) > 0}
    fixture_leagues = {}
    for e in entries:
        path = (root / str(e.get("output_path") or "")).resolve()
        if not path.is_file() or not path.is_relative_to(root / "data/statmaker/domestic_enriched"):
            continue
        for m in json.loads(path.read_text(encoding="utf-8")).get("matches", []):
            if m.get("fixture_id"):
                fixture_leagues[int(m["fixture_id"])] = e["league_code"]
    for endpoint, params, destination in requests[:maximum]:
        if not api_key:
            stopped = "Missing API_FOOTBALL_KEY"
            break
        rows, remaining = get_data(api_key, endpoint, params)
        sent += 1
        if remaining is None or remaining < reserve:
            stopped = "Provider remaining quota unknown or below reserve"
            break
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        if endpoint == "fixtures/events":
            fid = int(params["fixture"])
            code = fixture_leagues.get(fid)
            if code is None:
                stopped = "Missing canonical fixture identity"
                break
            record = {
                "sourceRequest": "/fixtures/events?fixture=" + str(fid),
                "fixtureId": fid, "leagueCode": code,
                "retrievedAtUTC": now, "response": rows
            }
        else:
            lid, season = int(params["league"]), int(params["season"])
            league = by_id.get(lid)
            if not league:
                stopped = "Missing canonical league identity"
                break
            record = {
                "sourceRequest": "/players/topscorers?league=" + str(lid) +
                    "&season=" + str(season),
                "leagueCode": league["league_code"], "apiFootballLeagueId": lid,
                "season": season, "retrievedAtUTC": now, "response": rows
            }
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_suffix(".tmp")
        temp.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
        temp.replace(destination)
        saved += 1
        time.sleep(0.75)
    return {"apiCalls": sent, "cachedFiles": saved, "stoppedReason": stopped,
            "requestsNotAttempted": max(0, len(requests) - sent)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repository-root", type=Path, default=Path("."))
    ap.add_argument("--execute", action="store_true", help="Explicitly spend API quota")
    ap.add_argument("--phase", choices=("build", "leaders", "events", "both"),
                    default="build")
    ap.add_argument("--max-requests", type=int, default=12)
    ap.add_argument("--lookback", type=int, default=14)
    ap.add_argument("--quota-reserve", type=int, default=RESERVE)
    args = ap.parse_args()
    if not 1 <= args.max_requests <= HARD_CAP or not 1 <= args.lookback <= 90:
        ap.error("Max 40 provider requests; lookback 1-90 days")
    if args.quota_reserve < RESERVE:
        ap.error("Quota reserve may not be below 1500")
    root = args.repository_root.resolve()
    pending = plan(root, args.phase, args.lookback)
    print(json.dumps({"phase": args.phase, "pending": len(pending),
                      "execute": args.execute,
                      "maximumRequests": args.max_requests,
                      "quotaReserve": args.quota_reserve}))
    if args.phase != "build" and not args.execute:
        print("DRY RUN, NO NETWORK. Pass --execute only with user-approved quota.")
        return
    outcome = {"apiCalls": 0, "cachedFiles": 0}
    if args.execute and args.phase != "build":
        outcome = execute(root, pending, args.max_requests,
                          os.getenv("API_FOOTBALL_KEY", ""), args.quota_reserve)
    doc = build(root,
        root / "data/api_football/schedule_scorers/events",
        root / "data/api_football/schedule_scorers/leaders")
    dest = root / "data/statmaker/schedule_scorers/season_snapshot.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    doc["apiCallsDuringGeneration"] = outcome["apiCalls"]
    dest.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"outcome": outcome,
                      "goalEvents": len(doc["goalEvents"]),
                      "topLeagues": len(doc["leagueTopScorers"])}))


if __name__ == "__main__":
    main()
