#!/usr/bin/env python3
"""Apply only mathematically proven scorer floors from historical provider totals.

The API-Football /players/topscorers endpoint may lag finalized fixture events.
G1/2026 also uses an immutable real provider snapshot as a historical baseline.
For a player appearing in both the baseline and current provider top-20, compute
baseline goals + verified later goals from complete match-event timelines.
If the provider total is smaller, publish the proven lower bound, not an
estimate. Missing leaderboard players can be recovered from full verified team
timelines, joined by provider player ID rather than abbreviated names.
This script uses zero API requests and does not touch betting data.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import argparse
import json
from pathlib import Path
import unicodedata
from scorer_refresh_sources import priority_completed
from build_statmaker_schedule_scorers import TOP_LEAGUES
from refresh_domestic_match_context import current_fixtures, build as build_context


CONTRACT = "statmaker-schedule-scorers-v1"
BASE_CONTRACT = "statmaker-verified-g1-scorer-baseline-v1"
EVENT_CONTRACT = "statmaker-domestic-match-context-v1"
TARGET_LEAGUE = "G1"
TARGET_SEASON = 2026


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def timestamp(value):
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Scorer evidence must include UTC timestamp")
    return result.astimezone(timezone.utc)


def tokens(name):
    decomposed = unicodedata.normalize("NFKD", str(name or "")).casefold()
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return ["".join(c for c in chunk if c.isalnum())
            for chunk in plain.split() if any(c.isalnum() for c in chunk)]


def person_matches(abbreviated, full):
    a, b = tokens(abbreviated), tokens(full)
    if not a or not b:
        return False
    if a == b:
        return True
    # Provider leaderboards abbreviate given names; fixture events spell them
    # out. Require the same surname AND given-name initial, never surname alone.
    return (len(a) == 2 and len(b) >= 2 and a[-1] == b[-1] and
            len(a[0]) == 1 and a[0] == b[0][:1])


def evidence_by_fixture(games, context, baseline_at):
    if context.get("contract") != EVENT_CONTRACT:
        raise ValueError("Not verified domestic match-context data")
    valid = {}
    for m in games.get("matches") or []:
        if m.get("status") not in {"FT", "AET", "PEN"}:
            continue
        try:
            kickoff = timestamp(m["date_utc"])
            fid = str(int(m["fixture_id"]))
            home, away = int(m["home_goals"]), int(m["away_goals"])
            if kickoff <= baseline_at or home < 0 or away < 0:
                continue
            hteam, ateam = tokens(m["home_team"]), tokens(m["away_team"])
            if not hteam or not ateam or hteam == ateam:
                continue
            valid[fid] = (home + away, hteam, ateam)
        except (KeyError, ValueError, TypeError):
            continue
    by_fixture = defaultdict(list)
    for row in context.get("goalEvents") or []:
        fid = str(row.get("fixtureId") or "")
        if fid in valid:
            by_fixture[fid].append(row)
    proven = []
    for fid, (expected, home, away) in valid.items():
        rows = by_fixture.get(fid, [])
        if expected == 0 or len(rows) != expected:
            continue
        if any(not tokens(row.get("player")) or tokens(row.get("team")) not in (home, away)
               for row in rows):
            continue
        proven.extend((fid, row) for row in rows if row.get("ownGoal") is False)
    return proven


def provider_player_ids(root, code, season):
    """Use the same league/season-bound provider records as the offline builder."""
    path = root / "data/api_football/schedule_scorers/leaders" / f"{code}_{season}.json"
    if not path.is_file():
        return {}
    raw = read(path)
    lid = raw.get("apiFootballLeagueId")
    if (raw.get("leagueCode") != code or raw.get("season") != season or
            type(lid) is not int or raw.get("sourceRequest") !=
            f"/players/topscorers?league={lid}&season={season}"):
        return {}
    result = defaultdict(set)
    for row in raw.get("response", []):
        pid = (row.get("player") or {}).get("id")
        if type(pid) is not int or pid <= 0:
            continue
        for stat in row.get("statistics", []):
            if (stat.get("league") or {}).get("id") != lid or (stat.get("league") or {}).get("season") != season:
                continue
            key = (tuple(tokens((row.get("player") or {}).get("name"))),
                   tuple(tokens((stat.get("team") or {}).get("name"))))
            result[key].add(pid)
    return result


def supplement_event_leaders(root, doc):
    """Fill omissions in a provider top-20 using complete verified team seasons.

    Own goals are excluded. Every contributing timeline must reconcile with both
    sides' final scores. Abbreviated/full player names merge only through a real
    provider player ID. Incomplete coverage never creates a new season total.
    """
    if not (root / "data/statmaker/domestic_enriched/index.json").is_file():
        return []
    listings = {(g["leagueCode"], g["season"]): g for g in doc.get("leagueTopScorers", [])
                if g.get("leagueCode") in TOP_LEAGUES and g.get("verified") is True}
    fixtures = [f for f in current_fixtures(root) if (f["leagueCode"], f["season"]) in listings]
    cache = root / "data/api_football/schedule_scorers"
    model = build_context(fixtures, {"calls": 0, "stop": None}, cache=cache)
    by_fixture = defaultdict(list)
    for event in model["goalEvents"]:
        by_fixture[str(event["fixtureId"])].append(event)
    required, complete = defaultdict(set), defaultdict(set)
    totals, names, evidence = defaultdict(int), defaultdict(set), defaultdict(set)
    for f in fixtures:
        league = f["leagueCode"], f["season"]
        fid = str(f["fixtureId"])
        for team in (f["home"], f["away"]):
            required[league + (team,)].add(fid)
        events = by_fixture[fid]
        if (len(events) != f["homeGoals"] + f["awayGoals"] or
                any(not g.get("ownGoal") and (type(g.get("playerId")) is not int or g["playerId"] <= 0)
                    for g in events)):
            continue
        for team in (f["home"], f["away"]):
            complete[league + (team,)].add(fid)
        for event in events:
            if event["ownGoal"]:
                continue
            key = league + (event["team"], event["playerId"])
            totals[key] += 1
            names[key].add(event["player"])
            evidence[key].add(fid)
    changes = []
    for (code, season), group in listings.items():
        old_players = group["players"]
        provider_ids = provider_player_ids(root, code, season)
        existing_derived = {(r.get("team"), r.get("playerId")) for r in old_players if r.get("eventDerived")}
        players = [r for r in old_players if not r.get("eventDerived")]
        used = set()
        for row in players:
            match_ids = provider_ids.get((tuple(tokens(row.get("player"))), tuple(tokens(row.get("team")))), set())
            candidates = [key for key in totals if key[:2] == (code, season) and
                          tokens(key[2]) == tokens(row.get("team")) and
                          ((len(match_ids) == 1 and key[3] in match_ids) or
                           (not match_ids and any(person_matches(row.get("player"), n) or
                                                  person_matches(n, row.get("player")) for n in names[key])))]
            if len(candidates) != 1:
                continue
            key = candidates[0]
            used.add(key)
            supplied = int(row["goals"])
            if totals[key] > supplied:
                row["goals"] = totals[key]
                row["verifiedEventGoalFloor"] = {"providerGoals": supplied,
                    "verifiedMinimum": totals[key], "fixtureIds": sorted(evidence[key])}
                changes.append({"player": row["player"], "team": row["team"],
                                "providerGoals": supplied, "verifiedMinimum": totals[key]})
        additions = []
        for key, goals in totals.items():
            if key[:2] != (code, season) or key in used:
                continue
            team_key = key[:3]
            full = required[team_key] <= complete[team_key]
            if not full and (key[2], key[3]) not in existing_derived:
                continue
            additions.append({"player": max(names[key], key=lambda n: (len(n), n)),
                "team": key[2], "playerId": key[3], "goals": goals, "assists": None,
                "eventDerived": True, "verifiedEventTotal": {"teamSeasonComplete": full,
                    "verifiedFixtures": len(complete[team_key]), "completedFixtures": len(required[team_key]),
                    "fixtureIds": sorted(evidence[key])}})
        additions.sort(key=lambda r: (-r["goals"], r["player"], r["team"]))
        players.extend(additions)
        players.sort(key=lambda r: -int(r["goals"]))
        # Compare complete rows so repeated runs neither double-count nor rewrite data.
        if players != old_players:
            for row in additions:
                previous = next((r for r in old_players if r.get("eventDerived") and
                                 r.get("playerId") == row["playerId"] and r.get("team") == row["team"]), None)
                if previous is None or previous.get("goals") != row["goals"]:
                    changes.append({"player": row["player"], "team": row["team"],
                                    "providerGoals": None, "verifiedMinimum": row["goals"]})
            group["players"] = players
    return changes


def apply_legacy_goal_floors(root, doc):
    folder = root / "data/statmaker/schedule_scorers"
    if not any(g.get("leagueCode") == TARGET_LEAGUE and g.get("season") == TARGET_SEASON
               and g.get("verified") is True for g in doc.get("leagueTopScorers", [])):
        return []
    if not all(p.is_file() for p in (folder / "verified_g1_baseline.json",
               folder / "domestic_match_context.json",
               root / "data/statmaker/domestic_enriched/greece_super_league_2026.json")):
        return []
    baseline = read(folder / "verified_g1_baseline.json")
    context = read(folder / "domestic_match_context.json")
    games = read(root / "data/statmaker/domestic_enriched/greece_super_league_2026.json")
    matches = {m["fixture_id"]: m for m in games.get("matches", [])}
    for _, _, row in priority_completed(root, {TARGET_LEAGUE: {
            "api_football_season": TARGET_SEASON, "api_football_league_id": 197}}):
        matches[row["fixture_id"]] = row
    games["matches"] = list(matches.values())
    if doc.get("contract") != CONTRACT or baseline.get("contract") != BASE_CONTRACT:
        raise ValueError("Unsupported scorer provenance contract")
    if baseline.get("leagueCode") != TARGET_LEAGUE or baseline.get("season") != TARGET_SEASON:
        raise ValueError("Incorrect immutable scorer baseline identity")
    if not baseline.get("providerVerified") or baseline.get("sourceRequest") != (
            "/players/topscorers?league=197&season=2026"):
        raise ValueError("Unverified baseline")
    if (games.get("competition") or {}).get("league_code") != TARGET_LEAGUE:
        raise ValueError("Canonical fixture league mismatch")
    baseline_at = timestamp(baseline["retrievedAtUTC"])
    proven = evidence_by_fixture(games, context, baseline_at)
    groups = [x for x in doc.get("leagueTopScorers", [])
              if x.get("leagueCode") == TARGET_LEAGUE and x.get("season") == TARGET_SEASON
              and x.get("verified") is True]
    if len(groups) != 1:
        raise ValueError("Missing verified current G1 scorer listing")
    league = groups[0]
    historic = baseline["players"]
    # Reconcile against the immutable baseline, not a previous derived total,
    # so repeated runs cannot double-count the same events.
    corrections = []
    for row in league.get("players", []):
        person, team = row.get("player"), row.get("team")
        candidate = [p for p in historic if tokens(p.get("team")) == tokens(team)
                     and person_matches(p.get("player"), person)]
        if len(candidate) != 1:
            continue
        base = candidate[0]
        matched = {(fid, str(event.get("player"))) for fid, event in proven
                   if tokens(event.get("team")) == tokens(team)
                   and person_matches(base.get("player"), event.get("player"))}
        # A fixture may have multiple goals scored by the same player; count
        # goal event rows, not just unique player names per fixture.
        verified_goals = sum(1 for fid, event in proven
                             if tokens(event.get("team")) == tokens(team)
                             and person_matches(base.get("player"), event.get("player")))
        if verified_goals <= 0:
            continue
        prior = int(base.get("goals", -1))
        supplied = int(row.get("goals", -1))
        lower_bound = prior + verified_goals
        if prior < 0 or supplied < 0 or lower_bound <= supplied:
            continue
        row["goals"] = lower_bound
        row["verifiedGoalFloor"] = {
            "method": "provider-baseline-plus-complete-later-match-events",
            "providerGoals": supplied,
            "baselineGoals": prior,
            "laterVerifiedGoals": verified_goals,
            "baselineRetrievedAtUTC": baseline["retrievedAtUTC"],
        }
        corrections.append({"player": person, "team": team,
                            "providerGoals": supplied, "verifiedMinimum": lower_bound})
    if corrections:
        # Stable ordering preserves the provider's ordering on tied counts.
        league["players"].sort(key=lambda row: -int(row["goals"]))
        doc["generatedAtUTC"] = datetime.now(timezone.utc).isoformat()
        doc["verifiedGoalFloorAudit"] = {
            "leagueCode": TARGET_LEAGUE,
            "season": TARGET_SEASON,
            "baselineRetrievedAtUTC": baseline["retrievedAtUTC"],
            "corrected": corrections,
            "source": "VERIFIED_CANONICAL_FINAL_SCORES_AND_GOAL_TIMELINES",
        }
    return corrections


def reconcile(root: Path):
    path = root / "data/statmaker/schedule_scorers/season_snapshot.json"
    doc = read(path)
    if doc.get("contract") != CONTRACT:
        raise ValueError("Unsupported scorer provenance contract")
    before = json.dumps(doc, ensure_ascii=False, sort_keys=True)
    corrections = apply_legacy_goal_floors(root, doc)
    event_changes = supplement_event_leaders(root, doc)
    if event_changes:
        doc["verifiedEventLeaderboardAudit"] = {
            "method": "complete-score-reconciled-events-with-provider-player-ids",
            "corrected": event_changes,
        }
    if before != json.dumps(doc, ensure_ascii=False, sort_keys=True):
        doc["generatedAtUTC"] = datetime.now(timezone.utc).isoformat()
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return corrections + event_changes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository-root", type=Path, default=Path("."))
    args = parser.parse_args()
    changes = reconcile(args.repository_root.resolve())
    print(json.dumps({"goalFloorsApplied": len(changes), "corrections": changes},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
