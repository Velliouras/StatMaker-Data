#!/usr/bin/env python3
"""Fail closed when one first-team identity is attached to multiple fixtures on one local date.

This audit reads only the immutable App-Ready betting bundles already committed in the
repository. It makes zero provider/API calls. The runtime candidate key uses the source fixture
date, while local_date is the Athens/UI date; those are intentionally separate date domains.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Tuple

import canonical_team_identity
import refresh_live_settlements as settlement


def _fixture_id(match: Dict[str, Any]) -> int | None:
    squad = match.get("squadContext")
    if isinstance(squad, dict):
        value = settlement.as_int(squad.get("apiFootballFixtureId"))
        if value is not None:
            return value
    for key in ("apiFootballFixtureId", "fixtureId", "fixture_id"):
        value = settlement.as_int(match.get(key))
        if value is not None:
            return value
    return None


def _norm_team(value: Any) -> str:
    return settlement.normalize_team(value)


def _inspect_bundle(path: Path) -> Tuple[int, List[Dict[str, Any]]]:
    with tempfile.TemporaryDirectory(prefix="statmaker-fixture-uniqueness-") as temp_dir:
        db_path = Path(temp_dir) / "statmaker_prepared_betting.db"
        with zipfile.ZipFile(path, "r") as archive:
            member = "databases/statmaker_prepared_betting.db"
            if member not in archive.namelist():
                raise SystemExit(f"APP_READY_FIXTURE_UNIQUENESS_DB_MISSING bundle={path.name}")
            with archive.open(member) as source, db_path.open("wb") as target:
                while True:
                    block = source.read(1024 * 1024)
                    if not block:
                        break
                    target.write(block)

        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            generation_row = connection.execute(
                """
                SELECT generation_id
                FROM prepared_pattern_generation
                WHERE state='ready'
                ORDER BY built_at_ms DESC
                LIMIT 1
                """
            ).fetchone()
            if generation_row is None:
                raise SystemExit(f"APP_READY_FIXTURE_UNIQUENESS_GENERATION_MISSING bundle={path.name}")
            generation_id = str(generation_row[0])
            rows = connection.execute(
                """
                SELECT DISTINCT c.competition_id, c.match_key, c.local_date, c.league_code,
                                m.payload
                FROM prepared_pattern_candidates c
                JOIN prepared_selections s
                  ON s.competition_id=c.competition_id
                 AND s.snapshot_version=c.snapshot_version
                 AND s.selection_key=c.selection_key
                JOIN prepared_matches m
                  ON m.competition_id=s.competition_id
                 AND m.snapshot_version=s.snapshot_version
                 AND m.match_key=s.match_key
                WHERE c.generation_id=? AND c.recommendation_eligible=1
                """,
                (generation_id,),
            ).fetchall()
        finally:
            connection.close()

    fixtures: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}
    for competition_id, candidate_key, local_date, league_code, payload in rows:
        try:
            match = json.loads(str(payload))
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(match, dict):
            continue
        candidate_key = str(candidate_key or "").strip()
        candidate_date = str(local_date or "").strip()[:10]
        if not canonical_team_identity.runtime_key_matches_payload(candidate_key, match):
            continue
        if not candidate_date:
            continue
        home = str(match.get("canonicalHomeTeam") or match.get("homeTeam") or "").strip()
        away = str(match.get("canonicalAwayTeam") or match.get("awayTeam") or "").strip()
        if not home or not away:
            continue
        signature = (str(competition_id or ""), candidate_key, home, away)
        fixtures[signature] = {
            "bundle": path.name,
            "generationId": generation_id,
            "competitionId": str(competition_id or ""),
            "leagueCode": str(league_code or match.get("leagueCode") or "").strip(),
            "matchKey": candidate_key,
            "localDate": candidate_date,
            "sourceDate": str(match.get("date") or "").strip()[:10],
            "homeTeam": home,
            "awayTeam": away,
            "providerHomeTeam": str(match.get("providerHomeTeam") or "").strip(),
            "providerAwayTeam": str(match.get("providerAwayTeam") or "").strip(),
            "apiFixtureId": _fixture_id(match),
        }

    by_team_day: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for fixture in fixtures.values():
        date = fixture["localDate"]
        for team in (fixture["homeTeam"], fixture["awayTeam"]):
            key = _norm_team(team)
            if key:
                by_team_day[(date, key)].append(fixture)

    conflicts: List[Dict[str, Any]] = []
    for (date, normalized_team), candidate_fixtures in sorted(by_team_day.items()):
        unique_signatures: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        for fixture in candidate_fixtures:
            opponent = fixture["awayTeam"] if _norm_team(fixture["homeTeam"]) == normalized_team else fixture["homeTeam"]
            sig = (fixture["matchKey"], _norm_team(opponent), str(fixture.get("apiFixtureId") or ""))
            unique_signatures[sig] = fixture
        if len(unique_signatures) <= 1:
            continue
        conflicts.append({
            "localDate": date,
            "normalizedTeam": normalized_team,
            "fixtures": list(unique_signatures.values()),
        })

    return len(fixtures), conflicts


def main() -> int:
    bundles = settlement.betting_bundle_paths()
    if not bundles:
        raise SystemExit("APP_READY_FIXTURE_UNIQUENESS_NO_BUNDLES")

    total_fixtures = 0
    all_conflicts: List[Dict[str, Any]] = []
    for bundle in bundles:
        fixture_count, conflicts = _inspect_bundle(bundle)
        total_fixtures += fixture_count
        all_conflicts.extend(conflicts)
        print(
            "APP_READY_FIXTURE_UNIQUENESS_BUNDLE",
            f"bundle={bundle.name}",
            f"fixtures={fixture_count}",
            f"conflicts={len(conflicts)}",
        )

    if all_conflicts:
        print("APP_READY_FIXTURE_TEAM_DATE_CONFLICTS", json.dumps(all_conflicts[:50], ensure_ascii=False, indent=2))
        raise SystemExit(
            f"APP_READY_FIXTURE_TEAM_DATE_UNIQUENESS_FAILED conflicts={len(all_conflicts)} fixtures={total_fixtures}"
        )

    print(
        "APP_READY_FIXTURE_TEAM_DATE_UNIQUENESS_OK",
        f"bundles={len(bundles)}",
        f"fixtures={total_fixtures}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
