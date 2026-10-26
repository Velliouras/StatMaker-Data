#!/usr/bin/env python3
"""Apply only mathematically proven scorer floors from historical provider totals.

The API-Football /players/topscorers endpoint may lag finalized fixture events.
Only G1/2026 is supported, using an immutable real provider snapshot as baseline.
For a player appearing in both the baseline and current provider top-20, compute
baseline goals + verified later goals from complete match-event timelines.
If the provider total is smaller, publish the proven lower bound, not an
estimate. Never overwrite a higher provider total or infer unknown players.
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


def reconcile(root: Path):
    folder = root / "data/statmaker/schedule_scorers"
    path = folder / "season_snapshot.json"
    doc = read(path)
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
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return corrections


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository-root", type=Path, default=Path("."))
    args = parser.parse_args()
    changes = reconcile(args.repository_root.resolve())
    print(json.dumps({"goalFloorsApplied": len(changes), "corrections": changes},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
