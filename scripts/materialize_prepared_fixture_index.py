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
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from canonical_team_identity import identity_key

COMPETITIONS = (
    "domestic",
    "champions_league",
    "europa_league",
    "conference_league",
)
ATHENS = ZoneInfo("Europe/Athens")
REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_VALIDITY_PATH = REPO_ROOT / "data" / "statmaker" / "fixture_validity.json"
DOMESTIC_INDEX_PATH = REPO_ROOT / "data" / "statmaker" / "domestic_enriched" / "index.json"
SCHEDULE_SIMULATION_HORIZON_DAYS = 14
SCHEDULE_EXCLUDED_STATUSES = {
    "FT", "AET", "PEN", "CANC", "PST", "ABD", "AWD", "WO",
}
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


def schedule_current_entry(row: dict) -> bool:
    lifecycle = str(row.get("lifecycle") or "")
    app_season = str(row.get("app_season") or "")
    target_season = str(row.get("target_app_season") or "")
    cache_path = REPO_ROOT / str(row.get("cache_path") or "")
    return (
        lifecycle == "active"
        and app_season
        and app_season == target_season
        and cache_path.is_file()
        and int(row.get("completed_fixtures") or 0) >= 10
    )


def schedule_local_date(raw_date: str) -> str:
    return betting_local_date({"kickoff": raw_date, "date": raw_date[:10]})


def supplemental_domestic_schedule_rows(snapshot_version: str) -> list[tuple]:
    if not DOMESTIC_INDEX_PATH.is_file():
        return []
    payload = json.loads(DOMESTIC_INDEX_PATH.read_text(encoding="utf-8"))
    today = datetime.now(ATHENS).date()
    last_day = today + timedelta(days=SCHEDULE_SIMULATION_HORIZON_DAYS)
    rows: list[tuple] = []

    for entry in payload.get("leagues") or []:
        if not isinstance(entry, dict) or not schedule_current_entry(entry):
            continue
        cache_path = REPO_ROOT / str(entry.get("cache_path") or "")
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        for fixture in cache.get("schedule_fixtures") or []:
            if not isinstance(fixture, dict):
                continue
            fixture_id = str(fixture.get("fixture_id") or "").strip()
            raw_date = str(fixture.get("date") or "").strip()
            home = str(fixture.get("home_team") or "").strip()
            away = str(fixture.get("away_team") or "").strip()
            status = str(fixture.get("status") or "").strip().upper()
            if not fixture_id or not raw_date or not home or not away:
                continue
            if status in SCHEDULE_EXCLUDED_STATUSES:
                continue
            local_date = schedule_local_date(raw_date)
            try:
                local_day = datetime.fromisoformat(local_date).date()
            except ValueError:
                continue
            if local_day < today or local_day > last_day:
                continue

            rows.append(
                (
                    "domestic",
                    snapshot_version,
                    fixture_id,
                    local_date,
                    fixture_id,
                    raw_date,
                    raw_date,
                    str(entry.get("league_code") or ""),
                    str(entry.get("country") or ""),
                    str(entry.get("league") or ""),
                    str(entry.get("app_season") or ""),
                    home,
                    away,
                    home,
                    away,
                    home,
                    away,
                    nullable_text(fixture.get("home_team_logo")),
                    nullable_text(fixture.get("away_team_logo")),
                    "schedule_only",
                    1,
                    nullable_text(fixture.get("venue")),
                )
            )
    return rows


def nullable_text(value):
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def supplemental_rows_for_gaps(connection: sqlite3.Connection, rows: list[tuple]) -> list[tuple]:
    """Fill free team/dates; provider fixture IDs are not cross-provider identities.

    The catalog owns its fixtures and markets. Supplemental schedules must not add a
    second fixture for either club on that league/date, including conflicting opponents.
    Such exclusions are logged, without rewriting IDs, odds or recommendations.
    """
    def slots(row):
        competition, snapshot, key, day = row[:4]
        league = str(row[7]).strip().upper()
        home = identity_key(row[15] or row[13])
        away = identity_key(row[16] or row[14])
        if not league or not day or not home or not away or home == away:
            raise ValueError(f"Invalid supplemental fixture identity: {key}")
        return {(competition, snapshot, league, day, home),
                (competition, snapshot, league, day, away)}

    occupied = set()
    for row in connection.execute("""
        SELECT competition_id, snapshot_version, match_key, local_date,
               id, date, kickoff, league_code, country, competition, season,
               provider_home_team, provider_away_team, home_team, away_team,
               canonical_home_team, canonical_away_team
        FROM prepared_fixture_matches
    """):
        occupied.update(slots(row))

    accepted = []
    proposed = {}
    excluded = 0
    for row in rows:
        keys = slots(row)
        if keys & occupied:
            excluded += 1
            print("APP_READY_SCHEDULE_FIXTURE_EXCLUDED", json.dumps({
                "fixtureId": row[4], "leagueCode": row[7], "localDate": row[3],
                "homeTeam": row[13], "awayTeam": row[14],
                "reason": "CATALOG_ALREADY_OCCUPIES_TEAM_DATE",
            }, ensure_ascii=False))
            continue
        fixture_key = (row[0], row[1], row[2])
        if any(key in proposed and proposed[key] != fixture_key for key in keys):
            raise ValueError(f"Conflicting supplemental schedule fixtures: {fixture_key}")
        for key in keys:
            proposed[key] = fixture_key
        accepted.append(row)
    print("APP_READY_SCHEDULE_GAPS", f"candidates={len(rows)}",
          f"excluded={excluded}", f"accepted={len(accepted)}")
    return accepted


