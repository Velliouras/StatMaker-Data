#!/usr/bin/env python3
"""Materialize the full fixture/filter read model into the prepared betting DB.

Input is the already-canonical compact catalog_payload stored by the existing prepared
snapshot builder. No provider/API call and no raw odds JSON parse is performed here.

The same host step also applies the repository fixture-validity ledger to canonical recommendation
candidates. This is deliberately off-device: a postponed/cancelled/rescheduled fixture is made
ineligible before the immutable App-Ready betting bundle is published.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

COMPETITIONS = (
    "domestic",
    "champions_league",
    "europa_league",
    "conference_league",
)
ATHENS = ZoneInfo("Europe/Athens")
REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_VALIDITY_PATH = REPO_ROOT / "data" / "statmaker" / "fixture_validity.json"
VOID_DISPOSITIONS = {"POSTPONED", "CANCELLED", "RESCHEDULED", "ABANDONED", "AWARDED", "WALKOVER"}


def betting_local_date(match: dict) -> str:
    raw = str(match.get("kickoff") or "").strip()
    if raw:
        normalized = raw.replace(" ", "T", 1) if " " in raw and "T" not in raw else raw
        try:
            if normalized.endswith("Z"):
                dt = datetime.fromisoformat(normalized[:-1] + "+00:00")
            else:
                dt = datetime.fromisoformat(normalized)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(ATHENS).date().isoformat()
        except ValueError:
            pass
    return str(match.get("date") or "").strip()


def match_key(match: dict) -> str:
    fixture_id = str(match.get("id") or "").strip()
    if fixture_id:
        return fixture_id
    return "|".join(
        (
            str(match.get("date") or ""),
            str(match.get("homeTeam") or ""),
            str(match.get("awayTeam") or ""),
        )
    )


def nullable_text(value):
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def all_catalog_matches(payload: dict) -> list[dict]:
    indexed: dict[str, dict] = {}
    for match in payload.get("matches") or []:
        if isinstance(match, dict):
            indexed.setdefault(match_key(match), match)
    for league in payload.get("leagues") or []:
        if not isinstance(league, dict):
            continue
        for match in league.get("matches") or []:
            if isinstance(match, dict):
                indexed.setdefault(match_key(match), match)
    return list(indexed.values())


def create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS prepared_fixture_matches (
            competition_id TEXT NOT NULL,
            snapshot_version TEXT NOT NULL,
            match_key TEXT NOT NULL,
            local_date TEXT NOT NULL,
            id TEXT NOT NULL,
            date TEXT NOT NULL,
            kickoff TEXT NOT NULL,
            league_code TEXT NOT NULL,
            country TEXT NOT NULL,
            competition TEXT NOT NULL,
            season TEXT NOT NULL,
            provider_home_team TEXT NOT NULL,
            provider_away_team TEXT NOT NULL,
            home_team TEXT NOT NULL,
            away_team TEXT NOT NULL,
            canonical_home_team TEXT,
            canonical_away_team TEXT,
            home_team_logo TEXT,
            away_team_logo TEXT,
            team_mapping_status TEXT NOT NULL,
            usable_for_stats INTEGER NOT NULL,
            venue TEXT,
            PRIMARY KEY (competition_id, snapshot_version, match_key)
        );

        CREATE TABLE IF NOT EXISTS prepared_fixture_markets (
            competition_id TEXT NOT NULL,
            snapshot_version TEXT NOT NULL,
            match_key TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            market TEXT NOT NULL,
            selection TEXT NOT NULL,
            team TEXT,
            line REAL,
            odd REAL NOT NULL,
            PRIMARY KEY (competition_id, snapshot_version, match_key, ordinal)
        );

        CREATE INDEX IF NOT EXISTS idx_prepared_fixture_scope
        ON prepared_fixture_matches(
            competition_id, snapshot_version, local_date, country, league_code
        );

        CREATE INDEX IF NOT EXISTS idx_prepared_fixture_date
        ON prepared_fixture_matches(competition_id, snapshot_version, local_date);

        CREATE INDEX IF NOT EXISTS idx_prepared_fixture_market_match
        ON prepared_fixture_markets(competition_id, snapshot_version, match_key);
        """
    )


