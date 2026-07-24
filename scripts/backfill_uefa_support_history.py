#!/usr/bin/env python3
"""Backfill verified history for current UEFA qualifier participants only.

The Android app never calls API-Football. This script runs repository-side and:
1. reuses the already-built Domestic UEFA support history first;
2. reads the current CL/EL/Conference qualifier feeds;
3. resolves only genuinely missing participant teams through API-Football;
4. fetches recent completed fixtures for those teams;
5. publishes compact score history plus official team logos.

No synthetic matches are created and no betting thresholds are changed.
"""
from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Sequence

import api_football_fetch_fixture_stats as api
from build_uefa_support_history import normalize_team_key

ROOT = Path(__file__).resolve().parents[1]
SUPPORT_PATH = ROOT / "data" / "statmaker" / "uefa_support_history.json"
TEAM_SUPPORT_PATH = ROOT / "data" / "statmaker" / "uefa_team_support_history.json"
LOGOS_PATH = ROOT / "data" / "statmaker" / "uefa_team_logos.json"
REPORT_PATH = ROOT / "reports" / "uefa_support_team_backfill.json"
DEFAULT_FEED_DIR = ROOT / "odds" / "odds_api_io"
FEED_NAMES = (
    "champions_league_odds.json",
    "europa_league_odds.json",
    "conference_league_odds.json",
)
COMPLETED = {"FT", "AET", "PEN"}


def load_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def participant_keys(participant: Mapping[str, Any]) -> set[str]:
    values = [
        participant.get("canonicalTeam"),
        participant.get("providerTeam"),
        *(participant.get("aliases") or []),
    ]
    return {normalize_team_key(value) for value in values if normalize_team_key(value)}


def support_index(participants: Sequence[Mapping[str, Any]]) -> Dict[str, List[int]]:
    index: MutableMapping[str, List[int]] = defaultdict(list)
    for position, participant in enumerate(participants):
        for key in participant_keys(participant):
            index[key].append(position)
    return dict(index)


def feed_team_names(feed_dir: Path) -> Dict[str, set[str]]:
    result: Dict[str, set[str]] = defaultdict(set)
    for filename in FEED_NAMES:
        payload = load_json(feed_dir / filename, {})
        competition = str(payload.get("leagueCode") or payload.get("competition") or filename).strip()
        for match in payload.get("matches", []) or []:
            if not isinstance(match, dict):
                continue
            for canonical_key, display_key, provider_key in (
                ("canonicalHomeTeam", "homeTeam", "providerHomeTeam"),
                ("canonicalAwayTeam", "awayTeam", "providerAwayTeam"),
            ):
                name = str(match.get(canonical_key) or match.get(display_key) or match.get(provider_key) or "").strip()
                if name:
                    result[competition].add(name)
    return result


