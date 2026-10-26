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
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from scorer_refresh_sources import priority_completed
try:
    from api_football_budget import BudgetStop, priority
except ImportError:
    from api_budget_runtime.api_football_budget import BudgetStop, priority

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data/statmaker/schedule_scorers/domestic_match_context.json"
CACHE = ROOT / "data/api_football/schedule_scorers"
CONTRACT = "statmaker-domestic-match-context-v1"
COMPLETED = {"FT", "AET", "PEN"}
PRIORITY = ("G1", "E0", "SP1", "I1", "D1", "F1")
MAX_CALLS = 64
RESERVE = 1500


def refresh_window(now=None):
    """Define one post-match and one morning refresh window in UTC."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if now.hour >= 20:
        day, part = now.date(), "postmatch"
    elif now.hour < 5:
        day, part = now.date() - timedelta(days=1), "postmatch"
    else:
        day, part = now.date(), "morning"
    start = datetime.combine(day, datetime.min.time(), timezone.utc)
    start += timedelta(hours=20 if part == "postmatch" else 5)
    end = start + timedelta(hours=9 if part == "postmatch" else 15)
    return f"{part}-{day.isoformat()}", start, end


def already_refreshed(snapshot, now=None):
    """Deduplicate provider spending across cron and fallback triggers."""
    if snapshot.get("contract") != CONTRACT:
        return False
    key, start, end = refresh_window(now)
    if snapshot.get("refreshWindowKey") == key:
        return True
    try:
        stamp = datetime.fromisoformat(
            str(snapshot.get("generatedAtUTC") or "").replace("Z", "+00:00"))
        calls = int(snapshot.get("apiCallsDuringGeneration") or 0)
        # Backward compatibility for successful runs before window markers.
        return (stamp.tzinfo is not None and start <=
                stamp.astimezone(timezone.utc) < end and 1 <= calls <= 32)
    except (ValueError, TypeError, AttributeError):
        return False


def read(path, fallback=None):
    if not path.is_file():
        return {} if fallback is None else fallback
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)



def fixture_priority(f, today=None):
    """Recent completed matches first across leagues, then favored competitions."""
    today = today or datetime.now(timezone.utc).date()
    try:
        match_day = datetime.fromisoformat(
            str(f["date"]).replace("Z", "+00:00")).date()
        age = max(0, (today - match_day).days)
        ordinal = match_day.toordinal()
    except (ValueError, TypeError, KeyError):
        age, ordinal = 100000, 0
    window = 0 if age <= 3 else 1 if age <= 14 else 2
    code = f["leagueCode"]
    group = PRIORITY.index(code) if code in PRIORITY else len(PRIORITY)
    return (window, group, -ordinal, code, f["fixtureId"])


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
    for code, season, row in priority_completed(ROOT, leagues):
        fid = row["fixture_id"]
        if fid in fixtures and fixtures[fid]["leagueCode"] != code:
            raise ValueError("Fast result fixture league collision: " + str(fid))
        fixtures[fid] = {"fixtureId": fid, "leagueCode": code, "season": season,
                         "home": row["home_team"], "away": row["away_team"],
                         "homeGoals": row["home_goals"], "awayGoals": row["away_goals"],
                         "homeId": row.get("home_team_id"), "awayId": row.get("away_team_id"),
                         "venue": row.get("venue"), "date": row["date_utc"]}
    return sorted(fixtures.values(), key=fixture_priority)


def watchdog_candidates(fixtures, at=None):
    now = at or datetime.now(timezone.utc)
    result = []
    for f in fixtures:
        try:
            stamp = datetime.fromisoformat(f["date"].replace("Z", "+00:00"))
            if stamp.tzinfo and now - timedelta(days=14) <= stamp <= now:
                result.append(f)
        except (KeyError, ValueError, TypeError):
            continue
    return result


def watchdog_refresh_needed(fixtures, snapshot, at=None):
    now = (at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    try:
        stamp = datetime.fromisoformat(str(snapshot.get("watchdogLastAttemptAtUTC")).replace("Z", "+00:00"))
        if stamp.tzinfo and int(stamp.timestamp()) // 1800 == int(now.timestamp()) // 1800:
            return False
    except (TypeError, ValueError):
        pass
    for f in watchdog_candidates(fixtures, now):
        if f["homeGoals"] + f["awayGoals"] == 0:
            continue
        if not build([f], {"calls": 0, "stop": None})["coverage"]["verifiedGoalTimelines"]:
            return True
    return False


def fixture_budget_tier(f):
    try:
        stamp = datetime.fromisoformat(f["date"].replace("Z", "+00:00"))
        if datetime.now(timezone.utc) - stamp > timedelta(days=3):
            return "background"
    except (KeyError, TypeError, ValueError):
        return "background"
    policy = read(ROOT / "config/api_football_refresh_policy.json")
    return next((row["tier"] for row in policy.get("leagueIds", {}).values()
                 if row.get("code") == f["leagueCode"]),
                "primary" if f["leagueCode"] in PRIORITY else "background")


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
    except BudgetStop as error:
        state["stop"] = "shared_budget_stop:" + str(error)
        return None
    except (OSError, ValueError, TypeError) as error:
        state["stop"] = "provider_unavailable:" + type(error).__name__
        return None


def collect(fixtures, maximum, reserve):
    state = {"calls": 0, "stop": None, "venuesSaved": 0, "eventsSaved": 0}
    fixture_by_id = {f["fixtureId"]: f for f in fixtures}
    missing = [f for f in fixtures if not (f.get("homeId") and f.get("awayId") and f.get("venue"))
               and not (CACHE / "fixtures" / ("fixture_" + str(f["fixtureId"]) + ".json")).is_file()]
    # Batch actual fixture venue requests, never infer stadium from home club.
    # Cap batches so the same update also has budget for verified scorer events.
    batches = min(5, max(1, maximum // 8))
    for offset in range(0, min(len(missing), batches * 20), 20):
        chunk = missing[offset:offset + 20]
        ids = [f["fixtureId"] for f in chunk]
        tier = min((fixture_budget_tier(f) for f in chunk),
                   key=lambda t: {"primary": 0, "europe": 1, "background": 2}[t])
        with priority(tier):
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
        if dest.is_file() and build([f], {"calls": 0, "stop": None})["coverage"]["verifiedGoalTimelines"]:
            continue
        with priority(fixture_budget_tier(f)):
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
        if isinstance(f.get("venue"), str) and f["venue"].strip():
            venues[str(fid)] = f["venue"].strip()
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
        home_id = ((teams.get("home") or {}).get("id") if same else None) or f.get("homeId")
        away_id = ((teams.get("away") or {}).get("id") if same else None) or f.get("awayId")
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
            "providerStopReason": state["stop"],
            "refreshWindowKey": state.get("refreshWindowKey")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--check-window", action="store_true",
                    help="Check provider window freshness with zero network calls")
    ap.add_argument("--watchdog", action="store_true",
                    help="Refresh recent scorer gaps once per UTC half-hour")
    ap.add_argument("--max-requests", type=int, default=64)
    ap.add_argument("--quota-reserve", type=int, default=1500)
    args = ap.parse_args()
    if not 1 <= args.max_requests <= MAX_CALLS or args.quota_reserve < RESERVE:
        ap.error("maximum 64 calls, minimum quota reserve 1500")
    if args.check_window:
        key, _, _ = refresh_window()
        done = (not watchdog_refresh_needed(current_fixtures(), read(OUT)) if args.watchdog
                else already_refreshed(read(OUT)))
        print("refresh_needed=" + ("false" if done else "true"))
        print("refresh_window=" + key)
        return
    fixtures = current_fixtures()
    if not args.execute:
        print(json.dumps({"dryRun": True, "fixtureCount": len(fixtures),
                          "apiCalls": 0, "noFilesWritten": True}))
        return
    if args.watchdog and not watchdog_refresh_needed(fixtures, read(OUT)):
        print("SCORER_EVENTS_ALREADY_FRESH apiCalls=0")
        return
    candidates = watchdog_candidates(fixtures) if args.watchdog else fixtures
    state = collect(candidates, args.max_requests, args.quota_reserve)
    state["refreshWindowKey"] = refresh_window()[0]
    data = build(fixtures, state)
    if args.watchdog:
        data["watchdogLastAttemptAtUTC"] = datetime.now(timezone.utc).isoformat()
    write(OUT, data)
    print(json.dumps({"coverage": data["coverage"], "network": state}, ensure_ascii=False))


if __name__ == "__main__":
    main()
