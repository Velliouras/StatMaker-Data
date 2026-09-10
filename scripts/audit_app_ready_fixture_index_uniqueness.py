#!/usr/bin/env python3
"""Fail closed on impossible team/date collisions in the full App-Ready fixture index.

Unlike the recommendation-candidate audit, this inspects prepared_fixture_matches itself using
exactly the latest ready snapshot per competition, which is what Android's
PreparedFixtureReadModelRepository loads. Zero provider/API calls are made.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Tuple

import refresh_live_settlements as settlement


def _norm(value: Any) -> str:
    return settlement.normalize_team(value)


def _identity_key(canonical: str, app_name: str) -> str:
    # Cross-league uniqueness must be based on the canonical/app identity only. Provider aliases
    # such as "SK Rapid" are intentionally NOT used globally because they can collapse unrelated
    # clubs (e.g. Rapid Vienna vs Rapid Bucuresti) after decoration removal.
    return _norm(canonical or app_name)


def _extract_db(bundle: Path, root: Path) -> Path:
    db_path = root / "statmaker_prepared_betting.db"
    with zipfile.ZipFile(bundle, "r") as archive:
        member = "databases/statmaker_prepared_betting.db"
        if member not in archive.namelist():
            raise SystemExit(f"APP_READY_FIXTURE_INDEX_DB_MISSING bundle={bundle.name}")
        with archive.open(member) as source, db_path.open("wb") as target:
            while True:
                block = source.read(1024 * 1024)
                if not block:
                    break
                target.write(block)
    return db_path


def _inspect(bundle: Path) -> Tuple[int, List[Dict[str, Any]]]:
    with tempfile.TemporaryDirectory(prefix="statmaker-fixture-index-audit-") as temp:
        db_path = _extract_db(bundle, Path(temp))
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = db.execute(
                """
                SELECT f.competition_id, f.snapshot_version, f.match_key, f.local_date,
                       f.id, f.league_code, f.country, f.competition,
                       f.provider_home_team, f.provider_away_team,
                       f.home_team, f.away_team,
                       f.canonical_home_team, f.canonical_away_team
                FROM prepared_fixture_matches f
                WHERE f.snapshot_version = (
                    SELECT m.snapshot_version
                    FROM prepared_snapshot_meta m
                    WHERE m.competition_id=f.competition_id
                      AND lower(m.state)='ready'
                    ORDER BY m.built_at_ms DESC
                    LIMIT 1
                )
                ORDER BY f.local_date, f.competition_id, f.match_key
                """
            ).fetchall()
        finally:
            db.close()

    fixtures: List[Dict[str, Any]] = []
    for row in rows:
        (
            competition_id, snapshot_version, match_key, local_date, fixture_id,
            league_code, country, competition, provider_home, provider_away,
            home, away, canonical_home, canonical_away,
        ) = row
        date = str(local_date or "").strip()[:10]
        home = str(home or "").strip()
        away = str(away or "").strip()
        if not date or not home or not away:
            continue
        fixtures.append({
            "bundle": bundle.name,
            "competitionId": str(competition_id or ""),
            "snapshotVersion": str(snapshot_version or ""),
            "matchKey": str(match_key or ""),
            "localDate": date,
            "fixtureId": str(fixture_id or ""),
            "leagueCode": str(league_code or ""),
            "country": str(country or ""),
            "competition": str(competition or ""),
            "homeTeam": home,
            "awayTeam": away,
            "canonicalHomeTeam": str(canonical_home or "").strip(),
            "canonicalAwayTeam": str(canonical_away or "").strip(),
            "providerHomeTeam": str(provider_home or "").strip(),
            "providerAwayTeam": str(provider_away or "").strip(),
        })

    by_team_day: Dict[Tuple[str, str], Dict[Tuple[str, str, str], Dict[str, Any]]] = defaultdict(dict)
    for fixture in fixtures:
        date = fixture["localDate"]
        sides = (
            (
                fixture["canonicalHomeTeam"], fixture["homeTeam"],
                fixture["canonicalAwayTeam"], fixture["awayTeam"],
            ),
            (
                fixture["canonicalAwayTeam"], fixture["awayTeam"],
                fixture["canonicalHomeTeam"], fixture["homeTeam"],
            ),
        )
        for canonical, app_name, opp_canonical, opp_app in sides:
            team_key = _identity_key(canonical, app_name)
            opponent_key = _identity_key(opp_canonical, opp_app)
            if not team_key or not opponent_key:
                continue
            signature = (
                fixture["competitionId"],
                fixture["matchKey"],
                opponent_key,
            )
            by_team_day[(date, team_key)][signature] = fixture

    conflicts: List[Dict[str, Any]] = []
    seen_conflicts = set()
    for (date, team_key), distinct_fixtures in sorted(by_team_day.items()):
        if len(distinct_fixtures) <= 1:
            continue
        conflict_signature = tuple(sorted(
            (f["competitionId"], f["matchKey"], f["homeTeam"], f["awayTeam"])
            for f in distinct_fixtures.values()
        ))
        if conflict_signature in seen_conflicts:
            continue
        seen_conflicts.add(conflict_signature)
        conflicts.append({
            "localDate": date,
            "teamIdentity": team_key,
            "fixtures": list(distinct_fixtures.values()),
        })

    return len(fixtures), conflicts


def main() -> int:
    bundles = settlement.betting_bundle_paths()
    if not bundles:
        raise SystemExit("APP_READY_FIXTURE_INDEX_NO_BUNDLES")

    total = 0
    all_conflicts: List[Dict[str, Any]] = []
    for bundle in bundles:
        count, conflicts = _inspect(bundle)
        total += count
        all_conflicts.extend(conflicts)
        print(
            "APP_READY_FIXTURE_INDEX_BUNDLE",
            f"bundle={bundle.name}",
            f"fixtures={count}",
            f"conflicts={len(conflicts)}",
        )

    if all_conflicts:
        print(
            "APP_READY_FIXTURE_INDEX_TEAM_DATE_CONFLICTS",
            json.dumps(all_conflicts[:100], ensure_ascii=False, indent=2),
        )
        raise SystemExit(
            f"APP_READY_FIXTURE_INDEX_UNIQUENESS_FAILED conflicts={len(all_conflicts)} fixtures={total}"
        )

    print(
        "APP_READY_FIXTURE_INDEX_UNIQUENESS_OK",
        f"bundles={len(bundles)}",
        f"fixtures={total}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
