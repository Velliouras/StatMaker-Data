#!/usr/bin/env python3
"""Bounded verified domestic venue and goal-scorer read model. No betting rebuild.

Read current canonical completed matches. For venue use provider /fixtures?ids
(up to 20 IDs per request); for players use /fixtures/events?fixture=ID.
Each goal event must reconcile with the FINAL SCORE and its team ID/name must
resolve to one of the fixture teams. Missing data stays missing.
Only StatMaker-Data/main is written; requests are bounded with quota reserve.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data/statmaker/schedule_scorers/domestic_match_context.json"
CACHE = ROOT / "data/api_football/schedule_scorers"
CONTRACT = "statmaker-domestic-match-context-v1"
COMPLETED = {"FT", "AET", "PEN"}
PRIORITY = ("G1", "E0", "SP1", "I1", "D1", "F1")
MAX_CALLS = 64
RESERVE = 1500


def read(path, fallback=None):
    if not path.is_file():
        return {} if fallback is None else fallback
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def current_fixtures():
    index = read(ROOT / "data/statmaker/domestic_enriched/index.json")
    assert index.get("schema_version") == 3, "Unexpected canonical index"
    leagues = {}
    for row in index.get("leagues", []):
        code = row.get("league_code")
        if not code or row.get("lifecycle") != "active":
            continue
        prior = leagues.get(code)
        ranking = (row.get("stats_role") == "current_target",
                   str(row.get("api_football_season", "")))
        previous = (prior.get("stats_role") == "current_target",
                    str(prior.get("api_football_season", ""))) if prior else None
        if prior is None or ranking > previous:
            leagues[code] = row
    fixtures = {}
    for code, row in leagues.items():
        name = str(row.get("output_path") or "")
        if not name.startswith("data/statmaker/domestic_enriched/") or ".." in Path(name).parts:
            continue
        archive = read(ROOT / name)
        if (archive.get("competition") or {}).get("league_code") != code:
            continue
        for m in archive.get("matches", []):
            if m.get("status") not in COMPLETED:
                continue
            fid = m.get("fixture_id")
            h, a = m.get("home_goals"), m.get("away_goals")
            home, away = m.get("home_team"), m.get("away_team")
            if not isinstance(fid, int) or fid < 1 or not isinstance(h, int) or not isinstance(a, int):
                continue
            if h < 0 or a < 0 or not home or not away or home == away:
                continue
            if fid in fixtures:
                raise ValueError("Duplicate canonical fixture ID " + str(fid))
            fixtures[fid] = {"fixtureId": fid, "leagueCode": code,
                             "season": int(row["api_football_season"]),
                             "home": home, "away": away, "homeGoals": h, "awayGoals": a,
                             "date": m.get("date_utc") or ""}
    def priority(f):
        code = f["leagueCode"]
        group = PRIORITY.index(code) if code in PRIORITY else len(PRIORITY)
        # Recent first within each priority competition.
        return (group, -(int(str(f["date"])[:10].replace("-", "")) if str(f["date"])[:10].replace("-", "").isdigit() else 0),
                code, f["fixtureId"])
    return sorted(fixtures.values(), key=priority)


def http_get(endpoint, params, state, maximum, reserve):
    if state["calls"] >= maximum or state["stop"]:
        return None
    secret = os.getenv("API_FOOTBALL_KEY", "")
    if not secret:
        state["stop"] = "missing_api_key"
        return None
    request = Request("https://v3.football.api-sports.io/" + endpoint + "?" + urlencode(params),
        headers={"x-apisports-key": secret, "Accept": "application/json",
                 "User-Agent": "StatMaker-Domestic-Results-Context"})
    state["calls"] += 1
    try:
        with urlopen(request, timeout=25) as res:
            raw = res.read(3_000_001)
            remaining = res.headers.get("x-ratelimit-requests-remaining")
        if len(raw) > 3_000_000:
            raise ValueError("oversized response")
        payload = json.loads(raw)
        if remaining is None or int(remaining) < reserve:
            state["stop"] = "quota_reserve_reached_or_unknown"
            return None
        if payload.get("errors") or not isinstance(payload.get("response"), list):
            raise ValueError("invalid_provider_response")
        time.sleep(.2)
        return payload["response"]
    except (OSError, ValueError, TypeError) as error:
        state["stop"] = "provider_unavailable:" + type(error).__name__
        return None


def collect(fixtures, maximum, reserve):
    state = {"calls": 0, "stop": None, "venuesSaved": 0, "eventsSaved": 0}
    fixture_by_id = {f["fixtureId"]: f for f in fixtures}
    missing = [f for f in fixtures if not
               (CACHE / "fixtures" / ("fixture_" + str(f["fixtureId"]) + ".json")).is_file()]
    # Batch actual fixture venue requests, never infer stadium from home club.
    # Cap batches so the same update also has budget for verified scorer events.
    batches = min(5, max(1, maximum // 8))
    for offset in range(0, min(len(missing), batches * 20), 20):
        chunk = missing[offset:offset + 20]
        ids = [f["fixtureId"] for f in chunk]
        rows = http_get("fixtures", {"ids": "-".join(map(str, ids))}, state, maximum, reserve)
        if rows is None:
            break
        for row in rows:
            fid = (row.get("fixture") or {}).get("id")
            if fid not in fixture_by_id or fid not in ids:
                continue
            f = fixture_by_id[fid]
            if (row.get("league") or {}).get("season") != f["season"]:
                continue
            record = {"fixtureId": fid, "leagueCode": f["leagueCode"],
                      "retrievedAtUTC": datetime.now(timezone.utc).isoformat(),
                      "response": row}
            write(CACHE / "fixtures" / ("fixture_" + str(fid) + ".json"), record)
            state["venuesSaved"] += 1
    for f in fixtures:
        if state["stop"] or state["calls"] >= maximum:
            break
        if f["homeGoals"] + f["awayGoals"] == 0:
            continue
        fid = f["fixtureId"]
        dest = CACHE / "events" / ("fixture_" + str(fid) + ".json")
        if dest.is_file():
            continue
        rows = http_get("fixtures/events", {"fixture": fid}, state, maximum, reserve)
        if rows is None:
            break
        record = {"sourceRequest": "/fixtures/events?fixture=" + str(fid),
                  "fixtureId": fid, "leagueCode": f["leagueCode"],
                  "retrievedAtUTC": datetime.now(timezone.utc).isoformat(), "response": rows}
        # Store only complete, internally consistent event sets.
        if len([e for e in rows if e.get("type") == "Goal" and
               e.get("detail") in {"Normal Goal", "Penalty", "Own Goal"}]) != f["homeGoals"] + f["awayGoals"]:
            continue
        write(dest, record)
        state["eventsSaved"] += 1
    return state


def normalized(value):
    return "".join(ch for ch in str(value or "").casefold() if ch.isalnum())


def build(fixtures, state):
    events, venues = [], {}
    total_with_events = 0
    for f in fixtures:
        fid = f["fixtureId"]
        fixed = read(CACHE / "fixtures" / ("fixture_" + str(fid) + ".json"))
        fixture = fixed.get("response") or {}
        same = fixed.get("fixtureId") == fid and fixed.get("leagueCode") == f["leagueCode"]
        if same and (fixture.get("fixture") or {}).get("id") == fid:
            name = ((fixture.get("fixture") or {}).get("venue") or {}).get("name")
            if isinstance(name, str) and name.strip():
                venues[str(fid)] = name.strip()
        row = read(CACHE / "events" / ("fixture_" + str(fid) + ".json"))
        if (row.get("sourceRequest") != "/fixtures/events?fixture=" + str(fid) or
            row.get("fixtureId") != fid or row.get("leagueCode") != f["leagueCode"] or
            not isinstance(row.get("response"), list)):
            continue
        scored = []
        teams = fixture.get("teams") or {}
        home_id = (teams.get("home") or {}).get("id") if same else None
        away_id = (teams.get("away") or {}).get("id") if same else None
        for ev in row["response"]:
            if ev.get("type") != "Goal" or ev.get("detail") not in {"Normal Goal", "Penalty", "Own Goal"}:
                continue
            team_raw = ev.get("team") or {}
            eid = team_raw.get("id")
            if home_id is not None and eid == home_id:
                team = f["home"]
            elif away_id is not None and eid == away_id:
                team = f["away"]
            else:
                n = normalized(team_raw.get("name"))
                if n == normalized(f["home"]):
                    team = f["home"]
                elif n == normalized(f["away"]):
                    team = f["away"]
                else:
                    scored = []
                    break  # No guessing between aliases: one ambiguous event rejects timeline.
            t = ev.get("time") or {}
            player = (ev.get("player") or {}).get("name")
            minute, extra = t.get("elapsed"), t.get("extra") or 0
            if (not isinstance(minute, int) or not 0 <= minute <= 130 or
                    not isinstance(extra, int) or not 0 <= extra <= 45 or
                    not isinstance(player, str) or not player.strip()):
                scored = []
                break
            scored.append({"fixtureId": str(fid), "team": team, "player": player.strip(),
                           "minute": minute, "extraMinute": extra,
                           "ownGoal": ev["detail"] == "Own Goal",
                           "penalty": ev["detail"] == "Penalty"})
        if len(scored) == f["homeGoals"] + f["awayGoals"]:
            events.extend(scored)
            total_with_events += 1
    return {"contract": CONTRACT, "schemaVersion": 1,
            "generatedAtUTC": datetime.now(timezone.utc).isoformat(),
            "source": "VERIFIED_API_FOOTBALL_FIXTURE_DETAILS_AND_EVENTS",
            "fixtureVenues": venues,
            "goalEvents": sorted(events, key=lambda g: (
                int(g["fixtureId"]), g["minute"], g["extraMinute"])),
            "coverage": {"completedFixtures": len(fixtures), "venues": len(venues),
                         "verifiedGoalTimelines": total_with_events},
            "apiCallsDuringGeneration": state["calls"],
            "providerStopReason": state["stop"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--max-requests", type=int, default=64)
    ap.add_argument("--quota-reserve", type=int, default=1500)
    args = ap.parse_args()
    if not 1 <= args.max_requests <= MAX_CALLS or args.quota_reserve < RESERVE:
        ap.error("maximum 64 calls, minimum quota reserve 1500")
    fixtures = current_fixtures()
    state = collect(fixtures, args.max_requests, args.quota_reserve) if args.execute else {
        "calls": 0, "stop": None, "venuesSaved": 0, "eventsSaved": 0}
    data = build(fixtures, state)
    write(OUT, data)
    print(json.dumps({"coverage": data["coverage"], "network": state}, ensure_ascii=False))


if __name__ == "__main__":
    main()