def fixture_dispositions() -> list[dict]:
    if not FIXTURE_VALIDITY_PATH.is_file():
        return []
    try:
        payload = json.loads(FIXTURE_VALIDITY_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = payload.get("dispositions") if isinstance(payload, dict) else []
    return [row for row in rows or [] if isinstance(row, dict)]


def apply_fixture_validity_gate(connection: sqlite3.Connection) -> int:
    dispositions = fixture_dispositions()
    if not dispositions:
        print("APP_READY_FIXTURE_VALIDITY_OK dispositions=0 blocked=0")
        return 0

    tables = {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    if "prepared_pattern_candidates" not in tables:
        raise SystemExit("Fixture-validity gate requires prepared_pattern_candidates")

    blocked = 0
    for row in dispositions:
        disposition = str(row.get("disposition") or "").strip().upper()
        if disposition not in VOID_DISPOSITIONS:
            continue
        competition_id = str(row.get("competitionId") or "").strip()
        candidate_match_key = str(row.get("matchKey") or "").strip()
        local_date = str(row.get("localDate") or "").strip()[:10]
        if not competition_id or not candidate_match_key or not local_date:
            continue
        reason = f"FIXTURE_{disposition}"
        cursor = connection.execute(
            """
            UPDATE prepared_pattern_candidates
            SET recommendation_eligible=0,
                policy_premium_eligible=0,
                policy_rejection_reason=?
            WHERE competition_id=?
              AND match_key=?
              AND local_date=?
              AND recommendation_eligible=1
            """,
            (reason, competition_id, candidate_match_key, local_date),
        )
        blocked += max(0, int(cursor.rowcount or 0))

    print(
        "APP_READY_FIXTURE_VALIDITY_OK",
        f"dispositions={len(dispositions)}",
        f"blocked={blocked}",
    )
    return blocked


def materialize(prepared_db: Path) -> dict[str, int]:
    connection = sqlite3.connect(prepared_db)
    connection.execute("PRAGMA foreign_keys=OFF")
    quick = connection.execute("PRAGMA quick_check").fetchone()
    if not quick or quick[0] != "ok":
        raise SystemExit(f"Prepared DB quick_check failed: {quick}")

    rows = connection.execute(
        """
        SELECT competition_id, snapshot_version, catalog_payload
        FROM prepared_snapshot_meta
        WHERE state='ready'
        """
    ).fetchall()
    by_competition = {str(row[0]): (str(row[1]), str(row[2])) for row in rows}
    if set(by_competition) != set(COMPETITIONS):
        raise SystemExit(
            f"Prepared DB does not contain exact 4/4 READY snapshots: {sorted(by_competition)}"
        )

    create_schema(connection)
    counts: dict[str, int] = {}

    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("DELETE FROM prepared_fixture_markets")
        connection.execute("DELETE FROM prepared_fixture_matches")

        for competition_id in COMPETITIONS:
            snapshot_version, raw_payload = by_competition[competition_id]
            payload = json.loads(raw_payload)
            matches = all_catalog_matches(payload)
            if not matches:
                raise SystemExit(f"Empty catalog_payload for {competition_id}")

            match_rows = []
            market_rows = []
            for match in matches:
                key = match_key(match)
                if not key:
                    raise SystemExit(f"Blank match key in {competition_id}")
                match_rows.append(
                    (
                        competition_id,
                        snapshot_version,
                        key,
                        betting_local_date(match),
                        str(match.get("id") or ""),
                        str(match.get("date") or ""),
                        str(match.get("kickoff") or ""),
                        str(match.get("leagueCode") or ""),
                        str(match.get("country") or ""),
                        str(match.get("competition") or ""),
                        str(match.get("season") or ""),
                        str(match.get("providerHomeTeam") or ""),
                        str(match.get("providerAwayTeam") or ""),
                        str(match.get("homeTeam") or ""),
                        str(match.get("awayTeam") or ""),
                        nullable_text(match.get("canonicalHomeTeam")),
                        nullable_text(match.get("canonicalAwayTeam")),
                        nullable_text(match.get("homeTeamLogo")),
                        nullable_text(match.get("awayTeamLogo")),
                        str(match.get("teamMappingStatus") or "matched"),
                        1 if bool(match.get("usableForStats", True)) else 0,
                        nullable_text(match.get("venue")),
                    )
                )
                for ordinal, market in enumerate(match.get("markets") or []):
                    if not isinstance(market, dict):
                        continue
                    odd = market.get("odd")
                    if odd is None:
                        continue
                    market_rows.append(
                        (
                            competition_id,
                            snapshot_version,
                            key,
                            ordinal,
                            str(market.get("market") or ""),
                            str(market.get("selection") or ""),
                            nullable_text(market.get("team")),
                            market.get("line"),
                            float(odd),
                        )
                    )

            connection.executemany(
                """
                INSERT INTO prepared_fixture_matches(
                    competition_id, snapshot_version, match_key, local_date,
                    id, date, kickoff, league_code, country, competition, season,
                    provider_home_team, provider_away_team, home_team, away_team,
                    canonical_home_team, canonical_away_team, home_team_logo, away_team_logo,
                    team_mapping_status, usable_for_stats, venue
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                match_rows,
            )
            connection.executemany(
                """
                INSERT INTO prepared_fixture_markets(
                    competition_id, snapshot_version, match_key, ordinal,
                    market, selection, team, line, odd
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                market_rows,
            )
            counts[competition_id] = len(match_rows)
            print(
                "APP_READY_FIXTURE_INDEX_OK",
                f"competition={competition_id}",
                f"matches={len(match_rows)}",
                f"markets={len(market_rows)}",
            )

        for competition_id, expected in counts.items():
            snapshot_version = by_competition[competition_id][0]
            actual = int(
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM prepared_fixture_matches
                    WHERE competition_id=? AND snapshot_version=?
                    """,
                    (competition_id, snapshot_version),
                ).fetchone()[0]
            )
            if actual != expected:
                raise SystemExit(
                    f"Fixture index count mismatch {competition_id}: expected={expected} actual={actual}"
                )

        apply_fixture_validity_gate(connection)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    return counts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("prepared_db", type=Path)
    args = parser.parse_args()
    counts = materialize(args.prepared_db)
    print("APP_READY_FIXTURE_INDEX_READY", " ".join(f"{k}={v}" for k, v in counts.items()))


if __name__ == "__main__":
    main()