def catalog_fixture_identity(match: dict):
    """Exact cross-provider game identity; a provider's event ID is only an alias."""
    league = str(match.get("leagueCode") or "").strip().upper()
    home = identity_key(match.get("canonicalHomeTeam") or match.get("homeTeam"))
    away = identity_key(match.get("canonicalAwayTeam") or match.get("awayTeam"))
    try:
        kickoff = datetime.fromisoformat(str(match.get("kickoff") or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if not league or not home or not away or home == away or kickoff.tzinfo is None:
        return None
    return league, kickoff.astimezone(timezone.utc), home, away


def all_catalog_matches(payload: dict, preferred_keys=frozenset()) -> list[dict]:
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
    grouped: dict[tuple, list[dict]] = {}
    for key, match in indexed.items():
        fixture = catalog_fixture_identity(match)
        grouped.setdefault(("fixture", fixture) if fixture else ("event", key), []).append(match)

    result = []
    for matches in grouped.values():
        if len(matches) == 1:
            result.append(matches[0])
            continue
        selected = [match for match in matches if match_key(match) in preferred_keys]
        chosen = max(selected or matches, key=lambda match: len(match.get("markets") or []))
        merged = dict(chosen)
        markets = []
        seen = set()
        # Keep the selected event's exact prices. Alternatives add only missing
        # selections/lines; existing provider records and issued selections remain
        # available in their original tables and catalog_payload.
        for match in [chosen] + [match for match in matches if match is not chosen]:
            for market in match.get("markets") or []:
                key = tuple(market.get(field) for field in ("market", "selection", "team", "line"))
                if key not in seen:
                    seen.add(key)
                    markets.append(market)
            for field in ("venue", "homeTeamLogo", "awayTeamLogo", "squadContext"):
                if not merged.get(field) and match.get(field):
                    merged[field] = match[field]
        merged["markets"] = markets
        result.append(merged)
        print("APP_READY_CATALOG_PROVIDER_FIXTURES_MERGED", json.dumps({
            "leagueCode": chosen.get("leagueCode"), "kickoff": chosen.get("kickoff"),
            "keptId": match_key(chosen), "providerIds": [match_key(match) for match in matches],
            "markets": len(markets),
        }, ensure_ascii=False))
    return result


def recommended_fixture_keys(connection: sqlite3.Connection, competition: str, snapshot: str) -> set[str]:
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"prepared_pattern_candidates", "prepared_selections"}.issubset(tables):
        return set()
    generation = ""
    if "prepared_pattern_generation" in tables:
        generation = """ AND c.generation_id=(SELECT generation_id FROM prepared_pattern_generation
            WHERE state='ready' ORDER BY built_at_ms DESC LIMIT 1)"""
    return {str(row[0]) for row in connection.execute("""
        SELECT DISTINCT s.match_key
        FROM prepared_pattern_candidates c
        JOIN prepared_selections s ON s.competition_id=c.competition_id
          AND s.snapshot_version=c.snapshot_version AND s.selection_key=c.selection_key
        WHERE c.competition_id=? AND c.snapshot_version=? AND c.recommendation_eligible=1
        """ + generation, (competition, snapshot))}


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
            matches = all_catalog_matches(payload, recommended_fixture_keys(connection, competition_id, snapshot_version))
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

        domestic_snapshot = by_competition["domestic"][0]
        supplemental_rows = supplemental_rows_for_gaps(
            connection, supplemental_domestic_schedule_rows(domestic_snapshot)
        )
        if supplemental_rows:
            before = connection.total_changes
            connection.executemany(
                """
                INSERT OR IGNORE INTO prepared_fixture_matches(
                    competition_id, snapshot_version, match_key, local_date,
                    id, date, kickoff, league_code, country, competition, season,
                    provider_home_team, provider_away_team, home_team, away_team,
                    canonical_home_team, canonical_away_team, home_team_logo, away_team_logo,
                    team_mapping_status, usable_for_stats, venue
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                supplemental_rows,
            )
            inserted = connection.total_changes - before
            print(
                "APP_READY_SCHEDULE_FIXTURES_OK",
                f"candidate_rows={len(supplemental_rows)}",
                f"inserted={inserted}",
                f"horizon_days={SCHEDULE_SIMULATION_HORIZON_DAYS}",
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
    # The normal App-Ready publisher can install a genuine evaluated model
    # release directly. No separate certification or approval artifact exists.
    # A missing model release does not interfere with unrelated app assets.
    release_path = os.environ.get("STATMAKER_BETTING_V2_RELEASE_PATH")
    if release_path:
        from betting_v2.production_app_ready import install_release
        v2 = install_release(args.prepared_db, Path(release_path), REPO_ROOT)
        print("APP_READY_VALIDATED_BETTING_V2_STAGED", v2)
    print("APP_READY_FIXTURE_INDEX_READY", " ".join(f"{k}={v}" for k, v in counts.items()))


if __name__ == "__main__":
    main()
