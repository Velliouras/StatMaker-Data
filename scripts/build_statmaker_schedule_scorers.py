#!/usr/bin/env python3
"""Build verified Schedule player data from PRE-DOWNLOADED API-Football JSON.

ZERO HTTP calls, ZERO Actions, ZERO provider quota. No names or goal totals
are inferred from a scoreline. Feed files may be imported only when bound to
a unique completed canonical fixture or current top-scorer league/season.
No existing betting or App-Ready files are modified.

Input file formats (one JSON object per file):
  fixture event: {"sourceRequest":"/fixtures/events?fixture=123",
                  "fixtureId":123,"leagueCode":"E0",
                  "retrievedAtUTC":"2026-10-10T18:00:00Z",
                  "response":[<official API-Football events>]}
  league leaders: {"sourceRequest":"/players/topscorers?league=39&season=2026",
                  "leagueCode":"E0","apiFootballLeagueId":39,
                  "season":2026,"retrievedAtUTC":"2026-10-10T18:00:00Z",
                  "response":[<official API-Football players with statistics>]}
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import re

TOP_LEAGUES = ("E0", "SP1", "I1", "D1", "F1", "G1")
COMPLETED = {"FT", "AET", "PEN"}
MAX_SOURCE_BYTES = 2_000_000


def int_value(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        integer = int(value)
    except (TypeError, ValueError):
        return None
    return integer if str(integer) == str(value) and integer >= 0 else None


def utc(value):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo is not None else None


def read_json(path: Path):
    if not path.is_file() or path.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError("Missing or oversized JSON source " + str(path))
    return json.loads(path.read_text(encoding="utf-8"))


def canonical_index(root: Path):
    index = read_json(root / "data/statmaker/domestic_enriched/index.json")
    if index.get("schema_version") != 3:
        raise ValueError("Unrecognized fixture source index")
    leagues = {}
    for item in index.get("leagues", []):
        code = str(item.get("league_code") or "")
        path = str(item.get("output_path") or "")
        if not code or not path.startswith("data/statmaker/domestic_enriched/") or ".." in Path(path).parts:
            continue
        previous = leagues.get(code)
        if (previous is None or
                (item.get("stats_role") == "current_target" and previous.get("stats_role") != "current_target") or
                (item.get("stats_role") == previous.get("stats_role") and
                 str(item.get("app_season", "")) > str(previous.get("app_season", "")))):
            leagues[code] = item
    fixtures = {}
    for code, league in leagues.items():
        path = root / league["output_path"]
        payload = read_json(path)
        if str(payload.get("competition", {}).get("league_code")) != code:
            raise ValueError("Canonical league mismatch: " + code)
        for m in payload.get("matches", []):
            if m.get("status") not in COMPLETED:
                continue
            fid = str(m.get("fixture_id") or "")
            h, a = int_value(m.get("home_goals")), int_value(m.get("away_goals"))
            if not fid or h is None or a is None:
                continue
            if fid in fixtures:
                raise ValueError("Fixture ID collision in selected seasons: " + fid)
            fixtures[fid] = {"leagueCode": code, "home": str(m.get("home_team") or ""),
                             "away": str(m.get("away_team") or ""),
                             "totalGoals": h + a}
    return leagues, fixtures


def build(root: Path, events_dir: Path | None = None,
          leaders_dir: Path | None = None) -> dict:
    leagues, fixtures = canonical_index(root)
    errors = Counter()
    event_data = []
    top_data = []
    seen_fixtures = set()
    for path in sorted(events_dir.glob("*.json")) if events_dir and events_dir.is_dir() else []:
        try:
            raw = read_json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            errors["invalid_event_file"] += 1
            continue
        fid = str(raw.get("fixtureId") or "")
        canonical = fixtures.get(fid)
        if not canonical or fid in seen_fixtures:
            errors["unknown_or_repeated_fixture"] += 1
            continue
        request = str(raw.get("sourceRequest") or "")
        if (request != "/fixtures/events?fixture=" + fid or
                raw.get("leagueCode") != canonical["leagueCode"] or
                utc(raw.get("retrievedAtUTC")) is None or
                not isinstance(raw.get("response"), list)):
            errors["invalid_event_provenance"] += 1
            continue
        seen_fixtures.add(fid)
        goals = []
        for ev in raw["response"]:
            if ev.get("type") != "Goal":
                continue
            detail = str(ev.get("detail") or "").strip()
            if detail not in {"Normal Goal", "Penalty", "Own Goal"}:
                continue
            player = str((ev.get("player") or {}).get("name") or "").strip()
            team = str((ev.get("team") or {}).get("name") or "").strip()
            time = ev.get("time") or {}
            minute = int_value(time.get("elapsed"))
            extra = int_value(time.get("extra")) or 0
            if not player or not team or minute is None or minute > 130 or extra > 45:
                goals = []
                errors["incomplete_event_identity"] += 1
                break
            goals.append({"fixtureId": fid, "team": team, "player": player,
                          "minute": minute, "extraMinute": extra,
                          "ownGoal": detail == "Own Goal",
                          "penalty": detail == "Penalty"})
        # Goal-scorer timeline is published only when all recorded goals
        # reconcile with the final match total; do not silently omit players.
        if len(goals) != canonical["totalGoals"]:
            errors["score_event_count_mismatch"] += 1
            continue
        event_data.extend(goals)

    seen_leagues = set()
    for path in sorted(leaders_dir.glob("*.json")) if leaders_dir and leaders_dir.is_dir() else []:
        try:
            raw = read_json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            errors["invalid_leaders_file"] += 1
            continue
        code = str(raw.get("leagueCode") or "")
        league = leagues.get(code)
        if code not in TOP_LEAGUES or league is None or code in seen_leagues:
            errors["unknown_or_duplicate_top_league"] += 1
            continue
        league_id = int_value(league.get("api_football_league_id"))
        season = int_value(league.get("api_football_season"))
        if league_id is None or season is None:
            errors["missing_canonical_league_season"] += 1
            continue
        expected = "/players/topscorers?league=" + str(league_id) + "&season=" + str(season)
        if (raw.get("sourceRequest") != expected or
                int_value(raw.get("apiFootballLeagueId")) != league_id or
                int_value(raw.get("season")) != season or
                utc(raw.get("retrievedAtUTC")) is None or
                not isinstance(raw.get("response"), list)):
            errors["invalid_topscorer_provenance"] += 1
            continue
        players = []
        for row in raw["response"]:
            player = str((row.get("player") or {}).get("name") or "").strip()
            for stat in row.get("statistics", []):
                league_meta = stat.get("league") or {}
                goals = (stat.get("goals") or {}).get("total")
                value = int_value(goals)
                if (int_value(league_meta.get("id")) != league_id or
                        int_value(league_meta.get("season")) != season or
                        value is None or not player):
                    continue
                team = str((stat.get("team") or {}).get("name") or "").strip()
                if not team:
                    continue
                assists = int_value((stat.get("goals") or {}).get("assists"))
                players.append({"player": player, "team": team, "goals": value,
                                "assists": assists})
                break
        if not players:
            errors["no_valid_leaderboard_players"] += 1
            continue
        seen_leagues.add(code)
        # Python sort is stable: retain provider tie-break ranking rather than
        # alphabetically rearranging players with the same goal count.
        players = sorted(players, key=lambda p: -p["goals"])
        top_data.append({"leagueCode": code, "season": season, "verified": True,
                         "retrievedAtUTC": raw["retrievedAtUTC"],
                         "players": players})
    return {
        "contract": "statmaker-schedule-scorers-v1", "schemaVersion": 1,
        "generatedAtUTC": datetime.now(timezone.utc).isoformat(),
        "source": "LOCAL_VERIFIED_API_FOOTBALL_JSON_EXPORTS",
        "apiCallsDuringGeneration": 0,
        "goalEvents": sorted(event_data, key=lambda x: (
            x["fixtureId"], x["minute"], x["extraMinute"], x["player"])),
        "leagueTopScorers": sorted(top_data, key=lambda x: TOP_LEAGUES.index(x["leagueCode"])),
        "rejections": dict(sorted(errors.items())),
        "supportedTopLeagues": list(TOP_LEAGUES),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repository-root", type=Path, default=Path("."))
    ap.add_argument("--events-dir", type=Path)
    ap.add_argument("--topscorers-dir", type=Path)
    ap.add_argument("--output", type=Path,
                    default=Path("data/statmaker/schedule_scorers/season_snapshot.json"))
    args = ap.parse_args()
    root = args.repository_root.resolve()
    dest = (root / args.output).resolve()
    allowed = root / "data/statmaker/schedule_scorers"
    if not dest.is_relative_to(allowed) or dest.suffix != ".json":
        raise ValueError("Refusing to write outside schedule scorer research namespace")
    result = build(root, args.events_dir, args.topscorers_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    print(json.dumps({"goalEvents": len(result["goalEvents"]),
                      "leaguesWithVerifiedScorers": len(result["leagueTopScorers"]),
                      "providerCalls": 0, "rejections": result["rejections"]}))


if __name__ == "__main__":
    main()
