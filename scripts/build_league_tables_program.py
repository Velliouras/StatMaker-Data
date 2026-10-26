#!/usr/bin/env python3
"""Publish season programmes and UEFA league tables from real cached/provider rows.

Domestic programmes use schedule_fixtures already present in the stats cache.
UEFA results/standings and phase-only scorer totals share one per-league snapshot.
No Android build, estimated fixtures/dates, bookmaker API or synthetic goals.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
try:
    from api_football_budget import BudgetStop, priority
except ImportError:
    from api_budget_runtime.api_football_budget import BudgetStop, priority

COMPLETED = {"FT", "AET", "PEN"}
CANCELLED = {"CANC", "ABD", "AWD", "WO"}
UEFA = {"CL": (2, "Champions League"), "EL": (3, "Europa League"),
        "UECL": (848, "Conference League")}
CONTRACT = "statmaker-league-tables-v1"


def read(path, default=None):
    if not path.is_file():
        return {} if default is None else default
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def instant(value):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except (TypeError, ValueError):
        return None


def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def current_leagues(root):
    selected = {}
    for row in read(root / "data/statmaker/domestic_enriched/index.json").get("leagues", []):
        code = str(row.get("league_code") or "")
        if not code or row.get("lifecycle") != "active":
            continue
        old = selected.get(code)
        priority = (row.get("stats_role") == "current_target", str(row.get("api_football_season", "")))
        previous = (old.get("stats_role") == "current_target", str(old.get("api_football_season", ""))) if old else None
        if old is None or priority > previous:
            selected[code] = row
    return selected


class Provider:
    def __init__(self, root, execute, budget, reserve, fresh_seconds=3600):
        self.root, self.execute, self.budget, self.reserve = root, execute, budget, reserve
        self.fresh_seconds = fresh_seconds
        self.calls = 0
        self.stop = None

    def get(self, key, endpoint, params, permanent=False, validator=None):
        path = self.root / "data/api_football/league_tables" / (key + ".json")
        cached = read(path)
        stamp = instant(cached.get("fetchedAtUTC"))
        fresh = stamp and 0 <= (datetime.now(timezone.utc) - stamp).total_seconds() < self.fresh_seconds
        valid_cache = (cached.get("params") == params and isinstance(cached.get("response"), list)
                       and (validator is None or validator(cached["response"])))
        if valid_cache and (permanent or fresh):
            return cached["response"]
        if not self.execute or self.stop or self.calls >= self.budget:
            return cached["response"] if valid_cache else []
        token = os.getenv("API_FOOTBALL_KEY", "")
        if not token:
            self.stop = "missing_provider_key"
            return cached["response"] if valid_cache else []
        request = Request("https://v3.football.api-sports.io/" + endpoint + "?" + urlencode(params),
                          headers={"x-apisports-key": token, "Accept": "application/json",
                                   "User-Agent": "StatMaker-League-Tables-Program"})
        self.calls += 1
        try:
            with priority("primary"), urlopen(request, timeout=25) as response:
                body = response.read(4_000_001)
                if len(body) > 4_000_000:
                    raise ValueError("oversized_provider_response")
                payload = json.loads(body)
                remaining = response.headers.get("x-ratelimit-requests-remaining")
            if remaining is None or int(remaining) < self.reserve:
                self.stop = "quota_reserve_reached_or_unknown"
            if payload.get("errors") or not isinstance(payload.get("response"), list):
                raise ValueError("invalid_provider_response")
            if validator is not None and not validator(payload["response"]):
                return cached["response"] if valid_cache else []
            write(path, {"endpoint": endpoint, "params": params, "fetchedAtUTC": now(),
                         "response": payload["response"]})
            time.sleep(0.2)
            return payload["response"]
        except BudgetStop as exc:
            self.stop = "shared_budget_stop:" + str(exc)
            return cached["response"] if valid_cache else []
        except (OSError, ValueError, TypeError):
            self.stop = "provider_response_unavailable"
            return cached["response"] if valid_cache else []


def normalize_fixture(row, code, aliases=None):
    aliases = aliases or {}
    raw_fixture = row.get("fixture") or {}
    teams = row.get("teams") or {}
    source_league = row.get("league") or row.get("source_league") or {}
    home, away = teams.get("home") or {}, teams.get("away") or {}
    home_id = home.get("id") or row.get("home_team_id")
    away_id = away.get("id") or row.get("away_team_id")
    fid = raw_fixture.get("id") or row.get("fixture_id")
    h = aliases.get(home_id) or home.get("name") or row.get("home_team")
    a = aliases.get(away_id) or away.get("name") or row.get("away_team")
    if not fid or not h or not a or h == a:
        return None
    status = (raw_fixture.get("status") or {}).get("short") or row.get("status") or "TBD"
    kickoff = raw_fixture.get("date") or row.get("date")
    date = instant(kickoff)
    goals = row.get("goals") or {}
    return {"fixture_id": int(fid), "date_utc": date.isoformat() if date else None,
            "round": source_league.get("round") or row.get("round") or "Other",
            "home_team": h, "away_team": a,
            "home_team_id": home_id, "away_team_id": away_id,
            "home_logo": home.get("logo") or row.get("home_team_logo"),
            "away_logo": away.get("logo") or row.get("away_team_logo"),
            "status": status,
            "home_goals": goals.get("home", row.get("home_goals")),
            "away_goals": goals.get("away", row.get("away_goals")),
            "venue": (raw_fixture.get("venue") or {}).get("name") or row.get("venue"),
            "league_code": code, "normalized_stats": {}}


def goal_events(raw_events, match):
    goals = []
    for row in raw_events:
        if row.get("type") != "Goal" or row.get("detail") not in {"Normal Goal", "Penalty", "Own Goal"}:
            continue
        team = row.get("team") or {}
        team_name = (match["home_team"] if team.get("id") == match.get("home_team_id")
                     else match["away_team"] if team.get("id") == match.get("away_team_id") else None)
        player = (row.get("player") or {}).get("name")
        elapsed = (row.get("time") or {}).get("elapsed")
        if not team_name or not player or not isinstance(elapsed, int) or elapsed < 0 or elapsed > 130:
            return None
        goals.append({"fixtureId": str(match["fixture_id"]), "team": team_name, "player": player,
                      "minute": elapsed, "extraMinute": (row.get("time") or {}).get("extra") or 0,
                      "ownGoal": row["detail"] == "Own Goal", "penalty": row["detail"] == "Penalty"})
    total = match.get("home_goals"), match.get("away_goals")
    if any(not isinstance(x, int) or x < 0 for x in total) or len(goals) != sum(total):
        return None
    return goals


def uefa_document(root, code, comp, provider, rows=None, standings_raw=None):
    league_id, name = UEFA[code]
    season = int(comp["season"])
    aliases = {}
    history = {}
    for f in comp.get("fixtures", []):
        history[int(f["fixtureId"])] = f
        for side in ("home", "away"):
            if f.get(side + "TeamId"):
                aliases[f[side + "TeamId"]] = f[side + "Team"]
    if rows is None:
        rows = provider.get(f"{code}_{season}_fixtures", "fixtures", {"league": league_id, "season": season})
    phase_start = str(comp.get("phaseStart") or f"{season}-09-01")
    fixtures = {}
    for row in rows:
        if (row.get("league") or {}).get("id") != league_id or (row.get("league") or {}).get("season") != season:
            continue
        match = normalize_fixture(row, code, aliases)
        if not match or (match["date_utc"] and match["date_utc"][:10] < phase_start):
            continue
        round_name = match["round"].lower()
        if any(x in round_name for x in ("qualif", "preliminary", "qualification")):
            continue
        old = history.get(match["fixture_id"])
        if old:
            match["normalized_stats"] = old.get("normalizedStats") or {}
            if old.get("status") in COMPLETED and not (match["status"] in COMPLETED and
                    all(type(match.get(k)) is int and match[k] >= 0 for k in ("home_goals", "away_goals"))):
                match.update(status=old["status"], home_goals=old.get("homeGoals"), away_goals=old.get("awayGoals"))
        fixtures[match["fixture_id"]] = match
    # Completed current-season results remain readable if a calendar refresh is unavailable.
    for fid, old in history.items():
        if fid in fixtures or old.get("status") not in COMPLETED:
            continue
        match = normalize_fixture({"fixture_id": fid, "date": old.get("kickoff"),
            "round": old.get("round"), "home_team": old.get("homeTeam"), "away_team": old.get("awayTeam"),
            "home_team_id": old.get("homeTeamId"), "away_team_id": old.get("awayTeamId"),
            "home_team_logo": old.get("homeTeamLogo"), "away_team_logo": old.get("awayTeamLogo"),
            "status": old.get("status"), "home_goals": old.get("homeGoals"), "away_goals": old.get("awayGoals")}, code)
        if match:
            match["normalized_stats"] = old.get("normalizedStats") or {}
            fixtures[fid] = match
    previous = read(root / "data/statmaker/league_tables" / (code + ".json"))
    if previous.get("season") == season and previous.get("apiFootballLeagueId") == league_id:
        for old in previous.get("fixtures", []):
            fid = old.get("fixture_id")
            if (old.get("status") in COMPLETED and fid and
                    (fid not in fixtures or fixtures[fid].get("status") not in COMPLETED)):
                fixtures[fid] = old
    if standings_raw is None:
        standings_raw = provider.get(f"{code}_{season}_standings", "standings", {"league": league_id, "season": season})
    standings = []
    for response in standings_raw:
        league = response.get("league") or {}
        if league.get("id") != league_id or league.get("season") != season:
            continue
        for group in league.get("standings") or []:
            for item in group:
                team = item.get("team") or {}
                all_games = item.get("all") or {}
                goals = all_games.get("goals") or {}
                if not team.get("name") or not isinstance(item.get("rank"), int):
                    continue
                standings.append({"rank": item["rank"], "team": aliases.get(team.get("id")) or team["name"],
                    "teamLogo": team.get("logo"), "played": all_games.get("played", 0),
                    "wins": all_games.get("win", 0), "draws": all_games.get("draw", 0),
                    "losses": all_games.get("lose", 0), "goalsFor": goals.get("for", 0),
                    "goalsAgainst": goals.get("against", 0), "points": item.get("points", 0)})
    completed = [f for f in fixtures.values() if f["status"] in COMPLETED]
    # Before the first matchday, show the known participants with zero played.
    # Alphabetical order at this point does not carry promotion/relegation zones.
    if not standings and not completed:
        logos = comp.get("teamLogos") or {}
        for index, team in enumerate(sorted(set(comp.get("participants") or [])), 1):
            standings.append({"rank": index, "team": team, "teamLogo": logos.get(team),
                "played": 0, "wins": 0, "draws": 0, "losses": 0, "goalsFor": 0, "goalsAgainst": 0, "points": 0})
    events, validated, totals = [], 0, Counter()
    for match in sorted(completed, key=lambda r: (r["date_utc"] or "", r["fixture_id"])):
        fid = match["fixture_id"]
        rows = provider.get(f"events_{fid}", "fixtures/events", {"fixture": fid}, permanent=True,
                            validator=lambda events: goal_events(events, match) is not None)
        verified = goal_events(rows, match)
        if verified is None:
            continue
        validated += 1
        events.extend(verified)
        for goal in verified:
            if not goal["ownGoal"]:
                totals[(goal["player"], goal["team"])] += 1
    leaders = [{"player": player, "team": team, "goals": goals} for (player, team), goals in totals.items()]
    leaders.sort(key=lambda p: (-p["goals"], p["player"], p["team"]))
    return {"contract": CONTRACT, "schemaVersion": 1, "generatedAtUTC": now(),
        "leagueCode": code, "country": "UEFA", "competition": name, "season": season,
        "displaySeason": str(comp.get("displaySeason") or f"{season}-{season+1}"),
        "apiFootballLeagueId": league_id, "fixtures": list(fixtures.values()),
        "standings": sorted(standings, key=lambda r: r["rank"]),
        "hasCompletedFixtures": bool(completed), "goalEvents": events,
        "scorers": {"verified": bool(fixtures) and validated == len(completed), "season": season,
                    "completedFixtures": len(completed), "verifiedFixtures": validated, "players": leaders}}


def build(root, provider, uefa_only=False):
    output = root / "data/statmaker/league_tables"
    catalogue = []
    warnings = []
    if uefa_only:
        old_index = read(output / "index.json")
        for row in old_index.get("leagues", []):
            code = str(row.get("leagueCode") or "")
            if code in UEFA or not code or "/" in code or ".." in code:
                continue
            doc = read(output / (code + ".json"))
            if (doc.get("contract") == CONTRACT and doc.get("leagueCode") == code and
                    doc.get("season") == row.get("season")):
                catalogue.append(row)
    for code, league in ({} if uefa_only else current_leagues(root)).items():
        cache_path = str(league.get("cache_path") or "")
        if not cache_path.startswith("data/api_football/fixture_stats/") or ".." in Path(cache_path).parts:
            continue
        cache = read(root / cache_path)
        expected_season = int(league["api_football_season"])
        if str(cache.get("season")) != str(expected_season):
            warnings.append(code + ": no current-season schedule cache")
            continue
        fixtures = {}
        for raw in cache.get("schedule_fixtures") or []:
            match = normalize_fixture(raw, code)
            if match and match["status"] not in COMPLETED | CANCELLED:
                fixtures[match["fixture_id"]] = match
        doc = {"contract": CONTRACT, "schemaVersion": 1, "generatedAtUTC": now(),
               "leagueCode": code, "country": league["country"], "competition": league["league"],
               "season": expected_season, "displaySeason": league["app_season"],
               "apiFootballLeagueId": league["api_football_league_id"], "fixtures": list(fixtures.values()),
               "sourceCache": cache_path}
        write(output / (code + ".json"), doc)
        catalogue.append({k: doc[k] for k in ("leagueCode", "country", "competition", "season", "displaySeason")})
    current = read(root / "data/statmaker/uefa_current_stats.json")
    uefa_sources = []
    # Fetch calendars and official tables for ALL three before spending the remaining budget on goal events.
    for comp in current.get("competitions") or []:
        code = {"CONF": "UECL"}.get(comp.get("leagueCode"), comp.get("leagueCode"))
        if code not in UEFA:
            continue
        league_id, _ = UEFA[code]
        season = int(comp["season"])
        fixtures = provider.get(f"{code}_{season}_fixtures", "fixtures", {"league": league_id, "season": season})
        table = provider.get(f"{code}_{season}_standings", "standings", {"league": league_id, "season": season})
        uefa_sources.append((code, comp, fixtures, table))
    for code, comp, fixtures, table in uefa_sources:
        doc = uefa_document(root, code, comp, provider, fixtures, table)
        old = read(output / (code + ".json"))
        if old.get("season") == doc["season"]:
            incoming = {f["fixture_id"]: f for f in old.get("fixtures", [])}
            for f in doc["fixtures"]:
                prior = incoming.get(f["fixture_id"])
                if prior and prior.get("status") in COMPLETED and f.get("status") not in COMPLETED:
                    continue
                incoming[f["fixture_id"]] = f
            doc["fixtures"] = list(incoming.values())
            if not doc["standings"]:
                doc["standings"] = old.get("standings", [])
        if not any(f["status"] not in COMPLETED | CANCELLED for f in doc["fixtures"]):
            warnings.append(code + ": no upcoming provider fixtures available")
        if not doc["scorers"]["verified"]:
            warnings.append(code + ": phase-only scorers incomplete")
        write(output / (code + ".json"), doc)
        catalogue.append({k: doc[k] for k in ("leagueCode", "country", "competition", "season", "displaySeason")})
    index = {"contract": CONTRACT, "schemaVersion": 1, "generatedAtUTC": now(),
             "leagues": catalogue, "warnings": warnings, "apiCalls": provider.calls,
             "providerStopReason": provider.stop}
    for row in catalogue:
        path = output / (row["leagueCode"] + ".json")
        doc = read(path)
        doc["generatedAtUTC"] = index["generatedAtUTC"]
        write(path, doc)
    write(output / "index.json", index)
    print(json.dumps({"leagues": len(catalogue), "apiCalls": provider.calls, "stop": provider.stop,
                      "warnings": warnings}, ensure_ascii=False), flush=True)
    return index


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--uefa-only", action="store_true",
                        help="Preserve domestic programmes without scanning full statistics caches")
    parser.add_argument("--max-requests", type=int, default=64)
    parser.add_argument("--quota-reserve", type=int, default=1500)
    args = parser.parse_args()
    if not 1 <= args.max_requests <= 64 or args.quota_reserve < 1500:
        parser.error("Maximum 64 provider calls, minimum reserve 1500")
    root = Path(__file__).resolve().parents[1]
    provider = Provider(root, args.execute, args.max_requests, args.quota_reserve,
                        fresh_seconds=900 if args.uefa_only else 3600)
    build(root, provider, uefa_only=args.uefa_only)


if __name__ == "__main__":
    main()