def choose_team_candidate(name: str, rows: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    target = normalize_team_key(name)
    exact: Dict[int, Mapping[str, Any]] = {}
    for row in rows:
        team = row.get("team") if isinstance(row, dict) else None
        if not isinstance(team, dict):
            continue
        team_id = team.get("id")
        candidate_name = str(team.get("name") or "").strip()
        if team_id is None or not candidate_name:
            continue
        if normalize_team_key(candidate_name) == target:
            exact[int(team_id)] = row
    if len(exact) == 1:
        return next(iter(exact.values()))
    return None


def fixture_to_match(item: Mapping[str, Any]) -> Dict[str, Any] | None:
    fixture = item.get("fixture") if isinstance(item.get("fixture"), dict) else {}
    status = fixture.get("status") if isinstance(fixture.get("status"), dict) else {}
    if str(status.get("short") or "").upper() not in COMPLETED:
        return None
    teams = item.get("teams") if isinstance(item.get("teams"), dict) else {}
    home = teams.get("home") if isinstance(teams.get("home"), dict) else {}
    away = teams.get("away") if isinstance(teams.get("away"), dict) else {}
    goals = item.get("goals") if isinstance(item.get("goals"), dict) else {}
    home_name = str(home.get("name") or "").strip()
    away_name = str(away.get("name") or "").strip()
    home_goals = goals.get("home")
    away_goals = goals.get("away")
    raw_date = str(fixture.get("date") or "").strip()
    if not home_name or not away_name or home_goals is None or away_goals is None or len(raw_date) < 10:
        return None
    league = item.get("league") if isinstance(item.get("league"), dict) else {}
    return {
        "competition": str(league.get("name") or "Verified club history").strip(),
        "season": str(league.get("season") or "").strip(),
        "stage": str(league.get("round") or "Historical fixture").strip(),
        "date": raw_date[:10],
        "homeTeam": home_name,
        "awayTeam": away_name,
        "homeGoals": int(home_goals),
        "awayGoals": int(away_goals),
        "sourceLabel": "StatMaker verified API-Football team-specific history",
    }


def match_key(item: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        item.get("date"),
        normalize_team_key(item.get("homeTeam")),
        normalize_team_key(item.get("awayTeam")),
        item.get("homeGoals"),
        item.get("awayGoals"),
    )


def aliases_for_candidate(feed_name: str, team_row: Mapping[str, Any]) -> List[str]:
    team = team_row.get("team") if isinstance(team_row.get("team"), dict) else {}
    values = [feed_name, str(team.get("name") or "").strip(), str(team.get("code") or "").strip()]
    return list(dict.fromkeys(value for value in values if value))


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill only missing UEFA qualifier participant support")
    parser.add_argument("--feed-dir", type=Path, default=DEFAULT_FEED_DIR)
    parser.add_argument("--min-matches", type=int, default=10)
    parser.add_argument("--max-matches", type=int, default=12)
    parser.add_argument("--fixture-lookback", type=int, default=30)
    parser.add_argument("--max-requests", type=int, default=300)
    args = parser.parse_args()

    api_key = os.getenv("API_FOOTBALL_KEY", "").strip()
    if not api_key:
        raise SystemExit("API_FOOTBALL_KEY is required")

    support = load_json(SUPPORT_PATH, {})
    base_participants: List[Dict[str, Any]] = [dict(row) for row in support.get("participants", []) or [] if isinstance(row, dict)]
    team_support = load_json(TEAM_SUPPORT_PATH, {})
    team_participants: List[Dict[str, Any]] = [dict(row) for row in team_support.get("participants", []) or [] if isinstance(row, dict)]
    participants: List[Dict[str, Any]] = base_participants + team_participants
    index = support_index(participants)
    feed_teams = feed_team_names(args.feed_dir)
    requested: Dict[str, set[str]] = defaultdict(set)
    for competition, names in feed_teams.items():
        for name in names:
            requested[normalize_team_key(name)].add(competition)

    missing_names: Dict[str, str] = {}
    for names in feed_teams.values():
        for name in names:
            key = normalize_team_key(name)
            if key and key not in index:
                missing_names.setdefault(key, name)

    logos = load_json(LOGOS_PATH, {})
    if not isinstance(logos, dict):
        logos = {}
    for participant in participants:
        logo = str(participant.get("logo") or "").strip()
        if not logo.startswith("https://"):
            continue
        for alias in participant_keys(participant):
            logos.setdefault(alias, logo)

    request_state = {"count": 0}
    added: List[str] = []
    unresolved: List[Dict[str, Any]] = []

    for key, feed_name in sorted(missing_names.items(), key=lambda item: item[1].casefold()):
        if request_state["count"] >= args.max_requests:
            unresolved.append({"team": feed_name, "reason": "request_cap"})
            continue
        try:
            team_payload = api.api_get(api_key, "teams", {"search": feed_name}, request_state, args.max_requests)
        except api.RequestLimitReached:
            unresolved.append({"team": feed_name, "reason": "request_cap"})
            continue
        except Exception as exc:
            unresolved.append({"team": feed_name, "reason": f"team_lookup:{type(exc).__name__}"})
            continue
        candidate = choose_team_candidate(feed_name, api.response_items(team_payload))
        if candidate is None:
            unresolved.append({"team": feed_name, "reason": "ambiguous_or_unresolved_team_identity"})
            continue

        team = candidate.get("team") if isinstance(candidate.get("team"), dict) else {}
        team_id = team.get("id")
        official_name = str(team.get("name") or feed_name).strip()
        logo = str(team.get("logo") or "").strip()
        country = str((candidate.get("venue") or {}).get("city") or "").strip()
        country = str(team.get("country") or country).strip()

        if request_state["count"] >= args.max_requests:
            unresolved.append({"team": feed_name, "reason": "request_cap_after_identity"})
            continue
        try:
            fixture_payload = api.api_get(
                api_key,
                "fixtures",
                {"team": int(team_id), "last": max(args.fixture_lookback, args.min_matches)},
                request_state,
                args.max_requests,
            )
        except api.RequestLimitReached:
            unresolved.append({"team": feed_name, "reason": "request_cap_after_identity"})
            continue
        except Exception as exc:
            unresolved.append({"team": feed_name, "reason": f"fixture_lookup:{type(exc).__name__}"})
            continue

        matches = [match for item in api.response_items(fixture_payload) if (match := fixture_to_match(item)) is not None]
        unique = {match_key(match): match for match in matches}
        ordered = sorted(unique.values(), key=lambda item: str(item.get("date") or ""), reverse=True)
        if len(ordered) < max(1, args.min_matches):
            unresolved.append({"team": feed_name, "reason": f"insufficient_completed_history:{len(ordered)}"})
            continue

        aliases = aliases_for_candidate(feed_name, candidate)
        participant = {
            "providerTeam": feed_name,
            "canonicalTeam": official_name,
            "aliases": aliases,
            "country": country,
            "leagueCode": "UEFA_TEAM_SUPPORT",
            "matches": ordered[: max(args.min_matches, args.max_matches)],
        }
        if logo.startswith("https://"):
            participant["logo"] = logo
            for alias in aliases:
                alias_key = normalize_team_key(alias)
                if alias_key:
                    logos[alias_key] = logo

        team_participants.append(participant)
        participants.append(participant)
        for participant_key in participant_keys(participant):
            index.setdefault(participant_key, []).append(len(participants) - 1)
        added.append(feed_name)

    team_participants.sort(key=lambda row: (str(row.get("country") or ""), str(row.get("canonicalTeam") or "").casefold()))
    team_support = {
        "schemaVersion": 1,
        "source": "StatMaker verified team-specific API-Football UEFA support",
        "minimumMatchesPerParticipant": max(1, int(args.min_matches)),
        "maximumMatchesPerParticipant": max(int(args.min_matches), int(args.max_matches)),
        "participantCount": len(team_participants),
        "participants": team_participants,
    }
    write_json(TEAM_SUPPORT_PATH, team_support)
    write_json(LOGOS_PATH, dict(sorted(logos.items())))

    final_participants = base_participants + team_participants
    final_index = support_index(final_participants)
    still_missing = sorted(
        name for key, name in missing_names.items()
        if key not in final_index
    )
    report = {
        "mode": "uefa-team-specific-support-backfill",
        "bettingEngineTouched": False,
        "bettingGatesTouched": False,
        "minimumMatchesPerTeam": max(1, int(args.min_matches)),
        "apiRequestsUsed": request_state["count"],
        "apiRequestCap": int(args.max_requests),
        "fixtureParticipantKeys": len(requested),
        "coveredBefore": len(requested) - len(missing_names),
        "missingBefore": len(missing_names),
        "participantsAdded": len(added),
        "addedTeams": added,
        "stillMissing": still_missing,
        "unresolved": unresolved,
        "baseParticipantCount": len(base_participants),
        "publishedTeamSpecificParticipantCount": len(team_participants),
        "totalParticipantCount": len(final_participants),
        "publishedLogoCount": len(logos),
    }
    write_json(REPORT_PATH, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
