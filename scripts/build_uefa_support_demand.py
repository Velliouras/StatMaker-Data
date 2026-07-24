#!/usr/bin/env python3
"""Build a zero-API-call demand list for UEFA participant support history.

The script reads current CL/EL/Conference repository odds artifacts plus the
published UEFA support history. Teams already covered by cached Domestic history
need no new API-Football calls. Only genuinely missing teams are emitted as
team-specific support demand candidates.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Set

from build_uefa_support_history import normalize_team_key

ROOT = Path(__file__).resolve().parents[1]
SUPPORT_PATH = ROOT / "data" / "statmaker" / "uefa_support_history.json"
OUTPUT_PATH = ROOT / "data" / "statmaker" / "uefa_support_demand.json"
UEFA_FEEDS = {
    "CL": ROOT / "odds" / "odds_api_io" / "champions_league_odds.json",
    "EL": ROOT / "odds" / "odds_api_io" / "europa_league_odds.json",
    "CONF": ROOT / "odds" / "odds_api_io" / "conference_league_odds.json",
}


def load_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def team_names_from_match(match: Dict[str, Any]) -> Iterable[str]:
    for keys in (
        ("canonicalHomeTeam", "homeTeam", "home"),
        ("canonicalAwayTeam", "awayTeam", "away"),
    ):
        for key in keys:
            value = str(match.get(key) or "").strip()
            if value:
                yield value
                break


def team_names_from_unmatched(row: Dict[str, Any]) -> Iterable[str]:
    for key in ("providerHomeTeam", "providerAwayTeam"):
        value = str(row.get(key) or "").strip()
        if value:
            yield value


def current_uefa_demand() -> Dict[str, Set[str]]:
    demand: Dict[str, Set[str]] = defaultdict(set)
    for competition_code, path in UEFA_FEEDS.items():
        payload = load_json(path, {})
        for match in payload.get("matches", []) or []:
            if isinstance(match, dict):
                demand[competition_code].update(team_names_from_match(match))
        debug = payload.get("debug") if isinstance(payload.get("debug"), dict) else {}
        for row in debug.get("unmatchedTeams", []) or []:
            if isinstance(row, dict):
                demand[competition_code].update(team_names_from_unmatched(row))
    return demand


def support_keys() -> Set[str]:
    payload = load_json(SUPPORT_PATH, {})
    keys: Set[str] = set()
    for participant in payload.get("participants", []) or []:
        if not isinstance(participant, dict):
            continue
        values: List[str] = [
            str(participant.get("canonicalTeam") or ""),
            str(participant.get("providerTeam") or ""),
            *[str(value or "") for value in participant.get("aliases", []) or []],
        ]
        for value in values:
            key = normalize_team_key(value)
            if key:
                keys.add(key)
    return keys


def build_payload() -> Dict[str, Any]:
    demand = current_uefa_demand()
    covered = support_keys()
    rows: Dict[str, Dict[str, Any]] = {}

    for competition_code, names in demand.items():
        for name in sorted(names):
            key = normalize_team_key(name)
            if not key:
                continue
            row = rows.setdefault(key, {
                "team": name,
                "normalizedKey": key,
                "competitions": [],
                "supportStatus": "covered" if key in covered else "missing_team_specific_support",
            })
            if competition_code not in row["competitions"]:
                row["competitions"].append(competition_code)

    participants = sorted(rows.values(), key=lambda row: (row["supportStatus"], row["team"].casefold()))
    missing = [row for row in participants if row["supportStatus"] != "covered"]
    return {
        "schemaVersion": 1,
        "purpose": "UEFA participant support demand without reopening excluded Domestic leagues",
        "apiCallsUsedToBuildDemand": 0,
        "policy": {
            "reuseCachedDomesticHistoryFirst": True,
            "reopenExcludedDomesticLeague": False,
            "missingParticipants": "team-specific support queue only",
        },
        "participantCount": len(participants),
        "coveredParticipantCount": len(participants) - len(missing),
        "missingParticipantCount": len(missing),
        "participants": participants,
        "missingParticipants": missing,
    }


def main() -> int:
    payload = build_payload()
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "participantCount": payload["participantCount"],
        "coveredParticipantCount": payload["coveredParticipantCount"],
        "missingParticipantCount": payload["missingParticipantCount"],
        "output": str(OUTPUT_PATH.relative_to(ROOT)),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
